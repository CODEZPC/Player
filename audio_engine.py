"""音频播放引擎。

主后端：sounddevice + soundfile + numpy 流式播放，支持倍速（简单变速 / 保音高）与音量。
兜底后端：pygame.mixer（当 sounddevice/soundfile/numpy 不可用时）。
"""

import os
import re
import threading

import numpy as np

try:
    import sounddevice as sd
    import soundfile as sf
    SD_AVAILABLE = True
except Exception:
    SD_AVAILABLE = False

try:
    import pygame
    PYGAME_AVAILABLE = True
except Exception:
    PYGAME_AVAILABLE = False

try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

# 音轨号标签的常见键名
_TRACK_TAG_KEYS = ("tracknumber", "track", "trck")
_TRACK_NUMBER_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# 相位声码器（保音高时间伸缩）
# ---------------------------------------------------------------------------

class PhaseVocoder:
    """流式相位声码器：时间伸缩（变速不变调）。

    factor = 输出长度 / 输入长度：
      - 倍速 s 播放且保音高 → 需把原音频压缩 1/s 倍 → factor = 1 / s。
    采用 STFT + 相位累加的经典相位声码器，逐声道独立处理。
    """

    def __init__(self, channels: int, factor: float,
                 fft: int = 2048, ha: int = 512):
        self.ch = channels
        self.factor = factor
        self.N = fft
        self.Ha = ha
        self.Hs = max(1, round(ha * factor))
        self.nf = fft // 2 + 1
        self._win = np.hanning(fft).astype(np.float32)
        self._omega = (2 * np.pi * ha *
                       np.arange(self.nf, dtype=np.float64)[:, None] / fft)
        self.reset()

    def reset(self) -> None:
        """重置全部状态（seek/换曲时调用）。"""
        self._ana_phase = np.zeros((self.ch, self.nf), dtype=np.float64)
        self._syn_phase = np.zeros((self.ch, self.nf), dtype=np.float64)
        self._started = False
        self._in_tail = np.empty((0, self.ch), dtype=np.float32)
        self._ola = np.empty((0, self.ch), dtype=np.float32)
        self._base = 0
        self._k = 0  # 全局合成帧索引（跨 process 调用保持）

    def process(self, x: np.ndarray) -> np.ndarray:
        """输入一段音频 (n, ch)，返回时间伸缩后的输出 (m, ch)。

        内部维护输入尾部缓冲，保证跨块处理时帧位置连续对齐。
        """
        data = (np.concatenate([self._in_tail, x], axis=0)
                if len(self._in_tail) else x)
        n = len(data)
        pos = 0
        N, Ha, Hs = self.N, self.Ha, self.Hs
        win = self._win
        omega = self._omega

        while pos + N <= n:
            frame = data[pos:pos + N].astype(np.float32)
            X = np.fft.rfft(frame * win[:, None], axis=0)
            mag = np.abs(X)
            phase = np.angle(X)
            if self._started:
                delta = phase - self._ana_phase - omega
                delta = np.mod(delta + np.pi, 2 * np.pi) - np.pi
                self._syn_phase = (self._syn_phase
                                   + omega * (Hs / Ha) + delta * (Hs / Ha))
            else:
                self._syn_phase = phase.copy()
                self._started = True
            self._ana_phase = phase.copy()

            syn = np.real(np.fft.irfft(
                mag * np.exp(1j * self._syn_phase), N, axis=0))
            syn = syn * win[:, None]

            # 重叠相加（合成位置由全局帧索引决定）
            abs_start = self._k * Hs
            rel = abs_start - self._base
            need = rel + N
            if need > len(self._ola):
                self._ola = np.concatenate([
                    self._ola,
                    np.zeros((need - len(self._ola), self.ch),
                             dtype=np.float32)], axis=0)
            self._ola[rel:rel + N] += syn
            self._k += 1
            pos += Ha

        # 发射已完成叠加的样本（[base, k*Hs)）
        emit = max(0, min(self._k * Hs - self._base, len(self._ola)))
        out = (self._ola[:emit].copy() if emit > 0
               else np.empty((0, self.ch), dtype=np.float32))
        if emit > 0:
            self._ola = self._ola[emit:]
            self._base += emit

        self._in_tail = (data[pos:] if pos < n
                         else np.empty((0, self.ch), dtype=np.float32))
        return out


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的大小字符串。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_track_number(path: str) -> str:
    """从音频文件的元数据中读取音轨号。"""
    if not MUTAGEN_AVAILABLE:
        return "不可用"
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return "读取失败"
    if not audio or not audio.tags:
        return "无标签"
    tags = {key.lower(): value for key, value in audio.tags.items()}
    for key in _TRACK_TAG_KEYS:
        if key not in tags:
            continue
        values = tags.get(key)
        if not values:
            continue
        raw = str(values[0]) if isinstance(values, (list, tuple)) else str(values)
        match = _TRACK_NUMBER_RE.search(raw)
        if match:
            return match.group(0)
    return "无"


# ---------------------------------------------------------------------------
# AudioEngine
# ---------------------------------------------------------------------------

class AudioEngine:
    """音频播放引擎。

    主后端 sounddevice（流式，支持倍速/保音高/音量），兜底 pygame.mixer。
    """

    def __init__(self) -> None:
        self._ready = False
        self._current_path: str | None = None
        self.backend = "none"

        # sounddevice 后端状态
        self._sf = None          # soundfile.SoundFile
        self._stream = None      # sounddevice.OutputStream
        self._lock = threading.Lock()
        self._sr = 0
        self._ch = 2
        self._total_frames = 0
        self._pos_frames = 0.0   # 源采样位置（浮点，允许插值）
        self._paused = False
        self._eof = False
        self._speed = 1.0
        self._volume = 1.0
        self._balance = 0.0   # 声道平衡：-1 全左 ~ +1 全右
        self._gain = 1.0      # 响度增益（前置放大）：0 ~ 2
        self._pitch_fix = False
        self._pv: PhaseVocoder | None = None
        self._pv_spill: np.ndarray = np.empty((0, 2), dtype=np.float32)

        self._init()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _init(self) -> None:
        """优先初始化 sounddevice，失败则回退 pygame。"""
        if SD_AVAILABLE:
            try:
                sd.query_devices(kind="output")
                self.backend = "sounddevice"
                self._ready = True
                return
            except Exception:
                pass
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.init()
                self.backend = "pygame"
                self._ready = True
                return
            except Exception:
                pass
        self.backend = "none"
        self._ready = False

    @property
    def ready(self) -> bool:
        """音频引擎是否可用。"""
        return self._ready

    @property
    def current_path(self) -> str | None:
        """当前加载的音频文件路径。"""
        return self._current_path

    @property
    def samplerate(self) -> int:
        """当前音频采样率。"""
        return self._sr

    @property
    def channels(self) -> int:
        """当前音频声道数。"""
        return self._ch

    # ------------------------------------------------------------------
    # 核心播放控制
    # ------------------------------------------------------------------

    def load(self, path: str) -> bool:
        """加载音频文件。

        Returns:
            bool: 加载成功返回 True，否则返回 False。
        """
        if not self._ready:
            return False
        self.stop()
        try:
            if self.backend == "sounddevice":
                with self._lock:
                    self._close_stream()
                    self._close_file()
                    self._sf = sf.SoundFile(path)
                    self._sr = self._sf.samplerate
                    self._ch = self._sf.channels
                    self._total_frames = self._sf.frames
                    self._pos_frames = 0.0
                    self._eof = False
                    self._paused = False
                    self._pv_spill = np.empty((0, self._ch), dtype=np.float32)
                    self._pv = (PhaseVocoder(self._ch, 1.0 / self._speed)
                                if self._pitch_fix else None)
                    self._stream = sd.OutputStream(
                        samplerate=self._sr, channels=self._ch,
                        dtype="float32", callback=self._callback,
                        blocksize=1024)
            else:
                pygame.mixer.music.stop()
                pygame.mixer.music.load(path)
        except Exception:
            return False
        self._current_path = path
        return True

    def play(self, start: float = 0.0) -> None:
        """从指定秒数开始播放。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            if start > 0:
                self.seek(start)
            self._paused = False
            if self._stream and not self._stream.active:
                self._stream.start()
        else:
            pygame.mixer.music.play(start=start)

    def pause(self) -> None:
        """暂停播放。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            self._paused = True
        else:
            pygame.mixer.music.pause()

    def unpause(self) -> None:
        """恢复播放。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            self._paused = False
        else:
            pygame.mixer.music.unpause()

    def stop(self) -> None:
        """停止播放（重置到开头）。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            with self._lock:
                self._paused = True
                self._eof = False
                if self._stream:
                    try:
                        self._stream.abort()
                    except Exception:
                        pass
                if self._sf:
                    try:
                        self._sf.seek(0)
                    except Exception:
                        pass
                self._pos_frames = 0.0
        else:
            pygame.mixer.music.stop()

    def seek(self, seconds: float) -> None:
        """跳转到指定位置并继续播放。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            if self._sf is None:
                return
            with self._lock:
                frames = int(seconds * self._sr)
                frames = max(0, min(frames, max(0, self._total_frames - 1)))
                self._pos_frames = float(frames)
                self._eof = False
                self._pv_spill = np.empty((0, self._ch), dtype=np.float32)
                if self._pv:
                    self._pv.reset()
        else:
            try:
                pygame.mixer.music.play(start=seconds)
            except Exception:
                pass

    def get_position(self) -> float | None:
        """返回当前源音频位置（秒）；pygame 后端返回 None。"""
        if self.backend != "sounddevice" or self._sf is None:
            return None
        return self._pos_frames / self._sr

    def is_busy(self) -> bool:
        """返回当前是否处于播放状态。"""
        if not self._ready:
            return False
        if self.backend == "sounddevice":
            return bool(self._stream and self._stream.active
                        and not self._paused and not self._eof)
        return pygame.mixer.music.get_busy()

    # ------------------------------------------------------------------
    # 倍速 / 音量 / 保音高
    # ------------------------------------------------------------------

    def set_speed(self, speed: float) -> None:
        """设置倍速（0.01 ~ 10.0）。pygame 后端不支持，忽略。"""
        self._speed = max(0.01, min(10.0, float(speed)))
        if self.backend == "sounddevice" and self._pitch_fix and self._sf:
            with self._lock:
                self._pv = PhaseVocoder(self._ch, 1.0 / self._speed)
                self._pv_spill = np.empty((0, self._ch), dtype=np.float32)

    def get_speed(self) -> float:
        """当前倍速。"""
        return self._speed

    def set_volume(self, volume: float) -> None:
        """设置音量（0.0 ~ 1.0）。"""
        self._volume = max(0.0, min(1.0, float(volume)))
        if self.backend == "pygame":
            try:
                pygame.mixer.music.set_volume(self._volume)
            except Exception:
                pass

    def get_volume(self) -> float:
        """当前音量。"""
        return self._volume

    def set_balance(self, balance: float) -> None:
        """设置声道平衡（-1 全左 ~ +1 全右）。仅 sounddevice 后端生效。"""
        self._balance = max(-1.0, min(1.0, float(balance)))

    def get_balance(self) -> float:
        """当前声道平衡。"""
        return self._balance

    def set_gain(self, gain: float) -> None:
        """设置响度增益（0 ~ 3，1 为原始音量）。仅 sounddevice 后端生效。"""
        self._gain = max(0.0, min(3.0, float(gain)))

    def get_gain(self) -> float:
        """当前响度增益。"""
        return self._gain

    def set_pitch_fix(self, on: bool) -> None:
        """切换保音高模式（变速不变调）。仅 sounddevice 后端生效。"""
        self._pitch_fix = bool(on)
        if self.backend == "sounddevice" and self._sf:
            with self._lock:
                self._pv = (PhaseVocoder(self._ch, 1.0 / self._speed)
                            if self._pitch_fix else None)
                self._pv_spill = np.empty((0, self._ch), dtype=np.float32)

    def get_pitch_fix(self) -> bool:
        """当前是否处于保音高模式。"""
        return self._pitch_fix

    # ------------------------------------------------------------------
    # sounddevice 回调
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int,
                  time_info, status) -> None:
        """PortAudio 回调：输出 frames 个样本。"""
        if self._paused:
            outdata.fill(0.0)
            return
        try:
            with self._lock:
                if self._pitch_fix and self._pv is not None:
                    self._callback_pitch(outdata, frames)
                else:
                    self._callback_simple(outdata, frames)
        except Exception:
            outdata.fill(0.0)

    def _read_range(self, start: int, count: int) -> np.ndarray:
        """从绝对帧位置 start 读取 count 帧（不足补零）。"""
        if self._sf is None or count <= 0:
            return np.zeros((count, self._ch), dtype=np.float32)
        if start >= self._total_frames:
            return np.zeros((count, self._ch), dtype=np.float32)
        self._sf.seek(start)
        data = self._sf.read(count, dtype="float32", always_2d=True)
        if data.shape[0] < count:
            pad = np.zeros((count - data.shape[0], self._ch),
                           dtype=np.float32)
            data = np.concatenate([data, pad], axis=0)
        return data

    def _callback_simple(self, outdata: np.ndarray, frames: int) -> None:
        """简单变速：线性插值重采样（音高随速度变化）。"""
        step = self._speed
        pos0 = self._pos_frames
        if pos0 >= self._total_frames:
            self._eof = True
            outdata.fill(0.0)
            return
        idx = pos0 + np.arange(frames, dtype=np.float64) * step
        i0 = np.minimum(idx.astype(np.int64), self._total_frames - 1)
        i1 = np.minimum(i0 + 1, self._total_frames - 1)
        start = int(np.floor(idx[0]))
        end = int(np.max(i1)) + 1
        chunk = self._read_range(start, end - start)
        rel0 = i0 - start
        rel1 = i1 - start
        a = chunk[rel0]
        b = chunk[rel1]
        frac = (idx - i0).astype(np.float32)[:, None]
        interp = a + (b - a) * frac
        outdata[:] = interp * self._volume
        self._apply_output_gain(outdata)
        self._pos_frames = pos0 + frames * step
        if self._pos_frames >= self._total_frames:
            self._eof = True

    def _callback_pitch(self, outdata: np.ndarray, frames: int) -> None:
        """保音高变速：输入经相位声码器伸缩后再输出。"""
        if self._pos_frames >= self._total_frames:
            self._eof = True
            outdata.fill(0.0)
            return
        need = int(np.ceil(frames * self._speed))
        chunk = self._read_range(int(self._pos_frames), need)
        self._pos_frames += need
        stretched = self._pv.process(chunk)
        if self._pv_spill.size:
            self._pv_spill = np.concatenate([self._pv_spill, stretched],
                                            axis=0)
        else:
            self._pv_spill = stretched
        if len(self._pv_spill) >= frames:
            out = self._pv_spill[:frames]
            self._pv_spill = self._pv_spill[frames:]
            outdata[:] = out * self._volume
        else:
            n = len(self._pv_spill)
            if n:
                outdata[:n] = self._pv_spill * self._volume
            outdata[n:] = 0.0
            self._pv_spill = np.empty((0, self._ch), dtype=np.float32)
        self._apply_output_gain(outdata)

    def _apply_output_gain(self, outdata: np.ndarray) -> None:
        """对输出缓冲施加响度增益与声道平衡（原地修改）。"""
        if self._gain != 1.0:
            outdata *= self._gain
        if self._ch > 1 and abs(self._balance) > 1e-6:
            b = self._balance
            if b <= 0:
                outdata[:, 1] *= (1.0 + b)
            else:
                outdata[:, 0] *= (1.0 - b)

    # ------------------------------------------------------------------
    # 文件信息
    # ------------------------------------------------------------------

    def get_duration(self, path: str) -> float | None:
        """获取音频时长（秒）。"""
        if not self._ready:
            return None
        if self.backend == "sounddevice":
            try:
                return float(sf.info(path).duration)
            except Exception:
                return None
        try:
            if os.path.getsize(path) > 80 * 1024 * 1024:
                return None
            sound = pygame.mixer.Sound(path)
            return float(sound.get_length())
        except Exception:
            return None

    @staticmethod
    def get_file_info(path: str) -> dict[str, str]:
        """获取音频文件的基本信息（不含时长，时长由 get_duration 提供）。

        Returns:
            dict: 包含「文件名」「格式」「文件大小」「音轨号」等字段。
        """
        info: dict[str, str] = {}

        info["文件名"] = os.path.basename(path)

        _, ext = os.path.splitext(path)
        info["格式"] = ext.upper() if ext else "未知"

        try:
            info["文件大小"] = _format_size(os.path.getsize(path))
        except OSError:
            info["文件大小"] = "未知"

        info["音轨号"] = _get_track_number(path)
        info["时长"] = "计算中..."
        info["歌词"] = "未知"

        return info

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass
            self._stream = None

    def _close_file(self) -> None:
        if self._sf is not None:
            try:
                self._sf.close()
            except Exception:
                pass
            self._sf = None

    def quit(self) -> None:
        """释放音频资源。"""
        if not self._ready:
            return
        if self.backend == "sounddevice":
            with self._lock:
                self._close_stream()
                self._close_file()
            self._ready = False
        else:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.quit()
            except Exception:
                pass
            self._ready = False
