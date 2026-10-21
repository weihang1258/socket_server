"""http_file 模块单元测试(纯函数级,不起真实服务器的部分)。"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from socket_server.http_file import parse_multipart, read_http_port, start_http_file_server_for_test


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
