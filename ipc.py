"""单实例 IPC —— 轻量模块（仅 socket/time，不依赖 app / tkinter）。

启动互斥策略（V1.8.7）：
- 进程最早期调用 `create_server()` 尝试 bind + listen 本机端口：
  - 成功 → 本实例成为服务端；端口自此被占用，用户再快速重复点击都会被
    识别并静默退出（消除"监听线程启动前"的多重启动窗口期）；
  - 失败 → 已有实例在运行，用 `notify_existing()` 发送 SHOW / OPEN 后退出。
- **Windows 绑定语义坑**：`SO_REUSEADDR` 允许第二个 socket 抢绑同一端口
  （实测两个 REUSEADDR 监听可同时 bind 成功），必须用独占绑定
  `SO_EXCLUSIVEADDRUSE`；非 Windows 平台回退 `SO_REUSEADDR`（多活跃监听
  同端口会失败，语义正确）。
- accept 循环由主窗口就绪后（`cmd_analyze._start_ipc_listener`）启动；
  窗口构建期间到达的通知由 listen backlog 缓存，不会丢失。
"""

import socket
import time

IPC_PORT = 17345
IPC_HOST = "127.0.0.1"
IPC_BACKLOG = 8          # 缓存窗口期内的多条重复点击通知


def create_server() -> socket.socket | None:
    """创建并绑定 IPC 服务端 socket（bind + listen）；端口被占用返回 None。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows：独占绑定（防止第二个实例“抢绑”同端口，V1.8.7）
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        # 非 Windows：SO_REUSEADDR（多个活跃监听同端口会失败，语义正确）
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((IPC_HOST, IPC_PORT))
        server.listen(IPC_BACKLOG)
        server.settimeout(1.0)
    except OSError:
        try:
            server.close()
        except OSError:
            pass
        return None
    return server


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


def notify_existing(action: str, data: str = "", delay: float = 0.15) -> bool:
    """通知已有实例；首次失败稍候重试一次。

    重试用于覆盖对端 `create_server` 内 bind 与 listen 之间的极小窗口
    （此时新连接会被拒绝），避免极快重复点击时误判为"无实例"而双开。
    """
    if send_to_existing(action, data):
        return True
    time.sleep(delay)
    return send_to_existing(action, data)
