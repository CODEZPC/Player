"""启动画面窗口 —— 在主窗口构建前显示，掩盖重量级模块导入耗时（类似 PS/Unity）。

特性：
- 无窗口修饰符（无边框、无标题栏，`overrideredirect`）
- 屏幕居中
- 主题与主程序匹配（深色背景 + 强调色）
- 内容：应用图标 + 应用名 + 版本号 + 加载动画（不定进度条）

注意：本模块刻意保持轻量（只依赖 tkinter / PIL / utils），
**不要** import app / audio_engine 等重量级模块，否则启动画面会延迟出现。
"""

import tkinter as tk
from tkinter import ttk, font as tkfont

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:  # PIL 缺失时退回文字图标
    _HAS_PIL = False

from utils import APP_NAME, resource_path


# ---- 主题常量：与 app.py 顶部保持一致，调整主题时请同步修改两处 ----
SPLASH_BG = "#23272E"
SPLASH_FG = "#C8C8C8"
SPLASH_ACCENT = "#6FA3FF"
SPLASH_SUBTLE = "#3A3F46"
SPLASH_MUTED = "#9AA0A6"


class SplashWindow(tk.Toplevel):
    """无边框、居中、主题匹配的启动画面。"""

    WIDTH = 420
    HEIGHT = 280

    def __init__(self, root: tk.Tk, version: str, app_name: str = APP_NAME) -> None:
        super().__init__(root)
        self.overrideredirect(True)          # 无窗口修饰符（无边框/标题栏/关闭钮）
        self.configure(bg=SPLASH_BG)

        # 屏幕居中
        x = (self.winfo_screenwidth() - self.WIDTH) // 2
        y = (self.winfo_screenheight() - self.HEIGHT) // 2
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        self._build(app_name, version)
        self.lift()
        self.focus_force()

    # ---------------------------------------------------------------
    # 构建
    # ---------------------------------------------------------------

    def _build(self, app_name: str, version: str) -> None:
        """构建启动画面内容：图标 / 应用名 / 版本号 / 进度条 / 状态文字。"""
        # 图标
        photo = self._load_icon()
        if photo is not None:
            tk.Label(self, image=photo, bg=SPLASH_BG).pack(pady=(36, 10))
            self._icon_photo = photo  # 防止被 GC
        else:
            tk.Label(self, text="♪",
                     font=tkfont.Font(family="Segoe UI", size=42),
                     fg=SPLASH_ACCENT, bg=SPLASH_BG).pack(pady=(30, 0))

        # 应用名
        tk.Label(self, text=app_name,
                 font=tkfont.Font(family="Segoe UI", size=24, weight="bold"),
                 fg=SPLASH_FG, bg=SPLASH_BG).pack()

        # 版本号
        tk.Label(self, text=f"V{version}\n启动中……",
                 font=tkfont.Font(family="Segoe UI", size=11),
                 fg=SPLASH_ACCENT, bg=SPLASH_BG).pack(pady=(2, 20))

        # 加载进度条（不定模式，滚动动画）
        style = ttk.Style(self)
        # Windows 默认 ttk 主题为 vista，原生渲染会忽略 background/troughcolor 等
        # 自定义选项（表现为白底绿块）。强制切到 clam（与主程序一致）样式才生效。
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Splash.Horizontal.TProgressbar",
                        troughcolor=SPLASH_SUBTLE,
                        background=SPLASH_ACCENT,
                        bordercolor=SPLASH_BG,
                        lightcolor=SPLASH_ACCENT,
                        darkcolor=SPLASH_ACCENT)
        self._bar = ttk.Progressbar(self, style="Splash.Horizontal.TProgressbar",
                                    mode="indeterminate", length=300)
        self._bar.pack(pady=(0, 14))
        self._bar.start(25)

        # 底部状态文字
        self._status_var = tk.StringVar(value="正在加载…")
        tk.Label(self, textvariable=self._status_var,
                 font=tkfont.Font(family="Segoe UI", size=10),
                 fg=SPLASH_MUTED, bg=SPLASH_BG).pack(side="bottom", pady=14)

    def _load_icon(self) -> ImageTk.PhotoImage | None:
        """加载 MP.ico 并缩放到 72x72；失败返回 None。"""
        if not _HAS_PIL:
            return None
        try:
            img = Image.open(resource_path("MP.ico"))
            img = img.resize((72, 72), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    # ---------------------------------------------------------------
    # 对外接口
    # ---------------------------------------------------------------

    def set_status(self, text: str) -> None:
        """更新底部状态文本（可选，供调用方在预热阶段提示）。"""
        if self._status_var is not None:
            self._status_var.set(text)

    def close(self) -> None:
        """停止动画并销毁启动画面（幂等，容错已销毁的 Tcl 对象）。"""
        try:
            # 先停止进度条动画，避免应用/窗口销毁时残留 after 回调报 Tcl 错误
            if self._bar is not None:
                try:
                    self._bar.stop()
                except tk.TclError:
                    pass
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass
