"""HTTP 文件服务:靶机侧 :9001,GET 下载 / POST multipart 上传。

spec: docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md
- 独立旁路服务,不接 TCP client tracking(不影响 autoupgrade idle 判定)
- 路径 realpath 归一化,全盘可访问(与 TCP 9000 文件接口同级安全模型,仅内网)
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

logger = logging.getLogger("socket_server")

DEFAULT_HTTP_PORT = 9001


def parse_multipart(content_type: str, body: bytes) -> tuple:
    """解析 multipart/form-data,返回 (path, file_content)。

    手切实现(Python 3.8 http.server 无内建 multipart 解析;email.parser 对
    二进制含 boundary 子串的对抗输入不如显式分隔行切分可靠):
    - 只认 "\r\n--boundary\r\n" 与 "\r\n--boundary--" 作为分隔(行首匹配),
      文件内容中的 boundary 子串(非行首、非完整分隔行)不影响切分。
    - 缺 path/file 字段或 boundary 抛 ValueError。
    """
    m = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            m = part[len("boundary="):].strip().strip('"').encode()
            break
    if not m:
        raise ValueError("multipart boundary missing")
    delim = b"\r\n--" + m          # 分隔行前缀(行首)
    final = delim + b"--\r\n"       # 结束分隔

    # 按分隔行切段:body 以 --boundary\r\n 开头,段间以 \r\n--boundary 分隔
    # 首段前无 \r\n,补一个使切分均匀(否则 split 后 segments[0] 即首字段、
    # segments[1:] 丢首字段——见 task-2-report.md 偏离说明)
    if not body.startswith(b"--" + m):
        raise ValueError("multipart body malformed")
    segments = (b"\r\n" + body).split(delim)
    # segments[0] 是补上的 "\r\n" 切出的空串,丢弃
    path_val = None
    file_content = None
    for seg in segments[1:]:
        if seg.startswith(b"--"):
            break  # 结束分隔
        seg = seg.lstrip(b"\r\n")
        # 每段: headers \r\n\r\n value
        header_end = seg.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = seg[:header_end].decode("utf-8", "replace")
        value = seg[header_end + 4:]
        # 去掉段尾的 \r\n(分隔行前的回车换行)
        if value.endswith(b"\r\n"):
            value = value[:-2]
        if 'name="path"' in headers:
            path_val = value.decode("utf-8", "replace")
        elif 'name="file"' in headers:
            file_content = value
    if path_val is None or file_content is None:
        raise ValueError("multipart missing path or file field")
    return path_val, file_content


def read_http_port(config_path: str = "/opt/socket/config") -> int:
    """读 config 的 http_port= 行;缺省 9001;显式 0 = 关闭。

    读取模式同 upgrader._read_proxy / autoupgrade._read_autoupgrade_config 先例。
    注意:config 里历史遗留的 port=9000 行未被 serve 接线,勿参照本函数以外的方式读它。
    """
    try:
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("http_port="):
                        return int(line.split("=", 1)[1].strip())
    except Exception as e:
        logger.warning(f"读 http_port 配置失败({e}),用默认 {DEFAULT_HTTP_PORT}")
    return DEFAULT_HTTP_PORT


def _normalize(path: str) -> str:
    """路径归一化:realpath 展开符号链接与 ../,统一绝对路径。

    全盘可访问(spec §1.4,与 TCP 9000 文件接口同级安全模型);穿越用例
    (/home/tmp/../../etc/passwd)归一化后即 /etc/passwd,行为可测。
    """
    return os.path.realpath(path)


class FileHandler(BaseHTTPRequestHandler):
    """GET /files?path= 下载;POST /files multipart 上传。"""

    server_version = "socket_server-file/1.7"

    # 静默默认 access log(默认写 stderr 会污染 systemd 日志),复用 logging
    def log_message(self, fmt, *args):
        logger.info(f"http_file {self.client_address[0]} {fmt % args}")

    def do_GET(self):
        try:
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "",
                          keep_blank_values=True)
        except Exception:
            self._send_error_json(400, "bad query")
            return
        raw = (qs.get("path") or [""])[0]
        path = _normalize(unquote(raw))
        if not raw:
            self._send_error_json(400, "missing path parameter")
            return
        if not os.path.isfile(path):
            self._send_error_json(404, f"file not found: {path}")
            return
        try:
            with open(path, "rb") as f:
                content = f.read()
        except OSError as e:
            self._send_error_json(500, f"read failed: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        self.wfile.write(content)
        logger.info(f"http_file GET {path} {len(content)}B <- {self.client_address[0]}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            path_val, content = parse_multipart(
                self.headers.get("Content-Type") or "", body)
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        path = _normalize(path_val)
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        except OSError as e:
            self._send_error_json(500, f"write failed: {e}")
            return
        resp = json.dumps({"ok": True, "path": path, "size": len(content)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        logger.info(f"http_file POST {path} {len(content)}B <- {self.client_address[0]}")

    def _send_error_json(self, code: int, msg: str):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_http_file_server_for_test():
    """测试辅助:127.0.0.1 随机端口起 FileHandler,返回 (port, stop_fn)。"""
    s = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    return s.server_address[1], (lambda: (s.shutdown(), s.server_close()))


_servers = []  # 持有 ThreadingHTTPServer 引用,供测试与优雅关闭


def start_http_file_server(port: int) -> None:
    """双栈启动 HTTP 文件服务(非阻塞,daemon 线程),立即返回。

    与 server.py 的 TCP 双栈实现逐行同构:IPv4 (0.0.0.0) + IPv6 ([::])
    IPV6_V6ONLY=1;IPv6 起不来降级仅 IPv4;port=0 关闭;端口被占放弃并告警
    (无备用端口逻辑),不影响 TCP 主服务。
    """
    if not port:
        logger.info("http_port=0,HTTP 文件服务不启动")
        return

    class _V6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            super().server_bind()

    try:
        server4 = ThreadingHTTPServer(("0.0.0.0", port), FileHandler)
        server4.daemon_threads = True
        _servers.append(server4)
        threading.Thread(target=server4.serve_forever, daemon=True,
                         name="http-file-v4").start()
        logger.info(f"HTTP 文件服务监听 0.0.0.0:{port} (IPv4)")
    except OSError as e:
        logger.warning(f"HTTP 文件服务 IPv4 监听失败: {e},HTTP 文件服务不可用")
        return

    try:
        server6 = _V6(("::", port), FileHandler)
        server6.daemon_threads = True
        _servers.append(server6)
        threading.Thread(target=server6.serve_forever, daemon=True,
                         name="http-file-v6").start()
        logger.info(f"HTTP 文件服务监听 [::]:{port} (IPv6)")
    except OSError as e:
        logger.warning(f"HTTP 文件服务 IPv6 监听失败: {e},降级为仅 IPv4 模式")
