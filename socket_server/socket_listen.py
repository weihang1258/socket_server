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

    def _start_server(self, host, af):
        with socket.socket(af, socket.SOCK_STREAM) as s:
            if af == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, self.port))
            s.listen(5)
            logger.info(f"Listening on [{host}]:{self.port} (IPv{'6' if af == socket.AF_INET6 else '4'})...")

            while self._stop_flag:
                conn, addr = s.accept()
                try:
                    with conn:
                        logger.info(f"Connected by {addr}")
                        while self._stop_flag:
                            data = conn.recv(10240)
                            if not data:
                                time.sleep(0.5)
                                logger.error("无效数据循环2...")
                                break
                            self.q.put(data)
                except Exception as e:
                    logger.error(e)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                logger.error("无效数据循环3...")
                time.sleep(0.5)

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
