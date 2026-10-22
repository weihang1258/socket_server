import socketserver
import socket
import threading
import time
import logging

logger = logging.getLogger(__name__)

# 客户端连接追踪
_active_clients = 0
_last_disconnect_time = time.time()
_client_lock = threading.Lock()


def get_idle_state():
    """返回 (活跃客户端数, 距上次客户端断开的秒数)"""
    with _client_lock:
        if _active_clients > 0:
            return _active_clients, 0
        return _active_clients, time.time() - _last_disconnect_time


class TrackedTCPHandler:
    """Mixin: 客户端连接计数。不继承 BaseRequestHandler，以确保 MRO 正确。"""

    def setup(self):
        global _active_clients
        with _client_lock:
            _active_clients += 1
        super().setup()

    def finish(self):
        global _active_clients, _last_disconnect_time
        with _client_lock:
            if _active_clients > 0:
                _active_clients -= 1
            if _active_clients == 0:
                _last_disconnect_time = time.time()
        super().finish()


def start_tcp_server(port, handler_class):
    """启动 IPv4 + IPv6 双栈 TCP 服务器，阻塞运行。"""
    handler = handler_class

    # 1.8.1: allow_reuse_address=True —— 旧进程退出后 :9000 的 TIME_WAIT 残留
    # 不再阻塞新进程 bind，重启秒起（此前要等 TIME_WAIT 自然过期 60s+，
    # systemd 每 5s 拉起一次、失败一次，实测 35 靶机超 90s 才恢复）。
    # daemon_threads=True —— 处理线程随主进程退出，不 pin 住旧进程
    #（与 http_file.py :9001 / socket_listen.py 既有写法对齐）。
    class ThreadingTCPServer4(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server4 = ThreadingTCPServer4(('0.0.0.0', port), handler)
    server6 = None

    logger.info(f"监听 0.0.0.0:{port} (IPv4)")

    try:
        class ThreadingTCPServer6(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET6
            allow_reuse_address = True
            daemon_threads = True
            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        server6 = ThreadingTCPServer6(('::', port), handler)
        logger.info(f"监听 [::]:{port} (IPv6)")
    except Exception as e:
        logger.warning(f"IPv6 监听启动失败: {e}，降级为仅 IPv4 模式")
        server6 = None

    t4 = threading.Thread(target=server4.serve_forever, daemon=True)
    t4.start()

    if server6:
        try:
            server6.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务停止")
            server4.shutdown()
            server6.shutdown()
    else:
        try:
            server4.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务停止")
            server4.shutdown()
