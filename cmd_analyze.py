"直接通过打开方式打开程序的解析"

import os
import sys
import socket
import threading
import tkinter as tk
from app import LrcPlayerApp

IPC_PORT = 17345
IPC_HOST = "127.0.0.1"


def send_to_existing(action: str, data: str = "") -> bool:
    """尝试连接已有实例，发送指令（OPEN 或 SHOW）。成功返回 True。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)  # 本机 loopback：0.5s 足够判定；避免无实例时挂满超时
        sock.connect((IPC_HOST, IPC_PORT))
        payload = f"{action}|{data}".encode("utf-8")
        sock.sendall(payload)
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _bring_to_front(window: tk.Tk) -> None:
    """将窗口显示并置顶。"""
    window.deiconify()
    window.lift()
    window.focus_force()
    window.attributes("-topmost", True)
    window.after(200, lambda: window.attributes("-topmost", False))


def _start_ipc_listener(app_instance: LrcPlayerApp) -> None:
    """在后台线程中启动 IPC 服务端。"""

    def listener() -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((IPC_HOST, IPC_PORT))
            server.listen(1)
            server.settimeout(1.0)
        except OSError:
            return  # 端口被占用，说明另一个实例正在运行

        while True:
            try:
                conn, _ = server.accept()
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
        server.close()

    threading.Thread(target=listener, daemon=True).start()


def run_app(root: tk.Tk, initial_file: str | None = None,
            splash: tk.Toplevel | None = None) -> None:
    """应用启动入口。

    root 由调用方（main.py）创建并已隐藏（withdraw）；
    splash 为启动画面（Toplevel，可空）。
    单实例检查已由 main.py 在启动画面期间并行完成，这里不再重复。
    """
    # 1. 构建主应用
    app = LrcPlayerApp(root, initial_file=initial_file)

    # 2. 关闭启动画面，显示主窗口
    _close_splash(splash)
    root.deiconify()
    root.lift()

    # 3. 启动 IPC 服务，让后续进程能找到本实例
    _start_ipc_listener(app)

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