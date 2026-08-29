"""音乐播放器入口。

启动流程：
1. 先创建隐藏根窗口 + 启动画面（无边框、居中、主题匹配，含图标/应用名/版本号/加载动画）。
2. 后台线程【预热导入重量级模块】与【单实例检查】并行执行，
   主线程泵事件驱动启动画面加载动画滚动，覆盖整个等待期（无"卡住"停顿）。
3. 判定完成后：已有实例则关闭启动画面退出；否则构建主窗口（极快），关闭启动画面。
"""

import sys
import threading
import time
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


def _check_existing(done: threading.Event,
                    result: dict[str, bool],
                    initial_file: str | None) -> None:
    """后台线程：单实例检查（本机 loopback，正常 <10ms；最坏超时内返回）。"""
    try:
        from cmd_analyze import send_to_existing
        action = "OPEN" if initial_file else "SHOW"
        result["exists"] = send_to_existing(action, initial_file or "")
    finally:
        done.set()


def _pump_until(root: tk.Tk, preload_done: threading.Event,
                check_done: threading.Event) -> None:
    """主线程泵事件直到预热与单实例检查都完成，启动画面动画持续滚动。"""
    while not (preload_done.is_set() and check_done.is_set()):
        root.update()
        time.sleep(0.016)
    root.update()  # 渲染最后一帧


def main() -> None:
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None

    # 1. 先建隐藏根窗口 + 启动画面（在重量级模块导入前弹出，掩盖等待）
    root = tk.Tk()
    root.withdraw()
    splash = SplashWindow(root, APP_VERSION, APP_NAME)
    root.update()

    # 2. 预热导入 + 单实例检查并行；主线程泵动画覆盖整个等待（含 socket 超时）
    preload_done = threading.Event()
    check_done = threading.Event()
    instance_result: dict[str, bool] = {}
    threading.Thread(target=_preload_imports, args=(preload_done,),
                     daemon=True).start()
    threading.Thread(target=_check_existing,
                     args=(check_done, instance_result, initial_file),
                     daemon=True).start()
    _pump_until(root, preload_done, check_done)

    # 3. 已有实例：唤醒完成，关闭启动画面并退出
    if instance_result.get("exists"):
        splash.close()
        root.destroy()
        if initial_file:
            print(f"已将文件发送至已运行的播放器: {initial_file}")
        else:
            print("已唤醒已运行的播放器。")
        return

    # 4. 无实例：构建主窗口（模块已缓存，极快）→ 由 run_app 关闭启动画面
    from cmd_analyze import run_app
    run_app(root, initial_file, splash)


if __name__ == "__main__":
    main()