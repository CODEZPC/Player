"""共享工具函数 —— 无外部业务依赖的纯工具。"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # 仅类型检查：app 依赖 utils，运行时导入会循环
    from app import LrcPlayerApp


# ===========================================================================
# 应用元信息（版本号唯一出处：改版本号只需改这里）
# ===========================================================================

APP_NAME = "Player PRO"
APP_VERSION = "1.14.20"


# ===========================================================================
# 系统资源
# ===========================================================================

def next_forward_index(pending, times, position: float) -> int:
    """从当前播放位置起环找「下一个仍待处理」的下标（V1.14.14）。

    用途：转写分段与歌词翻译在用户拖动进度条后**实时跟随新位置**——
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


def available_physical_memory() -> int:
    """当前可用物理内存（字节；V1.14.10）。

    用 Windows `GlobalMemoryStatusEx` 的 `ullAvailPhys`（任务管理器「可用」）。
    取不到时返回 -1（调用方应视为「未知」而非「不足」，避免误拦）。
    注：V1.14.18 起转写前的内存门槛已取消，本函数暂无调用方，保留备用。
    """
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return -1
        return int(status.ullAvailPhys)
    except Exception:
        return -1


def suppress_child_console_windows(force: bool = False) -> None:
    """禁止第三方库启动的**控制台子进程**弹出黑窗（V1.14.19，仅 Windows）。

    背景：`translators` 的 Bing 引擎通过 `exejs` 调 `node.exe` 计算 token
    （`server.get_tk` → `exejs.evaluate`），每次会 `subprocess.run` 一个
    `node.exe`。打包成窗口程序（`--windowed`）后，控制台子进程会**闪出一个
    黑色终端窗口**；并行翻译时同时出现多个，表现为「大量终端窗口一闪而过」。

    做法：给 `subprocess.Popen.__init__` 统一注入 `CREATE_NO_WINDOW`
    （`0x08000000`）。只在**自身没有控制台**（窗口版 / `pythonw`）时启用，
    控制台开发环境保持原样；`force=True` 供「已把 stdout 换成哑对象」的
    转写子进程显式启用。一次覆盖所有第三方库，无需改动它们。
    """
    if os.name != "nt":
        return
    if not force and sys.stdout is not None and sys.stderr is not None:
        return                      # 有控制台（源码/控制台版），保持原行为
    try:
        import subprocess

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


# ===========================================================================
# 时间格式化
# ===========================================================================

def format_time(seconds: float | None) -> str:
    """将秒数格式化为 mm:ss 字符串。"""
    if seconds is None:
        return "--:--"
    total = int(max(0.0, seconds))
    minutes = total // 60
    secs = total % 60
    return f"{minutes:02d}:{secs:02d}"


def format_hms(seconds: float) -> str:
    """将秒数格式化为 hh:mm:ss 字符串。"""
    total = int(max(0.0, seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ===========================================================================
# 文本文件读取
# ===========================================================================

def read_text_file(path: str) -> str | None:
    """尝试多种编码读取文本文件内容。

    utf-8-sig 优先：对带 BOM 的 UTF-8 文件自动剥离开头的 BOM 字符
    （BOM 混入歌词文本会干扰行首时间标签解析与逐字展开，V1.8.5）。
    """
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb18030"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeError:
            continue
        except OSError:
            return None
    return None


# ===========================================================================
# 音频时长（线程安全）
# ===========================================================================

def get_duration_mutagen(path: str) -> float | None:
    """用 mutagen 读取音频时长（线程安全，不占用音频设备）。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            return MP3(path).info.length
        elif ext == ".flac":
            from mutagen.flac import FLAC
            return FLAC(path).info.length
        elif ext == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            return OggVorbis(path).info.length
        elif ext == ".wav":
            from mutagen.wave import WAVE
            return WAVE(path).info.length
    except Exception:
        pass
    return None


# ===========================================================================
# 进度条
# ===========================================================================

def make_progress_bar(pct: float, width: int = 10) -> str:
    """生成文字进度条：▓▓▓▓▓░░░ 60%"""
    done = int(round(pct / 100 * width))
    done = max(0, min(width, done))
    return f"{'▓' * done}{'░' * (width - done)} {pct:.0f}%"


# ===========================================================================
# 状态栏分隔线
# ===========================================================================

def status_sep(parent: tk.Frame, fg_color: str = "#C8C8C8") -> None:
    """状态栏分隔竖线。"""
    sep = tk.Frame(parent, bg=fg_color, width=1, height=18)
    sep.pack(side="left", pady=3)


# ===========================================================================
# 悬停提示
# ===========================================================================

def bind_tooltip(widget: tk.Widget, text_var: tk.StringVar,
                 app: "LrcPlayerApp", font: tkfont.Font) -> None:
    """给状态栏标签绑定悬停提示。

    Args:
        app: LrcPlayerApp 实例，需具有 _tooltip 属性（类型仅作注解，
            见文件顶部 `TYPE_CHECKING` 导入）。
    """
    def _enter(event: tk.Event) -> None:
        if app._tooltip:
            return
        t = tk.Toplevel(widget)
        t.wm_overrideredirect(True)
        x = event.x_root + 10
        y = event.y_root - 10
        t.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(t, text=text_var.get(), bg="#FFFFCC", fg="black",
                       font=font, padx=4, pady=2)
        lbl.pack()
        app._tooltip = t

    def _leave(_event: tk.Event) -> None:
        if app._tooltip:
            app._tooltip.destroy()
            app._tooltip = None

    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)


# ===========================================================================
# 资源路径（开发 / Nuitka / PyInstaller 兼容）
# ===========================================================================

def _nuitka_exe_dir() -> str | None:
    """Nuitka 编译环境：返回 exe 所在目录；未编译（开发）返回 None。

    注意：Nuitka 下不能用于定位 exe 的特征——
    `sys.executable` 指向内部的 python.exe、`__file__` 为虚拟路径；
    只有 `sys.argv[0]`（启动时规范化）指向真实 exe（V1.8.6 实测验证）。
    """
    if "__compiled__" not in globals():
        return None
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def resource_path(relative_path: str) -> str:
    """获取资源的绝对路径，兼容开发环境与打包环境（Nuitka / PyInstaller）。

    - 开发环境：本文件（脚本）所在目录的 `_internal`（不依赖当前工作目录）；
    - Nuitka 打包（dist/main：exe 旁 `_internal`）：`sys.argv[0]` 目录的 `_internal`；
    - PyInstaller（旧）：`sys._MEIPASS`（--add-data 已把 _internal 内容放入其根）。
    """
    meipass = getattr(sys, "_MEIPASS", None)   # PyInstaller（保留向后兼容）
    if meipass:
        base_path = meipass
    else:
        exe_dir = _nuitka_exe_dir()            # Nuitka：exe 旁 _internal
        if exe_dir is not None:
            base_path = os.path.join(exe_dir, "_internal")
        else:                                  # 开发环境：脚本目录/_internal
            base_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "_internal")
    return os.path.join(base_path, relative_path)


# ===========================================================================
# 内置字体（V1.13.1：随程序打包，未安装系统字体也能用）
# ===========================================================================

BUNDLED_FONT_REL = "fonts/JC-600W.ttf"   # 相对 `_internal` 的路径（ASCII 文件名：避免打包脚本中文路径乱码）
FONT_LOAD_WARNING = ""      # 加载失败时的提示文本（空 = 正常）
_fonts_loaded = False       # 幂等标记


def load_bundled_fonts() -> tuple[bool, str]:
    """加载随程序打包的字体（Windows `AddFontResourceExW` 私有字体，无需安装）。

    幂等：重复调用只生效一次。返回 `(是否成功, 失败原因)`；失败原因同时
    写入模块级 `FONT_LOAD_WARNING`，供主窗口建成后弹窗提示。
    非 Windows 平台直接跳过（不算失败）。
    """
    global _fonts_loaded, FONT_LOAD_WARNING
    if _fonts_loaded:
        return (FONT_LOAD_WARNING == ""), FONT_LOAD_WARNING
    _fonts_loaded = True
    if os.name != "nt":
        FONT_LOAD_WARNING = ""
        return True, ""
    path = resource_path(BUNDLED_FONT_REL)
    if not os.path.isfile(path):
        FONT_LOAD_WARNING = (f"内置字体文件缺失：\n{path}\n\n"
                             "界面将使用系统默认字体。")
        return False, FONT_LOAD_WARNING
    try:
        import ctypes
        FR_PRIVATE = 0x10        # 仅本进程可用，不写注册表
        added = ctypes.windll.gdi32.AddFontResourceExW(path, FR_PRIVATE, 0)
    except Exception as exc:     # noqa: BLE001
        FONT_LOAD_WARNING = (f"内置字体加载失败：{exc}\n\n"
                             "界面将使用系统默认字体。")
        return False, FONT_LOAD_WARNING
    if not added:
        FONT_LOAD_WARNING = ("内置字体加载失败（系统未接受该字体文件）：\n"
                             f"{path}\n\n界面将使用系统默认字体。")
        return False, FONT_LOAD_WARNING
    FONT_LOAD_WARNING = ""
    return True, ""
