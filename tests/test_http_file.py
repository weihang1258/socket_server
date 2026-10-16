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
