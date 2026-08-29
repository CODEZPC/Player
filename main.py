"""音乐播放器入口。

启动流程：
1. 先创建隐藏根窗口 + 启动画面（无边框、居中、主题匹配，含图标/应用名/版本号/加载动画）。
2. 后台线程预热导入重量级模块（numpy / pygame / sounddevice…），
   期间主线程泵事件驱动启动画面加载动画滚动。
3. 模块缓存后构建主窗口（极快），关闭启动画面。
"""

import sys
import threading
import tkinter as tk

from utils import APP_NAME, APP_VERSION
from splash import SplashWindow


def _preload_imports(done: threading.Event) -> None:
    """后台线程：预热导入重量级模块（连带 app / audio_engine / numpy / pygame…）。"""
    try:
        import cmd_analyze  # noqa: F401  连带导入 app / audio_engine 等
        import console      # noqa: F401
        import cover_utils  # noqa: F401
        import lrc_parser   # noqa: F401
    finally:
        done.set()


def _pump_while_preloading(root: tk.Tk) -> None:
    """主线程泵事件直到预热完成，让启动画面加载动画持续滚动。"""
    done = threading.Event()
    threading.Thread(target=_preload_imports, args=(done,), daemon=True).start()
    while not done.wait(0.016):
        root.update()
    root.update()  # 渲染最后一帧


def main() -> None:
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None

    # 1. 先建隐藏根窗口 + 启动画面（在重量级模块导入前弹出，掩盖等待）
    root = tk.Tk()
    root.withdraw()
    splash = SplashWindow(root, APP_VERSION, APP_NAME)
    root.update()

    # 2. 预热导入期间动画持续滚动
    _pump_while_preloading(root)

    # 3. 主窗口构建（模块已缓存，极快）→ 由 run_app 关闭启动画面并显示主窗口
    from cmd_analyze import run_app
    run_app(root, initial_file, splash)


if __name__ == "__main__":
    main()