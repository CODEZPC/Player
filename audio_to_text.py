"""语音转写（自动字幕）：单文件双角色（V1.14.20）。

**角色一：被主程序 import → `SubtitleWorker` 客户端**
    - 确保外部转写组件 `_internal/extensions/audio_to_text.exe` 存在
      （首次自动下载 `audio_to_text.zip` 并解压）；
    - 以 `audio_to_text.exe --pipe` 启动它，通过 stdin/stdout 的
      **JSON Lines** 协议收发消息（一次一行 JSON）。

**角色二：被打包成 `audio_to_text.exe --pipe` 运行 → Worker**
    - 确保模型缓存 `_internal/extensions/model/modelscope` 存在
      （首次自动下载 `modelscope.zip` 并解压）；
    - 动态加载 FunASR 模型，然后按协议逐行处理 `run` / `position` / `quit`。

为什么要拆成独立 exe：FunASR + torch 体积巨大（>1GB），主程序要保持轻量
（<150MB），因此重型依赖只在 Worker 侧、按需下载。主程序与 Worker 之间
只用 JSON 行通信，互不依赖对方的 Python 环境。

**顶层 import 限制**：只允许轻量模块（见下方 import 段）。`funasr` /
`torch` / `modelscope` / `librosa` / `soundfile` / `numpy` 等一律在 Worker
分支或函数内用 `importlib` 动态导入，主程序打包时用 spec 的 excludes 排除。
本模块**不依赖** utils / tkinter。

消息协议（Worker → 主程序，每行一个 JSON）：
    {"type":"event","kind":"download|load|transcribe|progress|prepare","text":"…"}
    {"type":"partial","items":[[开始秒,"文本"], …]}     # 某一段完成
    {"type":"done","items":[[…]]}                       # 全部完成
    {"type":"error","message":"…"}
主程序 → Worker：
    {"type":"run","path":"…","start_at":0.0}
    {"type":"position","value":12.3}
    {"type":"quit"}

命令行：`python audio_to_text.py <音频路径>`（开发期自测，进程内转写）
         `audio_to_text.exe --pipe`（Worker 模式）
"""

import ctypes
import importlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

# 模型缓存（懒加载；仅在使用它的那个进程内有效）
_model = None
_model_lock = threading.Lock()
_model_device = ""

# ---- 扩展组件（V1.14.20）----
# 组件与模型的下载地址（GitHub Releases）；镜像前缀见 _mirror_prefixes()
COMPONENT_URL = ("https://github.com/CODEZPC/Player/releases/download/"
                 "Extensions/audio_to_text.zip")
MODEL_URL = ("https://github.com/CODEZPC/Player/releases/download/"
             "Model/modelscope.zip")
COMPONENT_EXE = "audio_to_text.exe"
WORKER_LOG = "asr_worker.log"

# 界面提示用的体积估算（GB）：两者合计约 2.8GB
COMPONENT_SIZE_GB = 0.3
MODEL_SIZE_GB = 2.5

# 镜像前缀（按顺序尝试，最后回退 GitHub 原始地址；可用环境变量覆盖）
DEFAULT_MIRRORS = [
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
    "https://ghproxy.cc/",
    "",          # 空串 = 直接用原始 URL
]
MIRRORS_ENV = "PLAYER_GH_MIRRORS"

# ---- 进度事件类型（V1.14.7）----
# 注意：不上报「k/N 段」——funasr 内部分批进度条的 total 等于本批段数，
# 恒为 k===N（表现为 1/1、12/12 跳动），无参考意义。
EVENT_DOWNLOAD = "download"      # 下载（组件/模型/内部模型文件）
EVENT_LOAD = "load"              # 正在加载语音模型
EVENT_TRANSCRIBE = "transcribe"  # 开始转写（文本为空）
EVENT_PROGRESS = "progress"      # 分段进度（文本形如 "2/5"）
EVENT_PREPARE = "prepare"        # 启动前准备（检查模型等，V1.14.20）

# ---- 静音切分参数（V1.14.8）----
# 思路：用能量包络找出句间静音，在静音中段下刀，使每段不短于 1 分钟；
# 若中段没有真静音（带伴奏的歌曲常见），则在每段 60~120s 窗口内取能量
# 最低帧兜底下刀。分段逐段转写（缩短单次推理、显示真实段进度、降低峰值内存）。
SEG_MIN_SEC = 60.0          # 每段最短时长（需求：最短 1min）
SILENCE_FRAME_SEC = 0.05    # 能量包络帧长（50ms）
SILENCE_MIN_SEC = 0.30      # 判为「静音」的最短持续时长
SILENCE_REL_RATIO = 0.05    # 静音阈值 = 95 分位能量 × 该比例
SILENCE_ABS_THRESH = 3e-4   # 静音阈值绝对下限（防全静音文件阈值过低）

# ---- 进度日志解析（V1.14.6 / V1.14.7）----
# 下载：`Downloading [model.pt]:  45%|...`；批量：`Processing 2 items: 100%|...`
_RE_DOWNLOAD = re.compile(r"Downloading \[(?P<name>[^\]]+)\]")
_RE_PCT = re.compile(r"(?P<pct>\d{1,3})%\|")
_RE_GOT_FILES = re.compile(r"Got (?P<n>\d+) files")
_RE_FILE_DONE = re.compile(r"Download model '[^']+' successfully")
# 转写开始：`rtf_avg: 0.16: 100%|...| 14/45 [00:06<00:00, ...]`（只用于探测阶段）
_RE_RTF_STEPS = re.compile(r"rtf_avg[^|]*\|.*?\|\s*\d+/\d+")
_RE_LOADING = re.compile(r"Loading pretrained params|Loading ckpt")


# ===========================================================================
# 空流 / 进程辅助（主程序与 Worker 共用）
# ===========================================================================

class _NullSink:
    """空输出流：打包为窗口程序时 `sys.stdout` 为 None，以此兜底。"""

    def write(self, s):
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("no fileno")


# Windows SetPriorityClass：低于正常（0x00004000）——Worker 不与音频回调抢 CPU
PRIORITY_BELOW_NORMAL = 0x00004000


def _demote_priority() -> None:
    """把当前（Worker）进程优先级降为「低于正常」（非 Windows 静默忽略）。"""
    try:
        kernel32 = ctypes.windll.kernel32       # type: ignore[attr-defined]
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(), PRIORITY_BELOW_NORMAL)
    except Exception:
        pass


def _suppress_child_console_windows(force: bool = False) -> None:
    """禁止第三方库启动的**控制台子进程**弹出黑窗（V1.14.19 内联版）。

    背景：`translators` 用 `exejs` 调 `node.exe`；窗口版程序里每个控制台
    子进程都会被分配一个控制台窗口 → 黑框一闪而过。给 `subprocess.Popen`
    统一注入 `CREATE_NO_WINDOW`（`0x08000000`）即可。

    `force=True` 供「已把 stdout 换成哑对象」的 Worker 显式启用。
    """
    if os.name != "nt":
        return
    if not force and sys.stdout is not None and sys.stderr is not None:
        return                      # 有控制台（源码/控制台版），保持原行为
    try:
        orig_init = subprocess.Popen.__init__
        if getattr(orig_init, "_no_window_patched", False):
            return
        create_no_window = 0x08000000   # CREATE_NO_WINDOW

        def patched(self, *args, **kwargs):
            try:
                flags = int(kwargs.get("creationflags") or 0)
                kwargs["creationflags"] = flags | create_no_window
            except Exception:
                pass
            return orig_init(self, *args, **kwargs)

        patched._no_window_patched = True           # type: ignore[attr-defined]
        subprocess.Popen.__init__ = patched         # type: ignore[method-assign]
    except Exception:
        pass


def _next_forward_index(pending, times, position: float) -> int:
    """从当前播放位置起环找「下一个仍待处理」的下标（V1.14.14 内联版）。

    先处理位置所在的那一项，然后依次向后，到末尾再回到开头。

    Args:
        pending: 仍待处理的下标集合（须为 `times` 的合法下标）。
        times: 全部项的起始时间（升序）。
        position: 当前播放位置（秒）。
    """
    if not pending:
        raise ValueError("pending 不能为空")
    n = len(times)
    cur = 0
    for i, ts in enumerate(times):
        if ts <= position:
            cur = i
        else:
            break
    for k in range(n):
        i = (cur + k) % n
        if i in pending:
            return i
    return next(iter(pending))


# ===========================================================================
# 路径：主程序 / Worker 两侧的 _internal/extensions
# ===========================================================================

def _app_internal_dir() -> str:
    """主程序所在目录下的 `_internal`（开发环境为脚本旁的 `_internal`）。"""
    base = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    candidate = os.path.join(base, "_internal")
    if os.path.isdir(candidate):
        return candidate
    return getattr(sys, "_MEIPASS", base)      # onefile 兜底


def _extensions_dir() -> str:
    """扩展组件目录：`_internal/extensions`。"""
    return os.path.join(_app_internal_dir(), "extensions")


def _component_exe_path() -> str:
    """转写组件 exe 的完整路径。"""
    return os.path.join(_extensions_dir(), COMPONENT_EXE)


def _model_cache_dir() -> str:
    """模型包解压根目录：`_internal/extensions/model/modelscope`。

    注意这是**我们自己的**约定目录（zip 解压到这里），不等于 modelscope 的
    缓存根——见 `_modelscope_cache_root()`。
    """
    return os.path.join(_extensions_dir(), "model", "modelscope")


def _modelscope_cache_root() -> str:
    """模型包目录对应的 `MODELSCOPE_CACHE` 取值（自动兼容两种布局，V1.14.24）。

    modelscope 1.29 在**设置了** `MODELSCOPE_CACHE` 时，把模型放在
    `<CACHE>/models/<ns>/<model>`（不再有 `hub` 这一层）。所以：

    - 模型包按 `<root>/models/...` 解压 → 直接用 `<root>`；
    - 旧包/旧用户缓存是 `<root>/hub/models/...` → 把 CACHE 指到 `<root>/hub`，
      这样 `<CACHE>/models` 正好命中已有文件，**不必搬动几 GB 数据**。
    """
    return _modelscope_cache_root_of(_model_cache_dir())


def _worker_model_root() -> str:
    """Worker 侧模型缓存根（优先环境变量，其次 exe 旁 `model/modelscope`）。"""
    env = os.environ.get("MODELSCOPE_CACHE")
    if env:
        return env
    base = os.path.dirname(os.path.abspath(sys.executable or sys.argv[0] or "."))
    return _modelscope_cache_root_of(
        os.path.join(base, "model", "modelscope"))


def _modelscope_cache_root_of(root: str) -> str:
    """给定模型包目录，返回 modelscope 应使用的 `MODELSCOPE_CACHE`。

    V1.14.24：**按内容**判断（空目录不算数）——
    - `<root>/models` 里有东西 → 新布局，CACHE = `<root>`；
    - 否则 `<root>/hub/models` 里有东西 → 旧布局，CACHE = `<root>/hub`；
    - 都没有 → CACHE = `<root>`（下载会落到 `<root>/models`）。
    """
    def has_content(path: str) -> bool:
        try:
            return os.path.isdir(path) and bool(os.listdir(path))
        except OSError:
            return False

    hub_models = os.path.join(root, "hub", "models")
    if has_content(hub_models) and not has_content(os.path.join(root, "models")):
        return os.path.join(root, "hub")
    return root


def _model_files_present() -> bool:
    """模型是否已就绪（按 modelscope 实际会读的路径判断，V1.14.24）。

    旧实现只判断「`modelscope` 目录非空」，于是 `hub/models` 布局下也判为就绪，
    但 modelscope 会去 `<CACHE>/models` 找 → 找不到就整套重下（实测 1GB+）。
    """
    return _models_present_under(_model_cache_dir())


def missing_packages() -> list[str]:
    """列出缺失的扩展组件 key（供界面提示，V1.14.20）。

    Returns:
        `["component"]` / `["model"]` / 两者 / 空列表（都齐全）。
    """
    missing: list[str] = []
    if not os.path.isfile(_component_exe_path()):
        missing.append("component")
    if not _model_files_present():
        missing.append("model")
    return missing


# ===========================================================================
# 下载与解压（镜像优先）
# ===========================================================================

def _mirror_prefixes() -> list[str]:
    """镜像前缀列表：环境变量 `PLAYER_GH_MIRRORS`（分号分隔）优先。"""
    raw = os.environ.get(MIRRORS_ENV, "").strip()
    if raw:
        items = [p.strip() for p in raw.split(";")]
        return [p for p in items if p != ""] or [""]
    return list(DEFAULT_MIRRORS)


def _download_progress(head: str, done: int, total: int):
    """下载进度 → `(文本, 0~1 比例或 None)`（V1.14.21）。

    比例用于界面画文字进度条（`make_progress_bar`）；总长未知时返回 None，
    文本里改成「已下载 xx MB」供参考。
    """
    if total and total > 0:
        frac = max(0.0, min(1.0, float(done) / float(total)))
        return head, frac
    return f"{head} {done / 1048576:.1f} MB", None


def _validate_zip(path: str) -> bool:
    """zip 是否可打开且无损坏。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except Exception:
        return False


def _download_with_mirrors(url: str, dest: str, on_progress=None,
                           timeout: tuple = (30, 300),
                           should_stop=None) -> bool:
    """按镜像优先级下载到 `dest`（先写 `.part`，校验 zip 后改名）。

    Args:
        url: GitHub 原始地址。
        dest: 目标文件路径。
        on_progress: `fn(done, total)`，已节流（百分比变化时才回调）。
        timeout: `(连接, 读取)` 超时。
        should_stop: `fn() -> bool`，逐块检查；返回 True 则中止下载
            （`.part` 会被清理，函数返回 False）。
    """
    import requests          # 延迟导入：顶层只允许轻量模块

    def stopped() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return True

    prefixes = _mirror_prefixes()
    for prefix in prefixes:
        if stopped():
            return False
        full = (prefix + url) if prefix else url
        part = dest + ".part"
        try:
            with requests.get(full, stream=True, timeout=timeout,
                              allow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last_pct = -1
                tick = 0
                with open(part, "wb") as fp:
                    for chunk in resp.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        fp.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            pct = int(done * 100 / total) if total else -1
                            if pct != last_pct:
                                last_pct = pct
                                try:
                                    on_progress(done, total)
                                except Exception:
                                    pass
                        tick += 1
                        if tick % 16 == 0 and stopped():
                            return False      # 用户取消：finally 会清理 .part
            if stopped():
                return False
            if _validate_zip(part):
                os.replace(part, dest)
                return True
        except Exception:
            pass
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
    return False


def _extract_zip(zip_path: str, dest_dir: str, required_rel: str,
                 on_progress=None) -> bool:
    """解压到 `dest_dir`，并校验关键路径（避免半成品）。

    先解压到 `dest_dir + "_tmp"`，确认 `required_rel` 存在后再逐个移动到
    `dest_dir`（同名覆盖）；任何失败都会清理临时目录并返回 False。

    Args:
        required_rel: 相对 `dest_dir` 的关键路径（组件为 exe 名，模型为 `modelscope`）。
        on_progress: `fn(done, total)`（按文件数）。
    """
    tmp = dest_dir.rstrip("\\/") + "_tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            total = max(1, len(names))
            for i, name in enumerate(names, 1):
                try:
                    zf.extract(name, tmp)
                except Exception:
                    pass
                if on_progress is not None and (i % 20 == 0 or i == total):
                    try:
                        on_progress(i, total)
                    except Exception:
                        pass
        if not os.path.exists(os.path.join(tmp, required_rel)):
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        os.makedirs(dest_dir, exist_ok=True)
        for entry in os.listdir(tmp):
            src = os.path.join(tmp, entry)
            dst = os.path.join(dest_dir, entry)
            try:
                if os.path.isdir(src):
                    shutil.rmtree(dst, ignore_errors=True)
                    shutil.move(src, dst)
                else:
                    if os.path.exists(dst):
                        os.remove(dst)
                    shutil.move(src, dst)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        return True
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return False


# 下载互斥：预下载（V1.14.22）与【转写前确保】可能分别在不同线程里发起，
# 同一时刻只允许一个在写 `.part` / 解压，否则会互踩文件。
_PACKAGE_LOCK = threading.Lock()


def _ensure_component(on_progress=None, should_stop=None) -> bool:
    """确保转写组件 exe 存在（必要时下载 `audio_to_text.zip` 并解压）。"""
    with _PACKAGE_LOCK:
        exe = _component_exe_path()
        if os.path.isfile(exe):
            return True
        ext = _extensions_dir()
        try:
            os.makedirs(ext, exist_ok=True)
        except Exception:
            pass
        zip_path = os.path.join(ext, "audio_to_text.zip")
        if not _download_with_mirrors(COMPONENT_URL, zip_path,
                                      on_progress=on_progress,
                                      should_stop=should_stop):
            return False
        ok = _extract_zip(zip_path, ext, required_rel=COMPONENT_EXE)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return ok and os.path.isfile(exe)


def _ensure_worker_model(on_progress=None, root: str = "",
                         should_stop=None) -> bool:
    """确保模型缓存存在（必要时下载 `modelscope.zip` 并解压）。

    `root`：模型包目录（zip 解压目标，默认 `_worker_model_root()`）；
    主程序侧预下载时传 `_model_cache_dir()`（V1.14.22）。

    V1.14.24：就绪判定改为**按 modelscope 实际会读的路径**
    （`<cache_root>/models` 非空），避免「`hub/models` 布局被误判为已就绪、
    结果 modelscope 又整套重下」。
    """
    with _PACKAGE_LOCK:
        root = root or _worker_model_root()
        if _models_present_under(root):
            return True
        model_dir = os.path.dirname(root) or root
        try:
            os.makedirs(model_dir, exist_ok=True)
        except Exception:
            pass
        zip_path = os.path.join(model_dir, "modelscope.zip")
        if not _download_with_mirrors(MODEL_URL, zip_path,
                                      on_progress=on_progress,
                                      should_stop=should_stop):
            return False
        ok = _extract_zip(zip_path, model_dir,
                          required_rel=os.path.basename(root.rstrip("\\/")))
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return ok and _models_present_under(root)


def _models_present_under(root: str) -> bool:
    """`root` 下是否已有 modelscope 能直接命中的模型文件（V1.14.24）。"""
    cache_root = _modelscope_cache_root_of(root)
    models_dir = os.path.join(cache_root, "models")
    try:
        if os.path.isdir(models_dir) and os.listdir(models_dir):
            return True
    except OSError:
        return False
    return False


def ensure_packages(on_event=None, should_stop=None) -> bool:
    """下载缺失的组件包与模型包（V1.14.22）。

    **只下载解压，不启动 Worker、不加载模型**——供「自动字幕开关一打开就
    预先备好资源」使用。

    Args:
        on_event: `fn(kind, text, progress)`，直接传 `EVENT_*` 与 0~1 进度。
        should_stop: `fn() -> bool`，返回 True 时尽快收尾（不再开始下一项）。

    Returns:
        True 表示两类包均已就绪。
    """
    def emit(kind: str, text: str = "", progress=None) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, text, progress)
        except Exception:
            pass

    def stopped() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return True

    def component_progress(done: int, total: int) -> None:
        text, frac = _download_progress("正在下载转写组件", done, total)
        emit(EVENT_DOWNLOAD, text, frac)

    def model_progress(done: int, total: int) -> None:
        text, frac = _download_progress("正在下载模型", done, total)
        emit(EVENT_DOWNLOAD, text, frac)

    missing = missing_packages()
    if "component" in missing:
        emit(EVENT_DOWNLOAD, "正在下载转写组件")
        if not _ensure_component(on_progress=component_progress,
                                 should_stop=should_stop):
            return False
    if stopped():
        return False
    if "model" in missing_packages():
        emit(EVENT_DOWNLOAD, "正在下载模型")
        if not _ensure_worker_model(on_progress=model_progress,
                                    root=_model_cache_dir(),
                                    should_stop=should_stop):
            return False
    if stopped():
        return False
    emit(EVENT_PREPARE, "转写资源已就绪")
    return True


def _apply_model_env() -> None:
    """写入模型缓存/离线相关环境变量（**必须在导入 funasr 之前**调用）。"""
    root = _worker_model_root()
    if root:
        # modelscope 1.29：<CACHE>/models/<ns>/<model>；旧包在 <root>/hub/models，
        # 就自动把 CACHE 指到 hub（V1.14.24）
        os.environ["MODELSCOPE_CACHE"] = _modelscope_cache_root_of(root)
    os.environ["FUNASR_NO_UPDATE_CHECK"] = "1"
    os.environ["MODELSCOPE_DISABLE_REMOTE"] = "1"


# ===========================================================================
# 进度解析 / 日志重定向（Worker 内使用）
# ===========================================================================

class _ProgressSink:
    """临时替换 stdout/stderr，从 tqdm/日志中提取进度事件。

    tqdm 用 `\\r` 刷新、日志用 `\\n`，因此按两者切分；未结束的尾巴留在缓冲里。
    回调签名为 `fn(kind, text)`（kind 见 `EVENT_*`）。
    """

    def __init__(self, callback=None) -> None:
        self._cb = callback
        self._buf = ""
        self._lock = threading.Lock()
        self.files_total = 0        # Got N files
        self.files_done = 0         # Download ... successfully 次数
        self.cur_name = ""
        self.cur_pct = 0
        self.transcribing = False   # 是否已进入转写阶段（只上报一次）

    # ---- 进度文本 ----
    def _emit(self, kind: str, text: str = "", progress=None) -> None:
        if self._cb is None:
            return
        try:
            self._cb(kind, text, progress)      # 新签名：带 0~1 进度
            return
        except TypeError:
            pass                                # 旧签名，回退两参数式
        except Exception:
            return
        try:
            self._cb(kind, text)
        except Exception:
            pass

    def _download_frac(self):
        """按「已完成文件数 + 当前文件百分比」估算总进度（0~1，未知时 None）。"""
        if not self.files_total:
            return None
        frac = (self.files_done + self.cur_pct / 100.0) / float(self.files_total)
        return max(0.0, min(1.0, frac))

    def _compose_download(self) -> str:
        """下载文本（V1.14.21 起只留短文本：文件名/百分比交给任务框进度条）。"""
        if self.files_total:
            return (f"正在下载模型 "
                    f"{min(self.files_done, self.files_total)}/{self.files_total}")
        return "正在下载模型"

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        m = _RE_GOT_FILES.search(line)
        if m:
            try:
                self.files_total = int(m.group("n"))
            except ValueError:
                self.files_total = 0
            self._emit(EVENT_DOWNLOAD, self._compose_download(),
                       self._download_frac())
            return
        m = _RE_DOWNLOAD.search(line)
        if m:
            self.cur_name = m.group("name").strip()
            mp = _RE_PCT.search(line)
            if mp:
                self.cur_pct = min(100, int(mp.group("pct")))
            self._emit(EVENT_DOWNLOAD, self._compose_download(),
                       self._download_frac())
            return
        if _RE_FILE_DONE.search(line):
            self.files_done += 1
            self.cur_pct = 0
            self._emit(EVENT_DOWNLOAD, self._compose_download(),
                       self._download_frac())
            return
        if _RE_RTF_STEPS.search(line):
            # 只通知「已进入转写阶段」，段数无意义（见 EVENT_* 注释，V1.14.7）
            if not self.transcribing:
                self.transcribing = True
                self._emit(EVENT_TRANSCRIBE)
            return
        if _RE_LOADING.search(line):
            self._emit(EVENT_LOAD, "正在加载语音模型…")

    # ---- 类文件对象 ----
    def write(self, s) -> int:
        if not s:
            return 0
        with self._lock:
            self._buf += s
            while True:
                i_n = self._buf.find("\n")
                i_r = self._buf.find("\r")
                idx = min(x for x in (i_n, i_r) if x >= 0) \
                    if (i_n >= 0 or i_r >= 0) else -1
                if idx < 0:
                    break
                line = self._buf[:idx]
                self._buf = self._buf[idx + 1:]
                self._handle_line(line)
            if len(self._buf) > 4096:      # 防无换行输出撑大缓冲
                self._handle_line(self._buf)
                self._buf = ""
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("no fileno")


def _preload_logging() -> None:
    """提前导入 funasr / modelscope，让它们的 logging handler 先创建好。

    否则 handler 会在 `AutoModel(...)` 构造时才创建（那时已错过重定向时机），
    导致下载/加载日志捕获不到（V1.14.6 实测）。
    """
    for mod in ("modelscope", "modelscope.hub.api", "funasr"):
        try:
            importlib.import_module(mod)
        except Exception:
            pass


def _redirect_log_handlers(sink):
    """把 funasr / modelscope / root 的 StreamHandler 临时指向 sink。

    logging 的 handler 在创建时已绑定 `sys.stderr` 对象，仅替换
    `sys.stderr` 对它们无效（V1.14.6 实测），必须直接改 handler.stream。
    返回 `[(handler, 原 stream)]` 供恢复。
    """
    saved = []
    seen = set()
    loggers = [logging.getLogger()]
    for name in ("funasr", "modelscope", "modelscope_hub", "root"):
        loggers.append(logging.getLogger(name))
    for lg in loggers:
        for h in lg.handlers:
            if id(h) in seen:
                continue
            if isinstance(h, logging.StreamHandler):
                seen.add(id(h))
                try:
                    saved.append((h, h.stream))
                    h.stream = sink
                except Exception:
                    pass
    return saved


def _restore_log_handlers(saved) -> None:
    """恢复 handler 的原始 stream。"""
    for h, stream in saved or []:
        try:
            h.stream = stream
        except Exception:
            pass


# ===========================================================================
# 模型（Worker 侧；全部用 importlib 动态导入）
# ===========================================================================

def is_available() -> bool:
    """funasr 是否可用（仅检查模块是否存在，不加载模型）。"""
    try:
        return importlib.util.find_spec("funasr") is not None
    except Exception:
        return False


def _pick_device() -> str:
    """有 CUDA 用 GPU，否则 CPU。"""
    try:
        torch = importlib.import_module("torch")
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def get_model():
    """获取（并缓存）funasr AutoModel（首次调用会加载模型）。"""
    global _model, _model_device
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            funasr = importlib.import_module("funasr")
            auto_model = getattr(funasr, "AutoModel")
            _model_device = _pick_device()
            _model = auto_model(
                model="paraformer-zh",
                vad_model="fsmn-vad",
                # V1.14.25：改用**小标点模型**（zh-common-vocab272727，约 290MB；
                # `ct-punc` 是 cn-en-large，1131MB）——模型包要控制在 2GiB 内
                punc_model="ct-punc-c",
                timestamp_model="fa-zh",        # 句级时间戳
                # 不再需要说话人分离：字幕只用「时间 + 文本」（V1.14.25 去掉
                # spk_model="cam++"，省 27.5MB；分句改由 sentence_timestamp 提供）
                device=_model_device,
                disable_update=True,            # 关闭联网更新检查（V1.14.4）
            )
    return _model


def model_device() -> str:
    """当前模型设备（未加载时返回空串）。"""
    return _model_device


# ===========================================================================
# 静音切分（V1.14.8）
# ===========================================================================

def _rms_envelope(sf_in, frame: int):
    """按帧计算 RMS 能量包络（逐块读取，内存只保留包络）。

    `sf_in`：已打开的 `soundfile.SoundFile`（当前位置会被改动）；
    `frame`：每帧采样点数。跨块的零头会累加到下一块，保证帧对齐不漂移。
    """
    import numpy as np

    env: list = []
    buf = np.empty(0, dtype=np.float32)
    block = frame * 64
    while True:
        data = sf_in.read(frames=block, dtype="float32", always_2d=True)
        if not len(data):
            break
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        if buf.size:
            mono = np.concatenate([buf, mono])
        n = len(mono) // frame
        if n:
            part = mono[: n * frame].reshape(n, frame)
            env.append(np.sqrt(np.mean(part * part, axis=1)))
        buf = mono[n * frame:]
    if not env:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(env)


def _silence_cut_times(env, frame_sec: float, total_sec: float,
                       min_seg: float) -> list[float]:
    """从能量包络中选出切点（秒）：静音段中点，且每段 ≥ `min_seg`。

    - 静音判定：帧能量 < max(绝对下限, 95 分位能量 × 比例)；
    - 连续静音需不短于 `SILENCE_MIN_SEC`，取该静音段中点下刀；
    - 贪心保证切点间距 ≥ `min_seg`；
    - 若末尾残余不足 `min_seg`，则放弃该切点（并入上一段，避免小尾巴）。
    """
    import numpy as np

    n = int(env.size)
    if n <= 0 or total_sec <= min_seg:
        return []
    thr = max(SILENCE_ABS_THRESH, float(np.percentile(env, 95)) * SILENCE_REL_RATIO)
    silent = env < thr
    min_run = max(1, int(round(SILENCE_MIN_SEC / frame_sec)))
    cuts: list[float] = []
    i = 0
    while i < n:
        if not bool(silent[i]):
            i += 1
            continue
        j = i
        while j < n and bool(silent[j]):
            j += 1
        if j - i >= min_run:
            cuts.append(((i + j) / 2.0) * frame_sec)
        i = j

    picked: list[float] = []
    last = 0.0
    for c in cuts:
        if c - last >= min_seg:
            picked.append(c)
            last = c
    while picked and (total_sec - picked[-1]) < min_seg:
        picked.pop()           # 末尾过短 → 并入上一段
    return picked


def _quiet_cuts_in(env, frame_sec: float, start_sec: float,
                   end_sec: float, min_seg: float) -> list[float]:
    """在 `[start_sec, end_sec)` 内按 60s 步进取「能量最低点」下刀。

    兜底策略（V1.14.8）：带伴奏的歌曲中段往往没有真静音，此时在每段
    `[上次切点 + min_seg, + 2×min_seg]` 窗口内取 RMS 最低帧下刀——
    尽量落在停顿/间奏处，且保证每段仍不短于 `min_seg`。
    """
    import numpy as np

    n = int(env.size)
    if n <= 0:
        return []
    lo_frame = max(0, int(round(start_sec / frame_sec)))
    hi_frame = min(n, int(round(end_sec / frame_sec)))
    min_frames = max(1, int(round(min_seg / frame_sec)))
    cuts: list[float] = []
    pos = lo_frame
    while True:
        lo = pos + min_frames
        hi = min(pos + 2 * min_frames, hi_frame)
        if hi - lo < 1:
            break
        idx = int(np.argmin(env[lo:hi])) + lo
        sec = idx * frame_sec
        if end_sec - sec < min_seg:      # 末尾过短 → 结束
            break
        cuts.append(sec)
        pos = idx
    return cuts


def _plan_cuts(env, frame_sec: float, total_sec: float,
               min_seg: float) -> list[float]:
    """切点规划：优先真静音；剩余过长的段再用「最安静处」兜底补齐。"""
    cuts = _silence_cut_times(env, frame_sec, total_sec, min_seg)
    bounds = [0.0] + cuts + [total_sec]
    extra: list[float] = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if end - start > 2.0 * min_seg:      # 该段过长 → 兜底切
            extra.extend(_quiet_cuts_in(env, frame_sec, start, end, min_seg))
    return sorted(set(cuts + extra))


def _write_mono_segment(sf_in, path: str, frames: int, sr: int) -> None:
    """把 `sf_in` 当前起点的 `frames` 帧写成单声道 PCM16 wav。"""
    import soundfile as sf

    chunk = max(1, int(sr * 30))
    with sf.SoundFile(path, mode="w", samplerate=sr, channels=1,
                      subtype="PCM_16") as out:
        left = frames
        while left > 0:
            data = sf_in.read(frames=min(left, chunk), dtype="float32",
                              always_2d=True)
            if not len(data):
                break
            mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
            out.write(mono)
            left -= len(data)


def _clean_stale_temp_dirs() -> None:
    """清理早前被 terminate() 打断时残留的分段临时目录（V1.14.10）。

    Worker 被硬终止时 `finally` 不执行，`player_asr_*` 会留在系统临时目录里
    （每首歌约 100MB+）。这里只删**超过 6 小时**的同名前缀目录，绝不会碰到
    正在使用的目录。
    """
    try:
        import glob

        root = tempfile.gettempdir()
        now = time.time()
        for path in glob.glob(os.path.join(root, "player_asr_*")):
            try:
                if not os.path.isdir(path):
                    continue
                if now - os.path.getmtime(path) < 6 * 3600:
                    continue
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def _split_by_silence(audio_path: str):
    """按静音把音频切成若干段 → `([(段起点秒, wav路径)], 临时目录)`。

    切点策略见 `_plan_cuts()`：优先真静音段中点，无真静音时在段内能量最低处
    兜底下刀（两种情况均保证每段 ≥ `SEG_MIN_SEC`）。
    无法切分（解码失败、总长不足 2×`SEG_MIN_SEC` 等）时返回
    `([(0.0, audio_path)], "")`，退化为「整文件一段」，与旧行为一致。
    """
    tmp_dir = ""
    try:
        import soundfile as sf

        with sf.SoundFile(audio_path) as sf_in:
            sr = int(sf_in.samplerate or 0)
            total_frames = int(sf_in.frames or 0)
            if sr <= 0 or total_frames <= 0:
                return [(0.0, audio_path)], ""
            total_sec = total_frames / float(sr)
            frame = max(1, int(round(SILENCE_FRAME_SEC * sr)))
            env = _rms_envelope(sf_in, frame)
            cuts = _plan_cuts(env, frame / float(sr), total_sec, SEG_MIN_SEC)
            if not cuts:
                return [(0.0, audio_path)], ""
            tmp_dir = tempfile.mkdtemp(prefix="player_asr_")
            bounds = [0.0] + cuts + [total_sec]
            segments: list[tuple[float, str]] = []
            for i in range(len(bounds) - 1):
                start, end = bounds[i], bounds[i + 1]
                start_frame = int(round(start * sr))
                end_frame = int(round(end * sr))
                if end_frame - start_frame <= 0:
                    continue
                path = os.path.join(tmp_dir, f"seg{i:03d}.wav")
                sf_in.seek(start_frame)
                _write_mono_segment(sf_in, path, end_frame - start_frame, sr)
                segments.append((start, path))
            if len(segments) <= 1:          # 切不出来就别用临时文件了
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return [(0.0, audio_path)], ""
            return segments, tmp_dir
    except Exception:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return [(0.0, audio_path)], ""


def _extract_items(result, offset: float) -> list[tuple[float, str]]:
    """把 funasr `generate()` 的返回值转为 `[(开始秒, 文本)]`（叠加段偏移）。"""
    post = importlib.import_module("funasr.utils.postprocess_utils")
    rich = getattr(post, "rich_transcription_postprocess")

    items: list[tuple[float, str]] = []
    for res in (result or []):
        if not isinstance(res, dict):
            continue
        sentences = res.get("sentence_info") or []
        if sentences:
            for seg in sentences:
                if not isinstance(seg, dict):
                    continue
                start = float(seg.get("start", 0) or 0) / 1000.0 + offset
                # funasr 1.4.x 的 sentence_info 用 `text`；旧文档写作 `sentence`
                raw = seg.get("sentence") or seg.get("text") or ""
                text = rich(str(raw)).strip()
                if text:
                    items.append((start, text))
        else:                  # 退化：无分句信息时整段作为一句
            text = rich(str(res.get("text") or "")).strip()
            if text:
                items.append((offset, text))
    return items


def _read_position(source) -> float:
    """读取当前播放位置：支持传入数值或可调用对象（V1.14.11）。"""
    try:
        return float(source() if callable(source) else source or 0.0)
    except Exception:
        return 0.0


def _next_segment(remaining, all_segments, position) -> tuple[float, str]:
    """从当前播放位置起环找下一个待转写的段并从 `remaining` 移除（V1.14.14）。

    每次调前重新读一次位置，因此用户拖动进度条后会**立刻从新位置继续**
    （已转完的段不重做）。
    """
    times = [offset for offset, _path in all_segments]
    pending = {i for i, seg in enumerate(all_segments) if seg in remaining}
    if not pending:
        return remaining.pop(0)
    idx = _next_forward_index(pending, times, _read_position(position))
    seg = all_segments[idx]
    try:
        remaining.remove(seg)
    except ValueError:
        pass
    return seg


def transcribe(audio_path: str, on_event=None, on_partial=None,
               position=0.0) -> list[tuple[float, str]]:
    """在**当前进程**内转写音频 → `[(开始秒, 文本)]`（按时间升序）。

    流程（V1.14.8）：按能量包络找到句间静音 → 在静音处切分为**每段不短于
    `SEG_MIN_SEC`（60s）** 的若干段（临时单声道 wav）→ 逐段转写并叠加
    段起点偏移 → 清理临时文件。切不出合适静音时自动退化为整文件一次转写。

    `on_event(kind, text)`：进度事件回调（kind 为 `EVENT_*` 常量）；
    `on_partial(items)`：**每段转写完立即回调**该段结果（V1.14.9）；
    `position`：当前播放位置——数值或可调用对象。**在模型加载完成后才读取**，
    且**每段开始前重新读一次**：拖动进度条后立即从新位置继续（V1.14.11/14）。
    异常由调用方捕获。
    """
    sink = _ProgressSink(on_event)
    _preload_logging()
    old_out, old_err = sys.stdout, sys.stderr
    saved_handlers = _redirect_log_handlers(sink)
    sys.stdout = sys.stderr = sink          # type: ignore[assignment]
    tmp_dir = ""
    try:
        model = get_model()
        _clean_stale_temp_dirs()
        segments, tmp_dir = _split_by_silence(audio_path)
        total = len(segments)
        if total > 1 and on_event is not None:
            try:
                on_event(EVENT_TRANSCRIBE, "")      # 进入转写阶段
            except Exception:
                pass
        items: list[tuple[float, str]] = []
        remaining = list(segments)
        done = 0
        while remaining:
            # 每段开始前重新定位：拖动进度条后立刻从新位置继续（V1.14.14）
            offset, seg_path = _next_segment(remaining, segments, position)
            done += 1
            if total > 1 and on_event is not None:
                try:
                    on_event(EVENT_PROGRESS, f"{done}/{total}")
                except Exception:
                    pass
            # timestamp=True：字符级时间戳；sentence_timestamp=True：输出
            # `sentence_info`（V1.14.25 去掉 spk_model 后必须显式开，否则只有整段文本）
            result = model.generate(input=seg_path, timestamp=True,
                                    sentence_timestamp=True)
            part = _extract_items(result, offset)
            if part:
                items.extend(part)
                if on_partial is not None:
                    try:
                        on_partial(list(part))      # 逐段即时回传（V1.14.9）
                    except Exception:
                        pass
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        _restore_log_handlers(saved_handlers)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    items.sort(key=lambda x: x[0])
    return items


# ===========================================================================
# 协议辅助（JSON Lines）
# ===========================================================================

def _items_to_json(items) -> list:
    """`[(秒, 文本)]` → JSON 友好结构（列表的列表）。"""
    out = []
    for ts, text in items or []:
        try:
            out.append([float(ts), str(text)])
        except Exception:
            continue
    return out


def _items_from_json(raw) -> list[tuple[float, str]]:
    """JSON 结构 → `[(秒, 文本)]`（容错解析）。"""
    out: list[tuple[float, str]] = []
    for item in raw or []:
        try:
            out.append((float(item[0]), str(item[1])))
        except Exception:
            continue
    return out


def _protocol_to_tuple(obj):
    """Worker 的一行 JSON → 主程序 `poll()` 的消息元组（V1.14.20）。"""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind == "event":
        progress = obj.get("progress")
        try:
            progress = None if progress is None else float(progress)
        except Exception:
            progress = None
        return ("event",
                (str(obj.get("kind") or ""), str(obj.get("text") or ""),
                 progress))
    if kind == "partial":
        return ("partial", _items_from_json(obj.get("items")))
    if kind == "done":
        return ("done", _items_from_json(obj.get("items")))
    if kind == "error":
        return ("error", str(obj.get("message") or "转写失败"))
    return None


# ===========================================================================
# 角色一：SubtitleWorker 客户端（主程序侧）
# ===========================================================================

class SubtitleWorker:
    """外部转写组件（`audio_to_text.exe --pipe`）的客户端（V1.14.20）。

    职责：
    - **按需下载组件**：首次 submit 时若 `_internal/extensions/audio_to_text.exe`
      不存在，先下载 `audio_to_text.zip` 并解压（进度以 `("event",("download",…))`
      上报）；下载失败 → 队列收到 `("error", …)`，下次 submit 可重试。
    - **启动/复用进程**：组件常驻，空闲时 `cancel()` 不杀进程（模型已加载，
      下次秒开）；正在转写时 `cancel()` 才终止进程。
    - **JSON Lines 通信**：写 `run` / `position` / `quit`，读 `event` /
      `partial` / `done` / `error` 并转成元组入队。

    消息协议（`poll()` 返回）：
    - `("event", (kind, text))`：下载/加载/转写进度
    - `("partial", items)`：某一段转写完成的结果（可边转边显示）
    - `("done", items)`：全部完成
    - `("error", msg)`：失败或进程异常
    """

    def __init__(self) -> None:
        self._proc = None
        self._log_fp = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._write_lock = threading.Lock()
        # 发送队列 + 写线程（V1.14.24）：主线程只入队，永不因管道写满而阻塞
        self._outbox: "queue.Queue" = queue.Queue(maxsize=256)
        self._inflight = False                   # 转写任务是否在进行
        self._pos = 0.0
        self._pos_sent = -1e9
        self._pos_sent_at = 0.0
        self._need_restart = False
        self._gen = 0                            # 转写取消世代
        # ---- 资源下载（与转写**解耦**，V1.14.23）----
        # 下载只受 cancel_download() 影响：切歌/取消转写都不会打断下载，
        # 转写任务在下载没完成时**等待**，不会重启第二次下载。
        self._dl_thread: threading.Thread | None = None
        self._dl_gen = 0                         # 下载取消世代
        self._dl_ok = False                      # 上一次下载是否成功
        self._dl_event = threading.Event()       # 下载结束信号（成功/失败/取消）
        self._dl_event.set()

    # ---- 状态 ----
    def alive(self) -> bool:
        """Worker 进程是否存活。"""
        proc = self._proc
        try:
            return bool(proc is not None and proc.poll() is None)
        except Exception:
            return False

    def busy(self) -> bool:
        """是否有转写任务在跑（准备/等待下载/转写中）。"""
        return self._inflight

    def downloading(self) -> bool:
        """资源下载是否在进行。"""
        th = self._dl_thread
        try:
            return bool(th is not None and th.is_alive())
        except Exception:
            return False

    def pending(self) -> bool:
        """是否还有活（转写任务或资源下载）——界面轮询用。"""
        return self._inflight or self.downloading()

    # ---- 任务 ----
    def submit(self, audio_path: str, start_at: float = 0.0) -> bool:
        """提交转写任务（资源没就绪时会先等下载完成，不会重启下载）。

        `start_at`：当前播放位置初值；Worker 会在**模型加载完成后**通过
        `set_position()` 陆续收到的位置决定优先段。
        成功提交返回 True（准备/下载等待/启动都在后台线程，结果经 `poll()` 上报）。
        """
        if self._inflight:
            return False
        self._drain_queue()          # 丢弃上一轮取消留下的残留消息
        self._inflight = True
        self._gen += 1
        gen = self._gen
        try:
            self._pos = float(start_at or 0.0)
        except Exception:
            self._pos = 0.0
        self._pos_sent = -1e9
        threading.Thread(target=self._prepare_and_run,
                         args=(audio_path, gen), daemon=True).start()
        return True

    # ---- 资源下载（独立于转写任务，V1.14.23）----
    def ensure_download(self) -> bool:
        """确保资源在下载/已就绪。

        - 资源已齐 → 立即返回 True；
        - 已在下载 → 返回 True（复用，不重启）；
        - 否则起后台线程下载，返回 True；
        - 起线程失败 → False。
        """
        if not missing_packages():
            self._dl_ok = True
            self._dl_event.set()
            return True
        if self.downloading():
            return True
        self._dl_ok = False
        self._dl_event.clear()
        self._dl_gen += 1
        gen = self._dl_gen
        try:
            self._dl_thread = threading.Thread(
                target=self._download_run, args=(gen,), daemon=True)
            self._dl_thread.start()
        except Exception:
            self._dl_event.set()
            self._dl_thread = None
            return False
        return True

    def _download_run(self, gen: int) -> None:
        """后台线程：下载组件包与模型包（只下载，不启动 Worker）。"""
        def on_event(kind, text="", progress=None) -> None:
            # 下载事件**不过滤世代**：取消转写不应吞掉下载进度（V1.14.23）
            self._queue.put(("event", (kind, text, progress)))

        ok = False
        try:
            ok = ensure_packages(on_event=on_event,
                                 should_stop=lambda: gen != self._dl_gen)
        except Exception:
            ok = False
        cancelled = gen != self._dl_gen
        self._dl_ok = bool(ok) and not cancelled
        if not cancelled:
            if ok:
                self._queue.put(("prepared", None))
            else:
                self._queue.put(
                    ("error", "转写资源下载失败，请检查网络后重试"))
        self._dl_event.set()         # 唤醒等待中的转写任务

    def cancel_download(self) -> None:
        """停止资源下载（用户关闭自动字幕时调用，V1.14.23）。"""
        if not self.downloading():
            return
        self._dl_gen += 1            # 下载线程在下一个检查点收尾退出
        self._dl_ok = False
        self._dl_event.set()         # 让等待中的转写任务立刻返回

    def set_position(self, seconds: float) -> None:
        """写入当前播放位置（主进程持续调用，已节流）。"""
        try:
            self._pos = float(seconds or 0.0)
        except Exception:
            return
        now = time.monotonic()
        if (abs(self._pos - self._pos_sent) < 0.2
                and now - self._pos_sent_at < 0.5):
            return                   # 位置变化不明显 → 不发包
        self._pos_sent = self._pos
        self._pos_sent_at = now
        self._send({"type": "position", "value": self._pos})

    def poll(self, timeout: float = 0.0):
        """取一条消息；无消息返回 None（`timeout=0` 为非阻塞）。"""
        try:
            if timeout and timeout > 0:
                return self._queue.get(timeout=timeout)
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def cancel(self) -> None:
        """取消**转写任务**（在跑则终止进程，空闲则保留热进程）。

        V1.14.23：**完全不动资源下载**——切歌 / 加载真实歌词 / 关闭自动字幕
        都不会打断下载，也不会让下载重启。
        """
        self._gen += 1
        if self.alive():
            self._stop_process()
        self._inflight = False

    def shutdown(self) -> None:
        """退出 Worker（程序关闭时调用）：先 quit，超时再 terminate。

        V1.14.24：`_send()` 已改为只入队（不会阻塞），所以这里直接发 quit 后
        最多等 1.5s，再强制 terminate——关闭流程绝不会卡住。
        """
        self._gen += 1
        proc = self._proc
        self._send({"type": "quit"})
        if proc is not None:
            try:
                proc.wait(timeout=1.5)
            except Exception:
                pass
        self._stop_process()
        self._inflight = False
        self._dl_gen += 1                # 下载线程（daemon）自行收尾

    # ---- 内部：准备与启动 ----
    def _prepare_and_run(self, audio_path: str, gen: int) -> None:
        """后台线程：等资源就绪 → 启动 Worker → 发送 run。

        V1.14.23：资源由 `ensure_download()` 统一下载并与转写任务解耦——
        这里只**等待**下载结束（下载进度照常上报），不再自己发起第二次下载，
        所以切歌/重启转写不会把进行中的下载打断或重新下载。
        """
        def stale() -> bool:
            return gen != self._gen
        try:
            if stale():
                return
            if not missing_packages():
                pass                       # 资源已就绪
            else:
                if not self.ensure_download():
                    if not stale():
                        self._fail("无法启动资源下载")
                    return
                # 等待下载结束（每 0.2s 检查一次取消世代；下载不受影响）
                while not self._dl_event.wait(0.2):
                    if stale():
                        return             # 只退出本任务，下载继续
                if stale():
                    return
                if not self._dl_ok:
                    self._fail("转写资源下载失败，请检查网络后重试")
                    return
        except Exception as exc:
            if not stale():
                self._fail(f"{type(exc).__name__}: {exc}")
            return
        try:
            if stale():
                return
            if self._need_restart or not self.alive():
                self._stop_process()
                if not self._launch():
                    if not stale():
                        self._fail("无法启动转写进程")
                    return
            if stale():
                return
            if not self._send({"type": "run", "path": audio_path,
                               "start_at": self._pos}):
                if not stale():
                    self._fail("无法向转写进程发送任务")
        except Exception as exc:
            if not stale():
                self._fail(f"{type(exc).__name__}: {exc}")

    def _fail(self, message: str) -> None:
        """上报失败并结束本次任务。"""
        self._inflight = False
        self._queue.put(("error", message))

    def _launch(self) -> bool:
        """启动 `audio_to_text.exe --pipe` 并开始读线程。"""
        exe = _component_exe_path()
        if not os.path.isfile(exe):
            return False
        ext = _extensions_dir()
        try:
            os.makedirs(ext, exist_ok=True)
        except Exception:
            pass
        env = os.environ.copy()
        env.update({
            # V1.14.24：取 modelscope 实际会用的缓存根（兼容 hub/models 旧布局）
            "MODELSCOPE_CACHE": _modelscope_cache_root(),
            "FUNASR_NO_UPDATE_CHECK": "1",
            "MODELSCOPE_DISABLE_REMOTE": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        log_fp = None
        try:
            log_fp = open(os.path.join(ext, WORKER_LOG), "a",
                          encoding="utf-8")
        except Exception:
            log_fp = subprocess.DEVNULL
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                [exe, "--pipe"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log_fp, text=True, encoding="utf-8", bufsize=1,
                creationflags=creationflags, env=env, cwd=ext)
        except Exception as exc:
            self._queue.put(("error",
                             f"启动转写进程失败：{type(exc).__name__}: {exc}"))
            if log_fp not in (None, subprocess.DEVNULL):
                try:
                    log_fp.close()
                except Exception:
                    pass
            return False
        self._proc = proc
        self._log_fp = log_fp if log_fp is not subprocess.DEVNULL else None
        self._need_restart = False
        self._pos_sent = -1e9
        self._reader = threading.Thread(target=self._read_loop, args=(proc,),
                                        daemon=True)
        self._reader.start()
        # 写线程（V1.14.24）：主线程入队，由它真正写管道
        self._writer = threading.Thread(target=self._write_loop, args=(proc,),
                                        daemon=True)
        self._writer.start()
        return True

    def _send(self, obj) -> bool:
        """把一行 JSON 放进发送队列（**绝不阻塞调用方**，V1.14.24）。

        Worker 未启动/已退出返回 False；队列满时位置类消息直接丢弃（下一次
        位置会覆盖它），其余消息短超时入队。真正的写由 `_write_loop()` 做。
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self._outbox.put_nowait(data)
            return True
        except queue.Full:
            pass
        if isinstance(obj, dict) and obj.get("type") == "position":
            return True                     # 位置可以丢（后到的最新）
        try:
            self._outbox.put(data, timeout=0.5)
            return True
        except queue.Full:
            self._need_restart = True
            return False

    def _write_loop(self, proc) -> None:
        """写线程：真正把发送队列里的数据写进 Worker 的 stdin。

        放在独立线程后，即使 Worker 长时间不读 stdin（例如旧版组件在下载
        模型、或 onefile 还在解压），也不会把主界面卡住。
        """
        while True:
            data = self._outbox.get()
            if data is None:                # 收尾信号
                return
            if self._proc is not proc:
                return
            with self._write_lock:
                try:
                    proc.stdin.write(data)  # type: ignore[union-attr]
                    proc.stdin.flush()      # type: ignore[union-attr]
                except Exception:
                    self._need_restart = True
                    return

    def _read_loop(self, proc) -> None:
        """读线程：逐行解析 JSON 并入队；EOF 视为进程退出。"""
        bad = 0
        try:
            for line in proc.stdout:        # type: ignore[union-attr]
                text = (line or "").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    bad += 1
                    if bad >= 10:           # 连续 10 行无法解析 → 认为进程异常
                        self._need_restart = True
                        self._queue.put(("error", "转写进程输出异常，已结束"))
                        self._kill(proc)
                        return
                    continue
                bad = 0
                message = _protocol_to_tuple(obj)
                if message is None:
                    continue
                if message[0] in ("done", "error"):
                    self._inflight = False
                self._queue.put(message)
        finally:
            if self._proc is proc:          # 正常/异常退出
                self._inflight = False
                self._need_restart = True
                self._queue.put(("error", "转写进程已退出"))
                self._cleanup_proc(proc)

    # ---- 内部：清理 ----
    def _drain_queue(self) -> None:
        """清空消息队列（新一轮任务开始前丢弃残留）。"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _kill(self, proc) -> None:
        """立即终止进程（尽力而为）。"""
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2.0)
        except Exception:
            pass

    def _stop_process(self) -> None:
        """终止并清理当前进程句柄。"""
        proc = self._proc
        if proc is None:
            return
        self._need_restart = True
        self._kill(proc)
        self._cleanup_proc(proc)

    def _cleanup_proc(self, proc) -> None:
        """关闭管道/日志，释放句柄（只在当前进程仍指向它时清空）。"""
        if self._proc is not proc:
            return
        self._proc = None
        # 让写线程退出：清空待发数据 + 放收尾信号（V1.14.24）
        try:
            while True:
                self._outbox.get_nowait()
        except queue.Empty:
            pass
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        self._writer = None
        for stream in ("stdin", "stdout"):
            handle = getattr(proc, stream, None)
            try:
                if handle is not None:
                    handle.close()      # 阻塞中的 write 会抛错并结束写线程
            except Exception:
                pass
        log_fp = self._log_fp
        self._log_fp = None
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass


# ===========================================================================
# 角色二：Worker 模式（`audio_to_text.exe --pipe`）
# ===========================================================================

_current_position = 0.0
_position_lock = threading.Lock()


def _set_current_position(value) -> None:
    """更新 Worker 侧当前播放位置（线程安全，V1.14.20）。"""
    global _current_position
    try:
        number = float(value or 0.0)
    except Exception:
        return
    with _position_lock:
        _current_position = number


def _get_current_position() -> float:
    """读取 Worker 侧当前播放位置（线程安全）。"""
    with _position_lock:
        return _current_position


def _worker_transcribe(path: str, send, send_event) -> None:
    """Worker 内的转写线程：调用 `transcribe()` 并按协议回传。"""
    try:
        items = transcribe(
            path,
            on_event=lambda kind, text="", progress=None:
                send_event(kind, text, progress),
            on_partial=lambda part: send({"type": "partial",
                                          "items": _items_to_json(part)}),
            position=_get_current_position,
        )
    except Exception as exc:
        send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return
    send({"type": "done", "items": _items_to_json(items)})


def _run_worker_main() -> None:
    """Worker 主流程：保护 stdout → 检查/下载模型 → 加载模型 → 协议循环。"""
    real_stdout = sys.stdout
    if real_stdout is None:
        real_stdout = _NullSink()
    if sys.stderr is None:
        sys.stderr = _NullSink()        # type: ignore[assignment]
    # 防 funasr / tqdm / 第三方 print 污染协议输出
    sys.stdout = sys.stderr             # type: ignore[assignment]

    write_lock = threading.Lock()

    def send(obj) -> None:
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        with write_lock:
            try:
                real_stdout.write(data)
                real_stdout.flush()
            except Exception:
                pass

    def send_event(kind: str, text: str = "", progress=None) -> None:
        obj = {"type": "event", "kind": kind, "text": text}
        if progress is not None:
            try:
                obj["progress"] = round(float(progress), 4)
            except Exception:
                pass
        send(obj)

    _suppress_child_console_windows(force=True)
    _demote_priority()

    # 0) 立刻起 stdin 读线程（V1.14.22 修卡死的关键）：
    #    模型下载/加载可能要几十分钟，若此时不读 stdin，主程序定期发来的
    #    position 会写满管道缓冲区 → 主程序 stdin.write() 阻塞 → 界面卡死。
    #    EOF（主程序退出/被杀）则立刻自杀，不留下继续下载的孤儿进程。
    inbox: "queue.Queue" = queue.Queue()

    def _read_stdin() -> None:
        try:
            for line in sys.stdin:      # type: ignore[union-attr]
                text = (line or "").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "position":
                    _set_current_position(obj.get("value"))
                    continue
                inbox.put(obj)
        except Exception:
            pass
        os._exit(0)                     # 管道已断开：父进程没了，直接退

    threading.Thread(target=_read_stdin, daemon=True).start()

    # 1) 模型缓存检查（首次自动下载）
    send_event(EVENT_PREPARE, "正在检查转写模型…")

    def _model_progress(done: int, total: int) -> None:
        text, frac = _download_progress("正在下载模型", done, total)
        send_event(EVENT_DOWNLOAD, text, frac)

    if not _ensure_worker_model(on_progress=_model_progress):
        send({"type": "error",
              "message": "无法获取转写模型，请检查网络后重试"})
        return

    # 2) 环境变量必须在导入 funasr/modelscope 之前写入
    _apply_model_env()

    # 3) 加载模型（失败直接报错）
    send_event(EVENT_LOAD, "正在加载语音模型…")
    try:
        _preload_logging()
        get_model()
    except Exception as exc:
        send({"type": "error",
              "message": f"加载语音模型失败：{type(exc).__name__}: {exc}"})
        return
    send_event(EVENT_LOAD, "语音模型已就绪")

    # 4) 协议主循环（消息由读线程入队，与下载/加载并行）
    job: dict = {"thread": None}
    try:
        while True:
            obj = inbox.get()
            if obj is None:
                break
            kind = obj.get("type")
            if kind == "quit":
                break
            if kind != "run":
                continue
            thread = job.get("thread")
            if thread is not None and thread.is_alive():
                send({"type": "error", "message": "已有转写任务在进行"})
                continue
            _set_current_position(obj.get("start_at"))
            path = str(obj.get("path") or "")
            thread = threading.Thread(target=_worker_transcribe,
                                      args=(path, send, send_event),
                                      daemon=True)
            job["thread"] = thread
            thread.start()
    finally:
        os._exit(0)


# ===========================================================================
# 入口
# ===========================================================================

if __name__ == "__main__":
    if "--pipe" in sys.argv[1:]:
        _run_worker_main()
    elif len(sys.argv) >= 2:
        # 开发期自测：进程内直接转写（需要本机装有 funasr）
        for start, text in transcribe(sys.argv[1],
                                      on_event=lambda k, t="": None):
            print(f"[{start:.1f}s] {text}")
    else:
        print("用法: python audio_to_text.py <音频路径>")
        print("      audio_to_text.exe --pipe   （Worker 模式）")
