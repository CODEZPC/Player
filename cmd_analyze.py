"""直接通过打开方式打开程序的解析。

IPC 约定（V1.8.7）：服务端 socket 由 main.py 最早期经 `ipc.create_server()`
绑定并传入 `run_app(ipc_server=...)`；本模块负责主窗口就绪后的 accept 循环。
"""

import os
import socket
import threading
import tkinter as tk
from app import LrcPlayerApp
from ipc import create_server, send_to_existing  # send_to_existing 保留 re-export


def _bring_to_front(window: tk.Tk) -> None:
    """将窗口显示并置顶。"""
    window.deiconify()
    window.lift()
    window.focus_force()
    window.attributes("-topmost", True)
    window.after(200, lambda: window.attributes("-topmost", False))


def _start_ipc_listener(app_instance: LrcPlayerApp,
                        server: socket.socket | None = None) -> None:
    """在后台线程中启动 IPC 服务端（accept 循环）。

    server 为启动最早期（main.py）已绑定的 socket（V1.8.7）——主窗口构建
    期间到达的通知由 listen backlog 缓存，不会丢失；为 None 时兜底重绑一次
    （端口被非常规占用等异常情形，失败则无 IPC 静默继续）。
    """

    def listener() -> None:
        srv = server if server is not None else create_server()
        if srv is None:
            return

        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(1024).decode("utf-8")
                conn.close()
                if not data:
                    continue

                parts = data.split("|", 1)
                action = parts[0]
                path = parts[1] if len(parts) > 1 else ""

                if action == "SHOW":
                    app_instance.root.after(0, lambda: _bring_to_front(app_instance.root))
                elif action == "OPEN" and os.path.exists(path):
                    app_instance.root.after(0, lambda p=path: app_instance.handle_external_file(p))
            except socket.timeout:
                continue
            except Exception:
                break
        srv.close()

    threading.Thread(target=listener, daemon=True).start()


def run_app(root: tk.Tk, initial_file: str | None = None,
            splash: tk.Toplevel | None = None,
            ipc_server: socket.socket | None = None) -> None:
    """应用启动入口。

    root 由调用方（main.py）创建并已隐藏（withdraw）；
    splash 为启动画面（Toplevel，可空）；
    ipc_server 为 main.py 最早期绑定的 IPC 服务端 socket（V1.8.7；可空，
    空时兜底重绑）。单实例互斥已在窗口创建前完成，这里只负责接管 accept。
    """
    # 1. 构建主应用
    app = LrcPlayerApp(root, initial_file=initial_file)

    # 2. 关闭启动画面，显示主窗口
    _close_splash(splash)
    root.deiconify()
    root.lift()

    # 3. 启动 IPC accept 循环（复用早前绑定的 socket），进入事件循环
    _start_ipc_listener(app, ipc_server)

    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


def _close_splash(splash: tk.Toplevel | None) -> None:
    """关闭启动画面（幂等，容错已销毁的 Tcl 对象）。

    优先调用 SplashWindow.close()（会先停动画再销毁）；
    对不含 close 方法的通用 Toplevel 直接 destroy。
    """
    if splash is None:
        return
    if hasattr(splash, "close"):
        splash.close()
    else:
        try:
            splash.destroy()
        except tk.TclError:
            pass