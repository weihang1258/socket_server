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


class TrackedTCPHandler(socketserver.BaseRequestHandler):
    """带客户端连接计数的 TCP Handler 基类。

    子类需实现 on_data(datatype, data, **kwargs) -> bytes|None
    """

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


def start_tcp_server(port, handler_class):
    """启动 IPv4 + IPv6 双栈 TCP 服务器，阻塞运行。"""
    handler = handler_class

    server4 = socketserver.ThreadingTCPServer(('0.0.0.0', port), handler)
    server6 = None

    logger.info(f"监听 0.0.0.0:{port} (IPv4)")

    try:
        class ThreadingTCPServer6(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET6
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
