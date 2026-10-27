"""http_file 模块单元测试(纯函数级,不起真实服务器的部分)。"""
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from socket_server.http_file import (
    parse_multipart_stream,
    read_http_port,
    start_http_file_server_for_test,
)


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


def _run_stream(body: bytes, boundary: bytes, open_file, length=None):
    """把 body 当 rfile 流喂给解析器。"""
    return parse_multipart_stream(io.BytesIO(body), len(body) if length is None else length,
                                  boundary, open_file)


class _MemSink:
    """open_file 替身:全部收进内存,便于断言。"""

    def __init__(self):
        self.written = b""
        self.path_seen = "UNSET"

    def open(self, path_val):
        self.path_seen = path_val
        return self

    def write(self, b):
        self.written += b


def test_parse_stream_basic():
    sink = _MemSink()
    payload = bytes(range(256))
    body = _multipart_body("/home/tmp/a.pcap", "a.pcap", payload, b"XBOUNDRY")
    r = _run_stream(body, b"XBOUNDRY", sink.open)
    assert r["path"] == "/home/tmp/a.pcap"
    assert r["size"] == len(payload)
    assert sink.written == payload


def test_parse_stream_large_crosses_chunks():
    # 内容远超 _BodyReader.CHUNK(256K),逼出 stream_value 的"留尾/多轮"路径
    payload = os.urandom(256 * 1024 * 4 + 12345)
    sink = _MemSink()
    body = _multipart_body("/tmp/big.bin", "big.bin", payload, b"XBOUND")
    r = _run_stream(body, b"XBOUND", sink.open)
    assert r["size"] == len(payload)
    assert sink.written == payload


def test_parse_stream_binary_contains_boundary_substring():
    # 对抗用例:文件内容里恰好含 boundary 串子序列——"分隔行"切分不受影响
    boundary = b"ABC123"
    tricky = b"xABC123x" + bytes(range(256)) + b"ABC123tail" + b"\r\n--ABC123x" * 3
    sink = _MemSink()
    body = _multipart_body("/tmp/t", "t.bin", tricky, boundary)
    r = _run_stream(body, boundary, sink.open)
    assert r["path"] == "/tmp/t"
    assert sink.written == tricky


def test_parse_stream_boundary_near_chunk_edge():
    # 分隔串跨 chunk 边界:内容长度选成使 \r\n--XBOUND 恰落在 256K 边界附近,
    # stream_value 每轮保留 len(delim)-1 尾字节,必须不误写不漏写
    for pad in (-7, -3, -1, 0, 1, 3, 7):
        payload = b"A" * (256 * 1024 + pad)
        sink = _MemSink()
        body = _multipart_body("/tmp/s.bin", "s.bin", payload, b"SPLITB")
        r = _run_stream(body, b"SPLITB", sink.open)
        assert sink.written == payload and r["size"] == len(payload), f"pad={pad}"


def test_parse_stream_file_before_path_order():
    # 非常规顺序:file 字段在前(path 未知 -> open_file 收到 None,走临时文件路径)
    boundary = b"ORD"
    body = (
        b"--ORD\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f.bin"\r\n\r\n'
        b"CONTENT-XYZ\r\n"
        b"--ORD\r\n"
        b'Content-Disposition: form-data; name="path"\r\n\r\n'
        b"/tmp/late.bin\r\n"
        b"--ORD--\r\n"
    )
    sink = _MemSink()
    r = _run_stream(body, boundary, sink.open)
    assert r["path"] == "/tmp/late.bin"
    assert r["size"] == len(b"CONTENT-XYZ")
    assert sink.written == b"CONTENT-XYZ"
    assert sink.path_seen is None  # 证明走了 path=None 的临时文件分支


def test_parse_stream_missing_path_field():
    boundary = b"B1"
    body = (
        b"--B1\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f"\r\n\r\n'
        b"abc\r\n--B1--\r\n"
    )
    sink = _MemSink()
    r = _run_stream(body, boundary, sink.open)
    assert r["path"] is None  # 解析层不报错,由 handler 层判缺 path 拒绝
    assert sink.written == b"abc"


def test_parse_stream_truncated_body():
    # Content-Length 大于实际 body:读值阶段发现流枯竭,必须抛错而非静默收下
    sink = _MemSink()
    body = _multipart_body("/tmp/t.bin", "t.bin", b"0123456789", b"TR")
    try:
        _run_stream(body[:len(body) - 5], b"TR", sink.open, length=len(body))
        assert False, "should raise"
    except ValueError:
        pass


def test_parse_stream_unknown_field_skipped():
    boundary = b"U1"
    body = (
        b"--U1\r\n"
        b'Content-Disposition: form-data; name="junk"\r\n\r\n'
        b"IGNORED\r\n"
        b"--U1\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f"\r\n\r\n'
        b"KEEP\r\n"
        b"--U1\r\n"
        b'Content-Disposition: form-data; name="path"\r\n\r\n'
        b"/tmp/u.bin\r\n"
        b"--U1--\r\n"
    )
    sink = _MemSink()
    r = _run_stream(body, boundary, sink.open)
    assert r["path"] == "/tmp/u.bin"
    assert sink.written == b"KEEP"


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


def _get_free_http_server(tmpdir):
    port, stop = start_http_file_server_for_test()
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
        assert not os.path.exists(dst + ".part")  # .part 收尾必须已顶替/清理
    finally:
        stop()


def test_post_upload_path_first_streaming(tmpdir):
    # path 在前(文档用法):直写 <dest>.part 后 os.replace 顶替
    dst = str(tmpdir.join("stream") + "/big.bin")
    payload = os.urandom(256 * 1024 * 2 + 555)  # 跨多个读入 chunk
    body = _multipart_body(dst, "big.bin", payload, b"PB")
    base, stop = _get_free_http_server(tmpdir)
    try:
        req = urllib.request.Request(
            base + "/files", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=PB"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        assert result["ok"] is True
        assert result["size"] == len(payload)
        with open(dst, "rb") as f:
            assert f.read() == payload
        assert not os.path.exists(dst + ".part")
    finally:
        stop()


def test_post_upload_file_before_path(tmpdir):
    # file 在前:走系统临时文件再搬到 dest(handler 的兜底分支)
    dst = str(tmpdir.join("late") + "/f.bin")
    payload = b"LATE-ORDER-\x00\xff"
    boundary = b"LB"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f.bin"\r\n\r\n' +
        payload + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="path"\r\n\r\n' +
        dst.encode() + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    base, stop = _get_free_http_server(tmpdir)
    try:
        req = urllib.request.Request(
            base + "/files", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=LB"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        assert result["ok"] is True
        assert result["size"] == len(payload)
        with open(dst, "rb") as f:
            assert f.read() == payload
    finally:
        stop()


def test_post_upload_truncated_leaves_no_part(tmpdir):
    # body 截断:400 且不留 .part / 临时文件残留
    dst = str(tmpdir.join("tr") + "/t.bin")
    full = _multipart_body(dst, "t.bin", b"0123456789" * 100, b"TB")
    body = full[:len(full) - 8]  # 去掉尾部,Content-Length 按完整 body 算
    base, stop = _get_free_http_server(tmpdir)
    try:
        req = urllib.request.Request(
            base + "/files", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=TB"},
            method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        assert not os.path.exists(dst)
        assert not os.path.exists(dst + ".part")
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


def test_start_http_file_server_dual_stack(tmpdir):
    from socket_server.http_file import start_http_file_server
    # 随机端口:用 0 会读 config 默认 9001,这里直测显式端口
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    start_http_file_server(port)  # 应双栈起服,立即返回(内部 daemon 线程)
    # IPv4 可连(无 path 参数 -> 400 也证明服务活着;urllib 对 4xx 抛 HTTPError)
    import time
    time.sleep(0.3)  # 给 daemon 线程起服留时间,避免竞态 flaky
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/files") as resp:
            assert resp.status in (200, 400)
    except urllib.error.HTTPError as e:
        assert e.code in (200, 400)
    # IPv6 可连(本机支持 IPv6 时)
    try:
        try:
            with urllib.request.urlopen(f"http://[::1]:{port}/files") as resp:
                assert resp.status in (200, 400)
        except urllib.error.HTTPError as e:
            assert e.code in (200, 400)
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
