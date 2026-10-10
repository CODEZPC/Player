"""音乐播放器入口。

启动流程（V1.8.7 起）：
0. 最早期【单实例互斥】：直接尝试绑定 IPC 端口（轻量 ipc 模块，毫秒级，无重量级依赖）
   ——绑定成功即成为服务端，后续重复点击都会被识别并静默退出（消除多重启动窗口期）；
   绑定失败 = 已有实例 → 发送 SHOW/OPEN 通知后本进程直接退出（不创建任何窗口）。
1. 建隐藏根窗口 + 启动画面（无边框、居中、主题匹配，含图标/应用名/版本号/加载动画）。
2. 后台线程【预热导入重量级模块】，主线程泵事件驱动启动画面加载动画滚动，
   覆盖整个等待期（无"卡住"停顿）。
3. 构建主窗口（极快），关闭启动画面；由 run_app 启动 IPC accept 循环
   （复用早前绑定的 socket，窗口构建期间到达的通知由 listen backlog 缓存不丢失）。
"""

import sys
import threading
import time
import tkinter as tk

import ipc
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


def _pump_until(root: tk.Tk, preload_done: threading.Event) -> None:
    """主线程泵事件直到预热导入完成，启动画面动画持续滚动。"""
    while not preload_done.is_set():
        root.update()
        time.sleep(0.016)
    root.update()  # 渲染最后一帧


def main() -> None:
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None

    # 0. 最早期单实例互斥：绑定 IPC 端口（毫秒级；一旦成功，端口即被占用，
    #    此后用户如何快速重复点击都不会再出现多重启动）
    ipc_server = ipc.create_server()
    if ipc_server is None:
        # 端口已被占用 → 已有实例在运行：发送通知后本进程直接退出
        action = "OPEN" if initial_file else "SHOW"
        if ipc.notify_existing(action, initial_file or ""):
            if initial_file:
                print(f"已将文件发送至已运行的播放器: {initial_file}")
            else:
                print("已唤醒已运行的播放器。")
            return
        # 通知失败（端口被非常规占用）：继续启动（无 IPC 兜底，与旧行为一致）
        ipc_server = None

    # 1. 建隐藏根窗口 + 启动画面（在重量级模块导入前弹出，掩盖等待）
    root = tk.Tk()
    root.withdraw()
    splash = SplashWindow(root, APP_VERSION, APP_NAME)
    root.update()

    # 2. 预热导入；主线程泵动画覆盖整个等待
    preload_done = threading.Event()
    threading.Thread(target=_preload_imports, args=(preload_done,),
                     daemon=True).start()
    _pump_until(root, preload_done)

    # 3. 构建主窗口（模块已缓存，极快）→ 由 run_app 关闭启动画面、
    #    启动 IPC accept 循环并进入事件循环
    from cmd_analyze import run_app
    run_app(root, initial_file, splash, ipc_server)


if __name__ == "__main__":
    main()