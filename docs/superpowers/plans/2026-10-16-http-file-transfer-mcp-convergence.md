# HTTP 文件传输 + MCP 工具收敛 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** socket_server 新增 HTTP 文件服务（:9001 GET 下载 / POST multipart 上传），mcp_socket_server 工具从 17 收敛到 12（删 7 增 2：`replay_run`、`socket_listen`），文件传输改由 LLM 直连靶机 HTTP。

**Architecture:** HTTP 文件服务是 socket_server 的旁路新模块（`http_file.py`，daemon 线程，双栈 :9001），TCP :9000 协议零改动。MCP 侧删 7 个低价值/被替代工具、新增 2 个编排工具（replay_run→datatype 0；socket_listen→171-174 四动作编排），SYSTEM 锁从 stub 变 per-target 排他实现。

**Tech Stack:** Python 3.8 stdlib `http.server.ThreadingHTTPServer`（socket_server 侧，已实测 3.8.10 可导入）；FastMCP/pydantic（MCP 侧）。

**Spec:** `docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md`（七轮 review 后定稿）

## Global Constraints

- Python 3.8.10 兼容（socket_server 打包环境；禁用 3.9+ 语法如 `dict |`、`str.removeprefix`）
- 禁止 shell=True；子进程一律 list args + shell=False（socket_server 既有铁律）
- TCP 协议（datatype 编号、帧格式、21-24 握手）零改动
- 所有新 MCP 工具 schema 必须含统一可选参数 `description`（调用目的透传）
- `/opt/socket/config` 的 `port=9000` 是历史未接线配置——实现中**不得参照、不得改动**
- 两仓独立提交：socket_server 在 `/opt/socket_server`，mcp_socket_server 在 `/opt/mcp_socket_server`
- 测试命令：socket_server 侧 `python3 -m pytest`（本机 3.8）；MCP 侧 `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q`

---

## Part A：socket_server 1.7.0（HTTP 文件服务）

### Task A1: http_file.py 模块骨架 + 配置读取

**Files:**
- Create: `/opt/socket_server/socket_server/http_file.py`
- Create: `/opt/socket_server/tests/test_http_file.py`（目录不存在则创建，含空 `__init__.py` 不需要——pytest 无 conftest 依赖直接跑）

**Interfaces:**
- Produces: `read_http_port() -> int`（默认 9001，http_port=0 返回 0）；`start_http_file_server(port: int) -> None`（阻塞，内部双栈 ThreadingHTTPServer；端口被占或 0 时直接返回并告警）；`FileHandler`（BaseHTTPRequestHandler 子类，含 `do_GET`/`do_POST`，Task A2/A3 实现）

- [ ] **Step 1: 写失败测试——配置读取**

```python
# /opt/socket_server/tests/test_http_file.py
"""http_file 模块单元测试(纯函数级,不起真实服务器的部分)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from socket_server.http_file import read_http_port


def test_read_http_port_default_when_no_config():
    # 不存在的 config 路径 -> 默认 9001
    assert read_http_port("/nonexistent/config") == 9001


def test_read_http_port_explicit():
    with tempfile.NamedTemporaryFile("w", suffix=".config", delete=False) as f:
        f.write("port=9000\nhttp_port=9100\n")
        path = f.name
    try:
        assert read_http_port(path) == 9100
    finally:
        os.unlink(path)


def test_read_http_port_disabled():
    with tempfile.NamedTemporaryFile("w", suffix=".config", delete=False) as f:
        f.write("http_port=0\n")
        path = f.name
    try:
        assert read_http_port(path) == 0
    finally:
        os.unlink(path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'socket_server.http_file'`）

- [ ] **Step 3: 最小实现 read_http_port**

```python
# /opt/socket_server/socket_server/http_file.py
"""HTTP 文件服务:靶机侧 :9001,GET 下载 / POST multipart 上传。

spec: docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md
- 独立旁路服务,不接 TCP client tracking(不影响 autoupgrade idle 判定)
- 路径 realpath 归一化,全盘可访问(与 TCP 9000 文件接口同级安全模型,仅内网)
"""
from __future__ import annotations

import logging
import os
import threading
from urllib.parse import parse_qs, unquote

logger = logging.getLogger("socket_server")

DEFAULT_HTTP_PORT = 9001


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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /opt/socket_server
git add socket_server/http_file.py tests/test_http_file.py
git commit -m "feat: http_file 配置读取 http_port= (默认9001,0=关闭)"
```

---

### Task A2: GET 下载 + POST 上传 handler

**Files:**
- Modify: `/opt/socket_server/socket_server/http_file.py`（追加 FileHandler）
- Modify: `/opt/socket_server/tests/test_http_file.py`（追加测试）

**Interfaces:**
- Produces: `FileHandler`——GET `/files?path=X` → 200 文件流（`Content-Type: application/octet-stream`、`Content-Disposition: attachment; filename="<basename>"`）/ 404 文件不存在 / 400 缺 path；POST `/files`（multipart，字段 `path`=落盘绝对路径文本、`file`=二进制）→ 200 `{"ok": true, "path": "...", "size": N}` / 400 缺字段 / 500 写盘失败；`parse_multipart(content_type: str, body: bytes) -> tuple[str, bytes]`（返回 (path, file_content)，供 handler 与测试直接调用）

- [ ] **Step 1: 写失败测试——multipart 解析（含对抗用例）**

追加到 `tests/test_http_file.py`：

```python
from socket_server.http_file import parse_multipart


def _multipart_body(path_val: str, filename: str, content: bytes, boundary: bytes) -> bytes:
    """构造标准 multipart/form-data body(与 curl -F 输出同构)。"""
    parts = []
    parts.append(
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="path"\r\n\r\n' +
        path_val.encode() + b"\r\n"
    )
    parts.append(
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n" +
        content + b"\r\n"
    )
    parts.append(b"--" + boundary + b"--\r\n")
    return b"".join(parts)


def test_parse_multipart_basic():
    body = _multipart_body("/home/tmp/a.pcap", "a.pcap", b"\x00\x01\x02DATA", b"XBOUNDRY")
    ct = 'multipart/form-data; boundary=XBOUNDRY'
    path, content = parse_multipart(ct, body)
    assert path == "/home/tmp/a.pcap"
    assert content == b"\x00\x01\x02DATA"


def test_parse_multipart_binary_contains_boundary_substring():
    # 对抗用例:文件内容里恰好含有 boundary 串的子序列——按"分隔行"切分不受影响
    boundary = b"ABC123"
    tricky = b"xABC123x" + bytes(range(256)) + b"ABC123tail"
    body = _multipart_body("/tmp/t", "t.bin", tricky, boundary)
    path, content = parse_multipart(f"multipart/form-data; boundary=ABC123", body)
    assert path == "/tmp/t"
    assert content == tricky


def test_parse_multipart_missing_path_field():
    boundary = b"B1"
    body = (
        b"--B1\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f"\r\n\r\n'
        b"abc\r\n--B1--\r\n"
    )
    try:
        parse_multipart("multipart/form-data; boundary=B1", body)
        assert False, "should raise"
    except ValueError:
        pass


def test_parse_multipart_missing_boundary():
    try:
        parse_multipart("multipart/form-data", b"xx")
        assert False, "should raise"
    except ValueError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 新增 4 个 FAIL（ImportError: parse_multipart）

- [ ] **Step 3: 实现 parse_multipart（手切 boundary，按 CRLF 分隔行切）**

追加到 `http_file.py`：

```python
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
    if not body.startswith(b"--" + m):
        raise ValueError("multipart body malformed")
    segments = body.split(delim)
    # segments[0] 是 "--boundary" 前的空段(或首段前的空串),丢弃
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 7 passed

- [ ] **Step 5: 写失败测试——FileHandler GET/POST（起真实测试服务器）**

追加到 `tests/test_http_file.py`：

```python
import json
import threading
import urllib.request
from socket_server.http_file import start_http_file_server_for_test


def _get_free_http_server(tmpdir):
    """在随机端口起 http_file 服务,返回 (base_url, stop_fn)。"""
    srv_holder = {}

    class _Srv:
        pass

    import socket
    from http.server import ThreadingHTTPServer

    from socket_server import http_file

    s4 = ThreadingHTTPServer(("127.0.0.1", 0), http_file.FileHandler)
    port = s4.server_address[1]
    t = threading.Thread(target=s4.serve_forever, daemon=True)
    t.start()

    def stop():
        s4.shutdown()
        s4.server_close()

    return f"http://127.0.0.1:{port}", stop


def test_get_download_roundtrip(tmpdir):
    src = tmpdir.join("dl.bin")
    src.write_binary(b"\x00PACKETDATA\xff" * 100)
    base, stop = _get_free_http_server(tmpdir)
    try:
        url = base + "/files?path=" + urllib.request.quote(str(src))
        with urllib.request.urlopen(url) as resp:
            body = resp.read()
            assert resp.headers.get("Content-Type") == "application/octet-stream"
        assert body == src.read_binary()
    finally:
        stop()


def test_get_download_404(tmpdir):
    base, stop = _get_free_http_server(tmpdir)
    try:
        try:
            urllib.request.urlopen(base + "/files?path=/nonexistent/file.bin")
            assert False, "should 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        stop()


def test_get_download_400_missing_path(tmpdir):
    base, stop = _get_free_http_server(tmpdir)
    try:
        try:
            urllib.request.urlopen(base + "/files")
            assert False, "should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        stop()


def test_post_upload_roundtrip(tmpdir):
    dst = str(tmpdir.join("up") + "/data.bin")  # 父目录不存在,应自动 makedirs
    payload = b"UPLOAD-CONTENT-\x01\x02"
    body = _multipart_body(dst, "data.bin", payload, b"TBOUND")
    base, stop = _get_free_http_server(tmpdir)
    try:
        req = urllib.request.Request(
            base + "/files", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=TBOUND"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        assert result["ok"] is True
        assert result["size"] == len(payload)
        with open(dst, "rb") as f:
            assert f.read() == payload
    finally:
        stop()


def test_post_upload_400_missing_field(tmpdir):
    base, stop = _get_free_http_server(tmpdir)
    try:
        req = urllib.request.Request(
            base + "/files", data=b"--B\r\n--B--\r\n",
            headers={"Content-Type": "multipart/form-data; boundary=B"},
            method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        stop()
```

- [ ] **Step 6: 跑测试确认失败**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 新增 5 个 FAIL（ImportError: FileHandler / start_http_file_server_for_test）

- [ ] **Step 7: 实现 FileHandler + do_GET/do_POST + 路径归一化**

追加到 `http_file.py`：

```python
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
```

补 import（文件顶部）：`import json`

- [ ] **Step 8: 追加 start_http_file_server_for_test（测试辅助,随机端口 IPv4）**

```python
def start_http_file_server_for_test():
    """测试辅助:127.0.0.1 随机端口起 FileHandler,返回 (port, stop_fn)。"""
    s = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    return s.server_address[1], (lambda: (s.shutdown(), s.server_close()))
```

同时把 Step 5 测试里 `_get_free_http_server` 简化为直接用该辅助（保留原签名包装即可，或替换实现）：

```python
def _get_free_http_server(tmpdir):
    port, stop = start_http_file_server_for_test()
    return f"http://127.0.0.1:{port}", stop
```

- [ ] **Step 9: 跑全量测试确认通过**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 12 passed

- [ ] **Step 10: Commit**

```bash
cd /opt/socket_server
git add socket_server/http_file.py tests/test_http_file.py
git commit -m "feat: http_file GET下载/POST multipart上传,realpath归一化+对抗用例"
```

---

### Task A3: 双栈启动 + serve 接线

**Files:**
- Modify: `/opt/socket_server/socket_server/http_file.py`（追加 start_http_file_server）
- Modify: `/opt/socket_server/socket_server/cli.py:8-43`（cmd_serve 插入 HTTP 启动，先于 start_tcp_server）
- Modify: `/opt/socket_server/tests/test_http_file.py`（追加双栈启动测试）

**Interfaces:**
- Consumes: `read_http_port()`（Task A1）、`FileHandler`（Task A2）
- Produces: `start_http_file_server(port: int) -> None`——非阻塞：IPv4 daemon 线程 + IPv6 daemon 线程（同构 server.py 的双栈；IPv6 失败降级仅 IPv4；port=0 或全失败只告警返回）。`cmd_serve` 在 `start_tcp_server` **之前**调用它（spec §1.1：TCP 主线程阻塞永不返回，HTTP 必须先就绪）

- [ ] **Step 1: 写失败测试——双栈启动函数**

追加到 `tests/test_http_file.py`：

```python
def test_start_http_file_server_dual_stack(tmpdir):
    from socket_server.http_file import start_http_file_server
    # 随机端口:用 0 会读 config 默认 9001,这里直测显式端口
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    start_http_file_server(port)  # 应双栈起服,立即返回(内部 daemon 线程)
    # IPv4 可连
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/files") as resp:
        assert resp.status in (200, 400)  # 无 path 参数 -> 400 也证明服务活着
    # IPv6 可连(本机支持 IPv6 时)
    try:
        with urllib.request.urlopen(f"http://[::1]:{port}/files") as resp:
            assert resp.status in (200, 400)
        ipv6_ok = True
    except Exception:
        ipv6_ok = False  # 容器无 IPv6 时允许,降级路径
    # 停服:直接探进程内 server 列表
    from socket_server import http_file as hf
    for srv in hf._servers:
        srv.shutdown()
        srv.server_close()
    hf._servers.clear()
    # 端口 0 = 不启动
    start_http_file_server(0)  # 应安静返回
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v -k dual_stack`
Expected: FAIL（ImportError: start_http_file_server）

- [ ] **Step 3: 实现 start_http_file_server（双栈,同构 server.py）**

追加到 `http_file.py`：

```python
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
```

补 import：`import socket`

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 13 passed

- [ ] **Step 5: cmd_serve 接线（HTTP 先于 TCP）**

修改 `cli.py` 的 `cmd_serve`，在 `start_tcp_server(args.port, ServiceHandler)` 之前插入：

```python
    # 启动 HTTP 文件服务(:9001,spec: docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md)
    # 必须先于 start_tcp_server:TCP 主线程 serve_forever 阻塞永不返回
    try:
        from .http_file import read_http_port, start_http_file_server
        http_port = read_http_port()
        if http_port:
            start_http_file_server(http_port)
    except Exception as e:
        logger = logging.getLogger("socket_server")
        logger.warning(f"HTTP 文件服务启动失败（不影响 TCP 主服务）: {e}")
```

- [ ] **Step 6: 本机端到端验证（spec §5：真实 serve + curl 全流程）**

```bash
cd /opt/socket_server
python3 -m socket_server.cli serve -p 19000 &   # 避开生产 9000,SERVE_PID 记录
sleep 2
# 下载 roundtrip
echo -n "HTTPFILE-E2E-测试内容" > /tmp/http_e2e_src.bin
curl -sf -o /tmp/http_e2e_dl.bin "http://127.0.0.1:9001/files?path=/tmp/http_e2e_src.bin"
cmp /tmp/http_e2e_src.bin /tmp/http_e2e_dl.bin && echo "GET OK"
# 上传 roundtrip(父目录自动创建)
curl -sf -F "path=/tmp/http_e2e_dir/up.bin" -F "file=@/tmp/http_e2e_src.bin" "http://127.0.0.1:9001/files"
cmp /tmp/http_e2e_src.bin /tmp/http_e2e_dir/up.bin && echo "POST OK"
# sha256 比对(spec §5)
sha256sum /tmp/http_e2e_src.bin /tmp/http_e2e_dir/up.bin
# IPv6
curl -sf -o /dev/null "http://[::1]:9001/files?path=/tmp/http_e2e_src.bin" && echo "IPv6 OK"
# TCP 19000 照常(spec: HTTP 不影响 TCP)
python3 -c "
import socket, struct, json
s = socket.create_connection(('127.0.0.1', 19000), timeout=5)
body = struct.pack('i', 14)
s.sendall(struct.pack('i', len(body)) + body)
n = struct.unpack('i', s.recv(4))[0]
import time; time.sleep(0.5)
print('TCP version resp OK:', n > 0)"
kill %1 2>/dev/null; rm -rf /tmp/http_e2e_*
```

Expected: GET OK / POST OK / 两行相同 sha256 / IPv6 OK / TCP version resp OK
（注意：本机 `/opt/socket/config` 若有 `http_port=9001` 则 serve 即监听 9001；无此行默认也是 9001）

- [ ] **Step 7: Commit**

```bash
cd /opt/socket_server
git add socket_server/http_file.py socket_server/cli.py tests/test_http_file.py
git commit -m "feat: http_file 双栈启动 + serve 接线(HTTP先于TCP)"
```

---

### Task A4: 打包接线 + 文档同步

**Files:**
- Modify: `/opt/socket_server/packaging/socket_server.spec`（hiddenimports 加一行）
- Modify: `/opt/socket_server/docs/api-guide.md`
- Modify: `/opt/socket_server/docs/mcp-integration.md`
- Modify: `/opt/socket_server/CHANGELOG.md`（1.7.0 段，版本号本身留到发版 Task A6）
- Modify: `/opt/socket_server/socket_server/version.py`（`VERSION = "1.7.0"`）

**Interfaces:** 无代码接口；本任务产出可打包、文档齐的仓库状态。

- [ ] **Step 1: spec hiddenimports 追加**

在 `packaging/socket_server.spec` 的 `hiddenimports` 列表 `'socket_server.autoupgrade',` 之后加：

```python
        'socket_server.http_file',
```

- [ ] **Step 2: 版本号 1.7.0**

`socket_server/version.py`：`VERSION = "1.7.0"`

- [ ] **Step 3: CHANGELOG.md 顶部加 1.7.0 段**

```markdown
## [1.7.0] - 2026-10-16

兼容 DPI/MCP 版本：mcp_socket_server 0.2.0

### 新增
- HTTP 文件服务（`http_port=9001`，config 可配，0=关闭）：GET `/files?path=` 下载、POST `/files` multipart 上传，双栈监听，TCP 协议零改动。供 LLM 直连靶机上传/下载文件（MCP file_upload/download 工具由 0.2.0 起移除）。

### 依赖
- 零第三方新依赖（http.server 标准库；二进制增量 <100KB）
```

- [ ] **Step 4: docs/api-guide.md 补 HTTP 文件服务节**

在文件合适位置（datatype 表之后）加：

```markdown
## HTTP 文件服务（1.7.0+）

与 TCP :9000 并行的旁路 HTTP 服务，默认 :9001（`/opt/socket/config` 的 `http_port=` 可改，0=关闭）。

| 方法 | 路径 | 参数 | 成功 | 失败 |
|------|------|------|------|------|
| GET | `/files?path=<绝对路径>` | query: path | 200 文件流（octet-stream, attachment） | 400 缺 path / 404 不存在 |
| POST | `/files` | multipart: `path`(文本) + `file`(二进制) | 200 `{"ok":true,"path":...,"size":N}` | 400 缺字段 / 500 写盘失败 |

curl 示例：
```bash
curl -o out.pcap "http://<host>:9001/files?path=/home/tmp/tmp.pcap"
curl -F "path=/home/tmp/deploy.zip" -F "file=@deploy.zip" http://<host>:9001/files
```
```

- [ ] **Step 5: docs/mcp-integration.md 补边界说明**

§2 项目边界表后加一段：

```markdown
> 1.7.0 起 socket_server 内置 HTTP 文件服务（:9001），MCP 层的 file_upload/file_download 工具已在 mcp_socket_server 0.2.0 移除，LLM 经工具描述中的 curl 示例直连靶机 HTTP 传输文件。
```

datatype 表 `file_upload/download` 相关行（若有）加注：`MCP 工具已删（0.2.0），改用 HTTP :9001`。

- [ ] **Step 6: release notes 重新生成 + 编译验证**

```bash
cd /opt/socket_server
python3 packaging/generate_release_notes.py
python3 -m py_compile socket_server/http_file.py socket_server/cli.py
python3 -m pytest tests/test_http_file.py -q
```

Expected: 全部通过

- [ ] **Step 7: Commit**

```bash
cd /opt/socket_server
git add packaging/socket_server.spec socket_server/version.py CHANGELOG.md docs/api-guide.md docs/mcp-integration.md socket_server/release_notes.py
git commit -m "chore: 1.7.0 打包接线+文档同步(api-guide/mcp-integration HTTP文件服务)"
```

---

### Task A5: 真实靶机端到端验证（发版前）

**Files:** 无代码改动；验证 + 修复发现的问题。

- [ ] **Step 1: 打包 1.7.0 二进制**

按 CLAUDE.md 发版流程第 4 步（CentOS 7 builder docker 命令），确认 exit 0、`./dist/socket_server current` 显示 1.7.0。

- [ ] **Step 2: 本机 serve 新二进制 + curl 全流程**

```bash
pkill -f "dist/socket_server serve" 2>/dev/null; sleep 1
./dist/socket_server serve &   # 读 /opt/socket/config http_port(默认9001)
sleep 2
curl -sf -o /tmp/rt.bin "http://127.0.0.1:9001/files?path=/opt/socket/config" && head -c 40 /tmp/rt.bin && echo && echo "GET OK"
echo "pack-test" > /tmp/pack_up.txt
curl -sf -F "path=/tmp/pack_up2.txt" -F "file=@/tmp/pack_up.txt" "http://127.0.0.1:9001/files" && cat /tmp/pack_up2.txt
pkill -f "dist/socket_server serve"; rm -f /tmp/rt.bin /tmp/pack_up*.txt
```

Expected: GET OK + pack-test 回显

- [ ] **Step 3: 靶机 10.12.131.82 手动部署验证**

```bash
# scp 二进制到靶机后(走既有运维路径),靶机上:
/opt/socket/versions/current/socket_server stop 2>/dev/null
# 按既有版本目录约定放置 1.7.0 并 switch,然后:
systemctl restart socket_server && sleep 2
curl -sf -o /tmp/t.bin "http://127.0.0.1:9001/files?path=/etc/hostname" && echo "target GET OK"
```

从中央节点侧验证跨机连通：`curl -sf -o /dev/null "http://10.12.131.82:9001/files?path=/etc/hostname" && echo cross-host OK`

Expected: 均 OK。发现问题回 Task A1-A3 修复，重跑本任务。

- [ ] **Step 4: 记录验证结果，发版（Task A6）前置条件达成**

---

### Task A6: socket_server 1.7.0 发版（按 CLAUDE.md 标准流程）

- [ ] **Step 1: 提交 + tag + push**

```bash
cd /opt/socket_server
git push origin main && git tag -a v1.7.0 -m "1.7.0: HTTP 文件服务(:9001 双栈 GET/POST multipart)" && git push origin v1.7.0
```

（若此前 Task A1-A4 已各自提交，此步只推 tag；确认 `git status` 干净）

- [ ] **Step 2: sha256 + gh release create**

按 CLAUDE.md 发版流程第 5-6 步执行（含"分类器拒绝时"预案：连续 2-3 次被挡则请用户在真实终端手动跑 `gh release create`，去掉开头 `!`）。

- [ ] **Step 3: 验证 Release**

`gh release view v1.7.0 --json tagName,url,assets`——assets 含 `socket_server` + `socket_server.sha256`。

---

## Part B：mcp_socket_server 0.2.0（工具收敛 17→12）

### Task B1: locks.py SYSTEM 去 stub（per-target 排他）+ commands.py 注册 REPLAY

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/locks.py:118-125`（LockManager.acquire 的 SYSTEM 分支）
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/commands.py`（LockClass 加 REPLAY）
- Modify: `/opt/mcp_socket_server/tests/test_locks.py`

**Interfaces:**
- Produces: `LockClass.REPLAY`（新枚举值 `"replay"`）；`LockManager.acquire(target, LockClass.SYSTEM)` 不再 raise，走 `_get_target_lock().acquire()` 的 per-target 排他路径（与 FILE_IO/SHELL/BROWSER 同通道）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_locks.py`（沿用该文件既有 fixture/风格；若该文件无 fixture 则直接实例化 LockManager）：

```python
def test_system_lock_per_target_exclusive():
    from mcp_socket_server.locks import LockManager, LockConflict
    from mcp_socket_server.commands import LockClass
    mgr = LockManager()
    mgr.acquire("t1", LockClass.SYSTEM)      # 不再 raise(stub 移除)
    try:
        mgr.acquire("t1", LockClass.SHELL)
        assert False, "SYSTEM 持有期间 SHELL 应冲突"
    except LockConflict:
        pass
    mgr.acquire("t2", LockClass.SHELL)        # 不同靶机不受影响(per-target)
    mgr.release("t1", LockClass.SYSTEM)
    mgr.acquire("t1", LockClass.SHELL)        # 释放后可获取
    mgr.release("t1", LockClass.SHELL)
    mgr.release("t2", LockClass.SHELL)


def test_replay_lock_exclusive_with_capture():
    from mcp_socket_server.locks import LockManager, LockConflict
    from mcp_socket_server.commands import LockClass
    mgr = LockManager()
    mgr.acquire("t1", LockClass.REPLAY)
    try:
        mgr.acquire("t1", LockClass.CAPTURE, ("/tmp/a.pcap",))
        assert False, "REPLAY 持有期间 CAPTURE 应冲突"
    except LockConflict:
        pass
    mgr.release("t1", LockClass.REPLAY)
    mgr.acquire("t1", LockClass.CAPTURE, ("/tmp/a.pcap",))
    mgr.release("t1", LockClass.CAPTURE, ("/tmp/a.pcap",))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_locks.py -v -k "system_lock_per_target or replay_lock"`
Expected: FAIL（SYSTEM raise LockConflict "stub" / AttributeError: REPLAY）

- [ ] **Step 3: 实现——commands.py 加 REPLAY，locks.py 去 stub**

`commands.py` LockClass 枚举加：

```python
    REPLAY = "replay"      # pcap 回放,per-target 排他
```

`locks.py` LockManager.acquire 删除这两行：

```python
        if lock_class == LockClass.SYSTEM:
            raise LockConflict(target, lock_class, "SYSTEM 本期 stub")
```

（SYSTEM 与 REPLAY 均自然落入 `_get_target_lock().acquire()` → `_acquire_exclusive` 通用通道；模块顶部 docstring 的 "SYSTEM:全局排他(stub,本期不实现)" 同步改为 "SYSTEM/REPLAY:per-target 排他"）

- [ ] **Step 4: 跑全量锁测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_locks.py -q`
Expected: 全 passed（既有测试无 SYSTEM raise 依赖——如失败，检查既有测试是否断言了 stub 行为并按新语义修正）

- [ ] **Step 5: Commit**

```bash
cd /opt/mcp_socket_server
git add src/mcp_socket_server/locks.py src/mcp_socket_server/commands.py tests/test_locks.py
git commit -m "feat: SYSTEM 锁去 stub(per-target 排他) + 新增 REPLAY 锁类"
```

---

### Task B2: socket_client 新增 replay_run/socket_listen 方法，删 7 个废弃方法

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/socket_client.py`（加 2 方法；删 `version`/`routeinfo`/`command_exists`/`filesize`/`version_detail`/`file_upload`/`file_download`）
- Modify: `/opt/mcp_socket_server/tests/mock_socket_server.py`（补 datatype 0/171/173(file语义)/174(error形态) handler）
- Modify: `/opt/mcp_socket_server/tests/test_socket_client.py`

**Interfaces:**
- Produces:
  - `replay_run(pcaps: list[str], uplink_iface: str, downlink_iface: str, uplink_vlan=None, downlink_vlan=None, mbps: int = 0, force_ip_src=None, force_ip_dst=None, force_sport=None, force_dport=None) -> dict`（datatype 0，`recv_text_response` + json.loads——datatype 0 响应是无长度前缀 JSON，与 5/6 同型）
  - `socket_listen(action: str, listen_port: int = 30001, file: str = None) -> object`——start→171 返回 `{"pid":[...]}`（recv_text_response+json.loads）；clean→172 / save→173（`recv_ok()` 判 `b"ok"`）；fetch→174（`recv_file_response()` 取 `[8B len][gzip]` 并 `decompress_gzip`；**先探测响应形态**：收到的首 4 字节按 `[4B?]` 不适用——174 无 4B 前缀，直接 `[8B <Q]`；服务端 error 时发的是无前缀 JSON `{"error":...}`，实现为：先 `recv_inline_text(timeout=0.3)` 试探性收，若以 `{` 开头按 error JSON 处理，否则按 8B 长度前缀流处理。**实现细节见 Step 1 代码**）
- 删除方法清单（与 server.py 工具删除一一对应）：`version`(153)/`routeinfo`(168)/`command_exists`(173)/`filesize`(178)/`version_detail`(183)/`file_upload`(239)/`file_download`(269)。**保留** `isfile`/`isdir`/`pcap_flow_extract`/`cmd_exec`/`capture_start`/`capture_stop`/`boce_run`

- [ ] **Step 1: 写失败测试（新方法 + mock handler 扩展）**

`mock_socket_server.py` 的 `_on_data` 追加（注意 174 已有 happy-path handler，改造为可注入错误形态）：

```python
        # _Handler.setup 追加实例开关
        #   self.listen_mode = "ok"   # "ok"|"error"|"big"
        # MockSocketServer.__init__ 追加类级默认
        #   listen_mode = "ok"

        # _on_data 追加/修改:
        if datatype == 0:
            return json.dumps({"total_files": 1, "total_packets": 10,
                               "total_bytes": 100, "uplink_packets": 5,
                               "uplink_bytes": 50, "downlink_packets": 5,
                               "downlink_bytes": 50, "flow_count": 1,
                               "duration_seconds": 0.5, "target_mbps": 0,
                               "actual_mbps": 1.6}).encode()
        if datatype == 171:
            return json.dumps({"pid": ["12345"]}).encode()
        if datatype == 174:
            if self.listen_mode == "error":
                return json.dumps({"error": "未启动监听"}).encode()
            data = b"listener-data" * 100  # 1300B,超过测试 max_bytes=1000
            content = _compress(data)
            return struct.pack("<Q", len(content)) + content
```

`tests/test_socket_client.py` 追加：

```python
def test_replay_run_method(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        stats = c.replay_run(["/tmp/a.pcap"], "eth0", "eth1", mbps=10)
        assert stats["total_packets"] == 10
        assert stats["duration_seconds"] == 0.5
    finally:
        c.close()


def test_socket_listen_start_clean_save(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        r = c.socket_listen("start", listen_port=30001)
        assert r["pid"] == ["12345"]
        assert c.socket_listen("clean") == b"ok"
        assert c.socket_listen("save", file="/tmp/l.log") == b"ok"
    finally:
        c.close()


def test_socket_listen_fetch_ok_and_error(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        data = c.socket_listen("fetch", max_bytes=1000)
        assert data == b"listener-data" * 100  # 1300B 全量取回(截断是 server.py 层职责)
    finally:
        c.close()

    # error 形态
    mock_server.listen_mode = "error"
    c2 = SocketServerClient(mock_server.host, mock_server.port)
    c2.connect()
    try:
        try:
            c2.socket_listen("fetch")
            assert False, "should raise"
        except ConnectionError as e:
            assert "未启动监听" in str(e)
    finally:
        c2.close()
        mock_server.listen_mode = "ok"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_socket_client.py -v -k "replay_run_method or socket_listen"`
Expected: FAIL（AttributeError: replay_run / socket_listen）

- [ ] **Step 3: 实现 2 个新方法**

`socket_client.py` 追加（放在 `boce_run` 之后、`file_upload` 之前的位置均可）：

```python
    def replay_run(self, pcaps: list, uplink_iface: str, downlink_iface: str,
                   uplink_vlan=None, downlink_vlan=None, mbps: int = 0,
                   force_ip_src=None, force_ip_dst=None,
                   force_sport=None, force_dport=None) -> dict:
        """datatype 0:pcap 回放。payload 对齐 handlers.py do() 的 0 分支
        (必填 pcaps/uplink_iface/downlink_iface;PcapReplayer(**data) 其余透传)。
        响应无长度前缀 JSON(同 5/6 型),返回 stats dict。"""
        payload = {"pcaps": pcaps, "uplink_iface": uplink_iface,
                   "downlink_iface": downlink_iface, "mbps": mbps}
        if uplink_vlan is not None:
            payload["uplink_vlan"] = uplink_vlan
        if downlink_vlan is not None:
            payload["downlink_vlan"] = downlink_vlan
        if force_ip_src is not None:
            payload["force_ip_src"] = force_ip_src
        if force_ip_dst is not None:
            payload["force_ip_dst"] = force_ip_dst
        if force_sport is not None:
            payload["force_sport"] = force_sport
        if force_dport is not None:
            payload["force_dport"] = force_dport
        self._send(0, json.dumps(payload).encode())
        return json.loads(self.recv_text_response().decode("utf-8", "replace"))

    def socket_listen(self, action: str, listen_port: int = 30001,
                      file: str = None, max_bytes: int = 1048576) -> object:
        """socket 监听编排(datatype 171-174)。

        start->171 {"host":null,"port":listen_port} -> {"pid":[...]}(json)
        clean->172 / save->173 {"file":file} -> b"ok"(error 判失败)
        fetch->174 -> 全量明文 bytes(gzip 解包)。
        174 失败形态:未启动监听时服务端发 {"error":...} JSON(无 8B 前缀),
        先按首字节探测:非 8B 长度前缀可解析或解析出明文 JSON 时按错误处理。
        """
        if action == "start":
            self._send(171, json.dumps({"host": None, "port": listen_port}).encode())
            result = json.loads(self.recv_text_response().decode("utf-8", "replace"))
            if not result.get("pid"):
                raise ConnectionError(f"listen start failed: pid empty, resp={result}")
            return result
        if action == "clean":
            self._send(172)
            resp = self.recv_ok()
            if resp != b"ok":
                raise ConnectionError(f"listen clean failed: resp={resp!r}")
            return resp
        if action == "save":
            if not file:
                raise ValueError("save requires file")
            self._send(173, json.dumps({"file": file}).encode())
            resp = self.recv_ok()
            if resp != b"ok":
                raise ConnectionError(f"listen save failed: resp={resp!r}")
            return resp
        if action == "fetch":
            self._send(174)
            # 探测:先窥探首 8 字节,合法 <Q 长度前缀则走文件流;否则按 error JSON
            head = self._recv_n(8)
            candidate_len = struct.unpack("<Q", head)[0]
            # 合法性判据:gzip 明文长度不会超过 4GB,且 JSON error 首字节是 '{'(0x7b)
            if head[0:1] == b"{":
                # error JSON:把 head 连同后续一起读完按 JSON 解析
                rest = self._recv_until_timeout()
                raise ConnectionError(f"listen fetch failed: {(head + rest).decode('utf-8', 'replace')}")
            content = self._recv_n(candidate_len)
            return decompress_gzip(content)
        raise ValueError(f"unknown action: {action}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_socket_client.py -q`
Expected: 全 passed

- [ ] **Step 5: 删 7 个废弃方法 + 删/改引用它们的测试**

删除方法（含 docstring）：`version`/`routeinfo`/`command_exists`/`filesize`/`version_detail`/`file_upload`/`file_download`。
同步处理 `tests/test_socket_client.py` / `tests/test_mock_server.py` / `tests/test_file_transfer.py` 中对这 7 个方法的所有引用（删除对应用例；`test_file_transfer.py` 整文件删除——它只测将废弃的传输方法）。

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_socket_client.py tests/test_mock_server.py -q && ! ls test_file_transfer.py`
Expected: 全 passed；test_file_transfer.py 不存在

- [ ] **Step 6: Commit**

```bash
cd /opt/mcp_socket_server
git add -A src/mcp_socket_server/socket_client.py tests/
git commit -m "feat: socket_client 增 replay_run/socket_listen,删 7 个废弃方法"
```

---

### Task B3: server.py 删 7 工具 + 改描述

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/server.py`
- Modify: `/opt/mcp_socket_server/tests/test_server_read_tools.py` / `tests/test_write_tools.py`

**Interfaces:**
- Consumes: Task B2 后的 socket_client（7 方法已删）
- Produces: 删除工具函数 `version_query`/`version_detail`/`filesize`/`command_exists`/`routeinfo`/`file_upload`/`file_download` 及其 import；`isfile`/`pcap_flow_extract` 描述更新（含 HTTP curl 示例）

- [ ] **Step 1: 更新测试为收敛后形态**

`tests/test_server_read_tools.py` 重写为：

```python
"""收敛后读支撑工具测试(0.2.0: 删 version_query/detail/filesize/command_exists/routeinfo)。"""
from mcp_socket_server.server import (
    isdir,
    isfile,
    pcap_flow_extract,
)


def test_isfile_and_isdir_tools(mock_server, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    r1 = isfile([mock_server.host], str(f), port=mock_server.port)
    assert r1["ok"] == 1 and r1["results"][0]["exists"] is True
    r2 = isdir([mock_server.host], str(tmp_path), port=mock_server.port)
    assert r2["ok"] == 1 and r2["results"][0]["exists"] is True


def test_pcap_flow_extract_tool(mock_server):
    r = pcap_flow_extract([mock_server.host], "/d", port=mock_server.port)
    assert r["ok"] == 1 and r["results"][0]["flows"][0]["protoType"] == 1


def test_partial_failure_isfile(mock_server):
    res = isfile([mock_server.host, "127.0.0.1:1"], "/x", port=mock_server.port)
    assert res["ok"] == 1
    assert len(res["failed"]) == 1
```

`tests/test_write_tools.py`：删除 `test_file_upload_download` 用例与 `file_download/file_upload` import；其余不动。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_server_read_tools.py tests/test_write_tools.py -q`
Expected: FAIL（ImportError: cannot import name 'version_query' 等——server.py 还没删，import 的是被删方法底层会炸；此处以测试先行定义目标形态）

- [ ] **Step 3: server.py 实施——删 7 个工具函数 + 更新描述**

- 删除函数：`version_query`(约208行)/`isfile` 保留/`isdir` 保留/`routeinfo`/`command_exists`/`filesize`/`version_detail`/`pcap_flow_extract` 保留/`cmd_exec` 保留/`capture_*` 保留/`boce_run` 保留/`file_upload`/`file_download`
- `isfile` 描述替换为（curl 示例按 spec §2.4）：

```python
@mcp.tool()
def isfile(targets: list[str], path: str, port: int = 9000,
           description: CallDescription = "") -> dict:
    """检查靶机文件是否存在(datatype 7,只读)。
    文件传输用靶机 HTTP 服务(:9001,与 TCP 9000 不同端口):
    下载 curl -o out.pcap "http://<host>:9001/files?path=<远端路径>"
    上传 curl -F "path=<远端路径>" -F "file=@本地文件" http://<host>:9001/files
    host 即 targets 里注册的 IP(IPv6 加方括号);若 9001 拒绝连接,说明该靶机未升级到 1.7.0。"""
```

- `pcap_flow_extract` 描述追加一行：

```python
    """提取 pcap 五元组流(datatype 200,只读)。pcap 可先经 HTTP 下载到本地分析:
    curl -o out.pcap "http://<host>:9001/files?path=<pcap 路径>"。"""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q`
Expected: 全 passed（test_transport/test_audit/test_registry 等不受影响）

- [ ] **Step 5: Commit**

```bash
cd /opt/mcp_socket_server
git add src/mcp_socket_server/server.py tests/
git commit -m "feat!: 删 7 工具(version_query/detail/filesize/command_exists/routeinfo/file_upload/download),isfile/pcap_flow_extract 描述带 HTTP 示例"
```

---

### Task B4: 新增 replay_run / socket_listen 工具

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/server.py`
- Modify: `/opt/mcp_socket_server/tests/test_write_tools.py`

**Interfaces:**
- Consumes: `client.replay_run(...)`、`client.socket_listen(action, listen_port, file)`（Task B2）；`LockClass.REPLAY`/`LockClass.SYSTEM`（Task B1）；`_run_batch`（既有）
- Produces: 工具 `replay_run`（REPLAY 锁，audit_tool="replay_run"）、`socket_listen`（SYSTEM 锁逐动作短锁，audit_tool="socket_listen"）——schema 严格按 spec §2.2/§2.3 JSON。FastMCP 从函数签名+docstring 生成 schema，`max_bytes` 的"gzip 解压后明文字节数"语义由 docstring description 表达（与 spec §2.3 schema 一致）；`socket_listen` docstring 必须完整复制 spec §2.3 的工具描述原文（含幂等警示与"9001 拒绝连接说明靶机未升级 1.7.0"的 9001 排查引导写在 isfile docstring，Task B3 Step 3 已含）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_write_tools.py`：

```python
def test_replay_run_tool(app, mock_server):
    res = replay_run([mock_server.host], ["/tmp/a.pcap"], "eth0", "eth1",
                     port=mock_server.port)
    assert res["ok"] == 1
    stats = res["results"][0]
    assert stats["total_packets"] == 10
    assert stats["duration_seconds"] == 0.5


def test_socket_listen_lifecycle(app, mock_server, tmp_path):
    r1 = socket_listen([mock_server.host], "start", listen_port=30001,
                       port=mock_server.port)
    assert r1["ok"] == 1
    assert r1["results"][0]["pid"] == ["12345"]

    r2 = socket_listen([mock_server.host], "fetch", max_bytes=1000,
                       port=mock_server.port)
    assert r2["ok"] == 1
    data = r2["results"][0]
    assert data["truncated"] is True          # 1300B > 1000B
    assert data["size"] == 1300
    assert len(base64.b64decode(data["data_b64"])) == 1000
    assert data["file_path"].startswith("/home/tmp/listen_")

    r3 = socket_listen([mock_server.host], "clean", port=mock_server.port)
    assert r3["ok"] == 1
```

（`import base64` 加到文件头；`replay_run`/`socket_listen` 加进 `from mcp_socket_server.server import (...)`）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_write_tools.py -v -k "replay_run_tool or socket_listen_lifecycle"`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现 2 个工具（schema 逐字对齐 spec）**

追加到 `server.py`（写工具区末尾）：

```python
@mcp.tool()
def replay_run(targets: list[str], pcaps: list[str], uplink_iface: str,
               downlink_iface: str, uplink_vlan: int | None = None,
               downlink_vlan: int | None = None, mbps: int = 0,
               force_ip_src: str | None = None, force_ip_dst: str | None = None,
               force_sport: int | None = None, force_dport: int | None = None,
               port: int = 9000, description: CallDescription = "") -> dict:
    """在靶机回放 pcap 流量文件(scapy 双接口收发,支持限速/改五元组)。需先用 HTTP 上传把 pcap 传到靶机: curl -F "path=/home/tmp/x.pcap" -F "file=@x.pcap" http://<host>:9001/files。回放与抓包/拨测同靶机互斥;回放+抓包请分属两台靶机(跨机拓扑)。返回 {total_files,total_packets,total_bytes,uplink_packets,uplink_bytes,downlink_packets,downlink_bytes,flow_count,duration_seconds,target_mbps,actual_mbps}"""
    logger.info(f"replay_run CALLED: targets={targets} pcaps={pcaps} "
                f"uplink={uplink_iface} downlink={downlink_iface} mbps={mbps}")
    resolved = _resolve_targets(targets)

    def run_one(client):
        return client.replay_run(pcaps, uplink_iface, downlink_iface,
                                 uplink_vlan=uplink_vlan,
                                 downlink_vlan=downlink_vlan, mbps=mbps,
                                 force_ip_src=force_ip_src,
                                 force_ip_dst=force_ip_dst,
                                 force_sport=force_sport,
                                 force_dport=force_dport)

    return _run_batch(resolved, port, run_one,
                      lock_class=LockClass.REPLAY,
                      audit_tool="replay_run",
                      audit_params={"pcaps": pcaps, "uplink_iface": uplink_iface,
                                    "downlink_iface": downlink_iface, "mbps": mbps},
                      timeout=120)


@mcp.tool()
def socket_listen(targets: list[str], action: str, listen_port: int = 30001,
                  file: str = "", max_bytes: int = 1048576, port: int = 9000,
                  description: CallDescription = "") -> dict:
    """在靶机启动 TCP 端口监听接收流量,配合回放做对比测试。action: start=启动返回pid / clean=清空已收数据 / save=已收数据保存到靶机文件 / fetch=取回已收数据(超过 max_bytes 落盘靶机并截断返回)。注意: 已在监听时再 start 为幂等操作,返回现有 pid,更换 listen_port 需先等监听自然结束(服务端无 stop 动作,重启靶机进程才能换端口)。同一监听实例须按 start→...→save/fetch 顺序调用"""
    logger.info(f"socket_listen CALLED: targets={targets} action={action} "
                f"listen_port={listen_port} file={file}")
    if action == "save" and not file:
        return {"ok": 0,
                "failed": [{"target": t, "reason": "save 动作需要 file 参数"}
                           for t in targets],
                "results": []}
    resolved = _resolve_targets(targets)

    def run_one(client):
        if action == "start":
            return client.socket_listen("start", listen_port=listen_port)
        if action == "clean":
            return client.socket_listen("clean")
        if action == "save":
            return client.socket_listen("save", file=file)
        # fetch: 174 取全量 -> 超 max_bytes 截断 + 复用 173 落盘
        data = client.socket_listen("fetch", max_bytes=max_bytes)
        import base64 as _b64
        import time as _time
        size = len(data)
        result = {"size": size}
        if size <= max_bytes:
            result.update({"data_b64": _b64.b64encode(data).decode(),
                           "truncated": False})
            return result
        ts = _time.strftime("%Y%m%d_%H%M%S")
        file_path = f"/home/tmp/listen_{ts}.log"
        client.socket_listen("save", file=file_path)  # 173 在靶机侧写 ss.data 全量
        result.update({"data_b64": _b64.b64encode(data[:max_bytes]).decode(),
                       "truncated": True, "file_path": file_path})
        return result

    return _run_batch(resolved, port, run_one,
                      lock_class=LockClass.SYSTEM,
                      audit_tool="socket_listen",
                      audit_params={"action": action, "listen_port": listen_port,
                                    "file": file, "max_bytes": max_bytes},
                      timeout=30)
```

（`from .audit import get_audit` 等 import 已存在；确认顶部 `import base64` 若用 in-function import 则无需——上面 run_one 内已局部 import）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_write_tools.py -q`
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
cd /opt/mcp_socket_server
git add src/mcp_socket_server/server.py tests/test_write_tools.py
git commit -m "feat: 新增 replay_run(REPLAY锁)/socket_listen(SYSTEM短锁+fetch超限落盘) 工具"
```

---

### Task B5: 版本 0.2.0 + 全量回归 + 真实靶机端到端

**Files:**
- Modify: `/opt/mcp_socket_server/pyproject.toml`（version 0.1.0 → 0.2.0）
- Modify: `/opt/mcp_socket_server/README.md`（工具清单与 HTTP 说明同步——如有工具列表章节）
- Modify: `/opt/mcp_socket_server/CHANGELOG.md`（若存在；不存在则跳过）

- [ ] **Step 1: 版本号 + 文档**

`pyproject.toml` `version = "0.2.0"`；README 工具列表（若维护了）同步为 12 工具清单 + HTTP 文件传输说明段（curl 示例同 spec §1.3）。

- [ ] **Step 2: 全量 pytest**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q`
Expected: 全 passed

- [ ] **Step 3: 进程内 ListTools 校验 12 工具**

```bash
cd /opt/mcp_socket_server && venv/bin/python - <<'EOF'
import asyncio
from mcp_socket_server.server import mcp

async def main():
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    print(len(names), names)
    assert len(names) == 12, names
    expected = sorted(["list_targets","add_target","remove_target","isfile",
                       "isdir","pcap_flow_extract","cmd_exec","capture_start",
                       "capture_stop","boce_run","replay_run","socket_listen"])
    assert names == expected, names
    # description 参数全员在位
    for t in tools:
        assert "description" in t.inputSchema["properties"], t.name
        assert "description" not in t.inputSchema.get("required", []), t.name
    print("12 工具 schema + description 参数校验 OK")

asyncio.run(main())
EOF
```

Expected: `12 工具 schema + description 参数校验 OK`

- [ ] **Step 4: 真实靶机端到端（需 Part A Task A5 完成——靶机已 1.7.0）**

```bash
cd /opt/mcp_socket_server && venv/bin/python - <<'EOF'
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8080/mcp"

async def call(s, name, args):
    r = await s.call_tool(name, args)
    return json.loads(r.content[0].text)

async def main():
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", len(tools.tools))
            assert len(tools.tools) == 12

            # isfile(保留工具回归)
            r = await call(s, "isfile", {"targets": ["10.12.131.82"], "path": "/etc/hostname"})
            print("isfile:", r)
            assert r["ok"] == 1

            # socket_listen start->fetch
            r = await call(s, "socket_listen",
                           {"targets": ["10.12.131.82"], "action": "start",
                            "listen_port": 36001})
            print("listen start:", r)
            assert r["ok"] == 1
            r = await call(s, "socket_listen",
                           {"targets": ["10.12.131.82"], "action": "fetch"})
            print("listen fetch size:", r["results"][0]["size"])
            assert r["ok"] == 1

asyncio.run(main())
EOF
```

Expected: 12 工具；isfile ok；listen start/fetch ok。（replay_run 真实回放需靶机有 pcap + 空闲网卡，验证时若无条件则以 mock 测试覆盖为准，记录跳过原因）

- [ ] **Step 5: 重启生产服务生效**

```bash
# 重启前确认无进行中调用(日志只有 sse ping)
grep -E "tools/call" /var/log/mcp-socket-server.log | tail -1
systemctl restart mcp-socket-server && sleep 3 && systemctl is-active mcp-socket-server
```

- [ ] **Step 6: Commit + push**

```bash
cd /opt/mcp_socket_server
git add -A
git commit -m "chore: 0.2.0 版本 + 文档同步"
git push origin main   # 直连不通则 HTTPS_PROXY=http://10.12.186.204:7897 git push origin main
```

---

## 依赖关系与执行顺序

```
Part A (socket_server):  A1 → A2 → A3 → A4 → A5 → A6(发版)
Part B (mcp):            B1 → B2 → B3 → B4 → B5
跨部依赖:                B5 的真实靶机端到端 需要 A5 完成
```

两 Part 可并行开发（不同仓库）；B5 Step 4 之前不依赖 A。
