"""共享工具函数 —— 无外部业务依赖的纯工具。"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont


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


# ===========================================================================
# 文本文件读取
# ===========================================================================

def read_text_file(path: str) -> str | None:
    """尝试多种编码读取文本文件内容。"""
    encodings = ["utf-8", "utf-8-sig", "gbk", "gb18030"]
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
    """用 mutagen 读取音频时长（不占用 pygame mixer，线程安全）。"""
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
        app: LrcPlayerApp 实例，需具有 _tooltip 属性。
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
# 资源路径（PyInstaller 兼容）
# ===========================================================================

def resource_path(relative_path: str) -> str:
    """获取资源的绝对路径，兼容开发环境和 PyInstaller 打包后的环境。"""
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    except AttributeError:
        base_path = os.path.abspath(".\\_internal\\")
    return os.path.join(base_path, relative_path)
