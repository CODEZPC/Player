"""音频播放引擎 —— 封装 pygame.mixer，提供统一的播放接口与文件元数据读取。"""

import os
import re
from typing import Any

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

    封装 pygame.mixer.music，提供加载、播放、暂停、停止、跳转等功能。
    """

    def __init__(self) -> None:
        self._ready = False
        self._current_path: str | None = None
        self._init()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _init(self) -> None:
        """初始化 pygame mixer。"""
        if not PYGAME_AVAILABLE:
            return
        try:
            pygame.mixer.init()
            self._ready = True
        except Exception:
            self._ready = False

    @property
    def ready(self) -> bool:
        """音频引擎是否可用。"""
        return self._ready

    @property
    def current_path(self) -> str | None:
        """当前加载的音频文件路径。"""
        return self._current_path

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
        try:
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
        pygame.mixer.music.play(start=start)

    def pause(self) -> None:
        """暂停播放。"""
        if not self._ready:
            return
        pygame.mixer.music.pause()

    def unpause(self) -> None:
        """恢复播放。"""
        if not self._ready:
            return
        pygame.mixer.music.unpause()

    def stop(self) -> None:
        """停止播放。"""
        if not self._ready:
            return
        pygame.mixer.music.stop()

    def seek(self, seconds: float) -> None:
        """跳转到指定位置并继续播放。"""
        if not self._ready:
            return
        try:
            pygame.mixer.music.play(start=seconds)
        except Exception:
            pass

    def is_busy(self) -> bool:
        """返回 mixer 当前是否处于播放状态。"""
        if not self._ready:
            return False
        return pygame.mixer.music.get_busy()

    # ------------------------------------------------------------------
    # 文件信息
    # ------------------------------------------------------------------

    def get_duration(self, path: str) -> float | None:
        """通过 pygame.Sound 获取音频时长（秒）。

        超过 80 MB 的文件跳过以节省内存。
        """
        if not self._ready:
            return None
        try:
            if os.path.getsize(path) > 80 * 1024 * 1024:
                return None
        except OSError:
            return None
        try:
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

    def quit(self) -> None:
        """释放音频资源。"""
        if self._ready:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.quit()
            except Exception:
                pass
            self._ready = False
