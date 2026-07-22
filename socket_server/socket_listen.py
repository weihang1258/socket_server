import os
import time
import socket
import logging
import multiprocessing
import threading

from .netutils import detect_ip_version

logger = logging.getLogger(__name__)


class SocketServerListen:
    # host=None 表示双栈模式，自动监听 0.0.0.0 + ::
    def __init__(self, host=None, port=30001):
        self.host = host
        self.port = port
        self.q = multiprocessing.Queue()
        self.data = b""
        self._stop_flag = True
        self._processes = []  # 支持多个监听进程（双栈场景）

    def _resolve_listeners(self):
        if self.host is None:
            return [
                ("0.0.0.0", socket.AF_INET),
                ("::",      socket.AF_INET6),
            ]
        version = detect_ip_version(self.host)
        af = socket.AF_INET if version == 4 else socket.AF_INET6
        return [(self.host, af)]

    def _handle_conn(self, conn, addr):
        """单连接接收循环，在独立线程中运行。

        并发 accept 后每个连接独占一个线程 recv，避免一个常驻连接
        阻塞后续连接（原迭代式 accept 的缺陷：accept 一个后即陷在该连接
        的 recv 循环里，直到它关闭才能接下一个）。所有连接的数据仍 put
        进同一个 self.q，下游 cachedata 维持合并流语义不变。
        """
        try:
            with conn:
                logger.info(f"Connected by {addr}")
                while self._stop_flag:
                    try:
                        data = conn.recv(10240)
                    except (ConnectionResetError, OSError) as e:
                        logger.info(f"连接 {addr} 异常断开: {e}")
                        break
                    if not data:  # 对端正常关闭
                        break
                    self.q.put(data)
        except Exception as e:
            logger.error(f"连接 {addr} 处理异常: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            logger.info(f"连接 {addr} 已断开")

    def _start_server(self, host, af):
        with socket.socket(af, socket.SOCK_STREAM) as s:
            if af == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, self.port))
            s.listen(128)
            logger.info(f"Listening on [{host}]:{self.port} (IPv{'6' if af == socket.AF_INET6 else '4'})...")

            while self._stop_flag:
                try:
                    conn, addr = s.accept()
                except OSError as e:
                    if not self._stop_flag:
                        break
                    logger.error(f"accept 异常: {e}")
                    time.sleep(0.5)
                    continue
                # 每连接一线程：accept 循环立即回到 accept 继续收下一个连接，
                # 不再被单个常驻连接的 recv 阻塞。
                t = threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True)
                t.start()

    def start_server(self):
        try:
            listeners = self._resolve_listeners()
            for host, af in listeners:
                p = multiprocessing.Process(
                    target=self._start_server,
                    args=(host, af),
                    daemon=True,
                )
                p.start()
                self._processes.append(p)
            self.getdataprocess = threading.Thread(target=self.cachedata, daemon=True)
            self.getdataprocess.start()
            time.sleep(5)
            pids = self.pid()
            if pids:
                return True
        except Exception as e:
            logger.error(e)

    def stop_server(self):
        self._stop_flag = False
        for p in self._processes:
            if p.is_alive():
                p.terminate()
                p.join(timeout=3)
                if p.is_alive():
                    p.kill()
        self._processes.clear()
        pid_str = self.pid()
        if pid_str:
            for pid in pid_str.split():
                os.system(f"kill -9 {pid}")
            return True

    def pid(self):
        pid = os.popen(
            "ss -lptn 'sport = :%s'|grep users|awk -F ',' '{print $2}'|awk -F '=' '{print $2}'" % self.port
        ).read().strip()
        return pid

    def cleandata(self):
        while self.q.qsize():
            time.sleep(1)
        time.sleep(1)
        self.data = b""
        os.system("rm -rf /tmp/socketbin/*")

    def cachedata(self):
        if not os.path.isdir("/tmp/socketbin"):
            os.makedirs("/tmp/socketbin")
        else:
            os.system("rm -rf /tmp/socketbin/*")
        datawrite = b""
        while True:
            if self.q.qsize():
                try:
                    tmp_q_get = self.q.get(timeout=1)
                except Exception:
                    continue
                datawrite += tmp_q_get
                self.data += tmp_q_get
            else:
                if datawrite:
                    with open(f"/tmp/socketbin/{int(time.time() * 1000000)}.bin", "wb") as f:
                        f.write(datawrite)
                    datawrite = b""
                time.sleep(1)

    def data_writefile(self, file):
        with open(file, "wb") as f:
            f.write(self.data)
