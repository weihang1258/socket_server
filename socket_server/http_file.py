"""HTTP 文件服务:靶机侧 :9001,GET 下载 / POST multipart 上传。

spec: docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md
- 独立旁路服务,不接 TCP client tracking(不影响 autoupgrade idle 判定)
- 路径 realpath 归一化,全盘可访问(与 TCP 9000 文件接口同级安全模型,仅内网)
- 1.8.2 起全程流式:上传边收边写 .part 临时文件、下载边读盘边发,
  内存占用恒定(约 1MB 级)与文件大小无关,支持 GB 级大文件不 OOM
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

logger = logging.getLogger("socket_server")

DEFAULT_HTTP_PORT = 9001


class _BodyReader:
    """Content-Length 限界内的流式 body 读取器。

    buf 常驻不超过 CHUNK + 分隔串长度,与传输的文件大小无关。
    """

    CHUNK = 256 * 1024

    def __init__(self, rfile, length):
        self.rfile = rfile
        self.remaining = length
        self.buf = b""

    def fill(self, need):
        """保证 buf 至少 need 字节(body 耗尽为止)。"""
        while len(self.buf) < need and self.remaining > 0:
            chunk = self.rfile.read(min(self.CHUNK, self.remaining))
            if not chunk:
                self.remaining = 0
                break
            self.buf += chunk
            self.remaining -= len(chunk)

    def take(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_headers(self, limit=16384):
        """读到 \\r\\n\\r\\n(字段头结束),返回头文本,buf 停在字段值起点。"""
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx != -1:
                out = self.buf[:idx]
                self.buf = self.buf[idx + 4:]
                return out
            if len(self.buf) > limit:
                raise ValueError("multipart part headers too large")
            before = len(self.buf)
            self.fill(before + 8192)
            if len(self.buf) == before:
                raise ValueError("multipart body truncated(headers)")

    def stream_value(self, delim, sink):
        """读字段值至分隔行(不含),内容经 sink 落地;完成后 buf 推进到其后。

        每轮保留尾部 len(delim)+1 字节不写,防分隔串跨 chunk 截断误写。
        分隔行歧义处理(RFC 2046):delim 命中后必须后随 "--"(结束)或
        "\\r\\n"(下一字段头)才算分隔行;如 "\\r\\n--ABC123x"(x 为内容)不切,
        从 delim 之后继续扫,内容不丢字节——旧整载实现在此处会截短文件。
        """
        keep = len(delim) + 1
        while True:
            idx = self.buf.find(delim)
            if idx != -1:
                tail = self.buf[idx + len(delim): idx + len(delim) + 2]
                if tail[:2] == b"--" or tail[:2] == b"\r\n" or tail[:1] == b"\n":
                    sink(self.buf[:idx])
                    self.buf = self.buf[idx + len(delim):]
                    return
                # 伪命中:是内容不是分隔行,写掉 delim 之前的字节继续扫
                if idx + len(delim) + 2 < len(self.buf):
                    sink(self.buf[:idx + len(delim)])
                    self.buf = self.buf[idx + len(delim):]
                    continue
                # 后续字节还没读进来,等 fill
            if len(self.buf) > keep:
                sink(self.buf[:-keep])
                self.buf = self.buf[-keep:]
            before = len(self.buf)
            self.fill(before + self.CHUNK)
            if len(self.buf) == before:
                raise ValueError("multipart body truncated(value)")


class _CapSink:
    """内存 sink 带上限(path 等小字段用),超限抛 ValueError。"""

    def __init__(self, cap):
        self.parts = []
        self.total = 0
        self.cap = cap

    def __call__(self, b):
        self.total += len(b)
        if self.total > self.cap:
            raise ValueError("field value too large")
        self.parts.append(b)

    def value(self):
        return b"".join(self.parts).decode("utf-8", "replace")


def _boundary_from_content_type(content_type: str):
    """从 Content-Type 提取 boundary(bytes),缺失或超长(>256)返回 None。"""
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            b = part[len("boundary="):].strip().strip('"')
            if b and len(b) <= 256:
                return b.encode()
    return None


def parse_multipart_stream(rfile, content_length, boundary, open_file):
    """流式解析 multipart/form-data(与 curl -F 输出同构):

      --B\\r\\n 头 \\r\\n\\r\\n 值 \\r\\n--B\\r\\n 头 \\r\\n\\r\\n 值 \\r\\n--B--\\r\\n

    文件内容经 open_file(path值或None) 返回的文件对象边收边写,不整载内存。
    path 字段晚于 file 字段到达时(非常规顺序)open_file 收到 None,调用方应
    落临时文件、解析结束后再搬运到目标路径。
    返回 {"path": str|None, "size": int, "filename": str|None, "file_seen": bool}。
    body 畸形/截断/字段超限抛 ValueError;已写数据由调用方负责清理。
    """
    br = _BodyReader(rfile, content_length)
    delim = b"\r\n--" + boundary
    br.fill(len(boundary) + 4)
    if not br.buf.startswith(b"--" + boundary):
        raise ValueError("multipart body malformed")
    br.take(len(boundary) + 2)

    path_val = None
    filename = None
    size = 0
    file_seen = False
    fh = None
    while True:
        br.fill(2)
        if not br.buf:
            raise ValueError("multipart body truncated(boundary)")
        if br.buf.startswith(b"--"):
            break  # --boundary-- 结束
        if not br.buf.startswith(b"\r\n"):
            raise ValueError("multipart body malformed")
        br.take(2)
        headers = br.read_headers().decode("utf-8", "replace")
        m = re.search(r'\bname="([^"]*)"', headers)
        name = m.group(1) if m else ""
        if name == "path":
            sink = _CapSink(4096)
            br.stream_value(delim, sink)
            path_val = sink.value()
        elif name == "file":
            m = re.search(r'filename="([^"]*)"', headers)
            if m:
                filename = m.group(1)
            if fh is None:
                fh = open_file(path_val)
            n = [0]

            def _sink(b, _n=n, _fh=fh):
                _n[0] += len(b)
                _fh.write(b)

            br.stream_value(delim, _sink)
            size = n[0]
            file_seen = True
        else:
            br.stream_value(delim, lambda b: None)  # 未知字段:跳过
    return {"path": path_val, "size": size, "filename": filename,
            "file_seen": file_seen}


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


def _discard_upload(up: dict) -> None:
    """失败清理:关句柄、删 .part 与临时文件。"""
    if up.get("fh") is not None:
        try:
            up["fh"].close()
        except OSError:
            pass
        up["fh"] = None
    for key in ("part", "temp"):
        p = up.get(key)
        if p:
            try:
                os.unlink(p)
            except OSError:
                pass
            up[key] = None


class FileHandler(BaseHTTPRequestHandler):
    """GET /files?path= 流式下载;POST /files multipart 流式上传。"""

    server_version = "socket_server-file/1.8.2"

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
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError as e:
            self._send_error_json(500, f"read failed: {e}")
            return
        # 流式发送:1MB 分块边读边写,Content-Length 锁死发送上限(文件中途
        # 变化时宁短勿超);客户端中断按 info 记,不出 traceback 污染日志
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        sent = 0
        try:
            while sent < size:
                chunk = f.read(min(1024 * 1024, size - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            logger.info(f"http_file GET {path} 客户端中断于 {sent}/{size}B "
                        f"<- {self.client_address[0]}")
        except OSError as e:
            logger.error(f"http_file GET {path} 发送失败({e}) <- {self.client_address[0]}")
        finally:
            f.close()
        logger.info(f"http_file GET {path} {sent}B <- {self.client_address[0]}")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error_json(400, "bad content-length")
            return
        if length <= 0:
            self._send_error_json(400, "empty body")
            return
        boundary = _boundary_from_content_type(self.headers.get("Content-Type") or "")
        if not boundary:
            self._send_error_json(400, "multipart boundary missing")
            return
        # 落盘策略:path 字段先到(文档用法 curl -F "path=..." -F "file=@...")
        # 则直写目标目录 <path>.part,收完 os.replace 原子顶替(同盘零拷贝);
        # file 字段先到则落系统临时文件,收完 shutil.move 搬到目标(跨盘兜底)
        up = {"fh": None, "part": None, "temp": None, "dest": None}

        def open_file(path_val):
            if up["fh"] is not None:
                return up["fh"]  # 多 file 字段:续用首个落盘
            if path_val:
                dest = _normalize(path_val)
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                part = dest + ".part"
                up["fh"] = open(part, "wb")
                up["part"] = part
                up["dest"] = dest
            else:
                fd, tmp = tempfile.mkstemp(prefix="socket_server-upload-")
                up["fh"] = os.fdopen(fd, "wb")
                up["temp"] = tmp
            return up["fh"]

        try:
            result = parse_multipart_stream(self.rfile, length, boundary, open_file)
        except ValueError as e:
            _discard_upload(up)
            self._send_error_json(400, str(e))
            return
        except OSError as e:
            _discard_upload(up)
            try:
                self._send_error_json(500, f"write failed: {e}")
            except OSError:
                pass  # 客户端已断开
            return
        finally:
            if up["fh"] is not None:
                try:
                    up["fh"].close()
                except OSError:
                    pass
                up["fh"] = None
        if not result["path"]:
            _discard_upload(up)
            self._send_error_json(400, "multipart missing path or file field")
            return
        dest = _normalize(result["path"])
        try:
            if up["temp"] is not None:
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.move(up["temp"], dest)
                up["temp"] = None
            else:
                os.replace(up["part"], dest)
                up["part"] = None
        except OSError as e:
            _discard_upload(up)
            self._send_error_json(500, f"write failed: {e}")
            return
        size = result["size"]
        resp = json.dumps({"ok": True, "path": dest, "size": size}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        logger.info(f"http_file POST {dest} {size}B <- {self.client_address[0]}")

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
