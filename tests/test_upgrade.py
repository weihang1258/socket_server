"""upgrader 本地段单测（1.8.0 升级机制重构后）。

联网查询/下载已收敛到中央节点，upgrader 只剩"本地落盘 + 本地切换"：
  - finalize_pushed_version：dt20 同步段（sha 校验/落盘/残包清理/路径穿越拒绝）
  - switch_to：原子软链切换 + 拒绝未下载版本（service_restart 打桩，不真重启）
  - list_local_versions：本地版本枚举 + current 标记
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from socket_server import upgrader


@pytest.fixture
def versions_dir(tmp_path, monkeypatch):
    d = tmp_path / "versions"
    d.mkdir()
    monkeypatch.setattr(upgrader, "VERSIONS_DIR", str(d))
    return d


def _make_staging(versions_dir, version, content=b"fake-binary"):
    staging = versions_dir / version / ".staging"
    staging.mkdir(parents=True)
    binary = staging / "socket_server"
    binary.write_bytes(content)
    os.chmod(binary, 0o755)
    return binary, hashlib.sha256(content).hexdigest()


def test_finalize_success(versions_dir):
    _make_staging(versions_dir, "1.8.0", b"bin-contents")
    ok, err = upgrader.finalize_pushed_version("1.8.0", hashlib.sha256(b"bin-contents").hexdigest())
    assert ok is True and err is None
    final = versions_dir / "1.8.0" / "socket_server"
    assert final.read_bytes() == b"bin-contents"
    assert os.access(final, os.X_OK)
    assert not (versions_dir / "1.8.0" / ".staging").exists()


def test_finalize_accepts_v_prefix(versions_dir):
    _make_staging(versions_dir, "1.8.1", b"x")
    ok, _ = upgrader.finalize_pushed_version("v1.8.1", hashlib.sha256(b"x").hexdigest())
    assert ok is True


def test_finalize_sha_mismatch_cleans_staging(versions_dir):
    _make_staging(versions_dir, "1.8.0", b"bin")
    ok, err = upgrader.finalize_pushed_version("1.8.0", "0" * 64)
    assert ok is False and "校验失败" in err
    assert not (versions_dir / "1.8.0" / ".staging").exists()


def test_finalize_missing_package(versions_dir):
    ok, err = upgrader.finalize_pushed_version("1.8.0", "a" * 64)
    assert ok is False and "未找到" in err


def test_finalize_accepts_non_executable_staging(versions_dir):
    """:9001 推上来的包是 0644（无执行位），finalize 应照样收货并补 0755（wire 实测教训）"""
    staging = versions_dir / "1.8.0" / ".staging"
    staging.mkdir(parents=True)
    binary = staging / "socket_server"
    binary.write_bytes(b"uploaded")
    os.chmod(binary, 0o644)
    ok, err = upgrader.finalize_pushed_version("1.8.0", hashlib.sha256(b"uploaded").hexdigest())
    assert ok is True and err is None
    assert os.access(versions_dir / "1.8.0" / "socket_server", os.X_OK)


def test_finalize_rejects_path_traversal(versions_dir):
    ok, err = upgrader.finalize_pushed_version("../evil", "a" * 64)
    assert ok is False and "非法" in err


def test_switch_to_updates_symlink(versions_dir, monkeypatch):
    for v, body in (("1.7.4", b"x"), ("1.8.0", b"y")):
        (versions_dir / v).mkdir()
        (versions_dir / v / "socket_server").write_bytes(body)
        os.chmod(versions_dir / v / "socket_server", 0o755)
    monkeypatch.setattr(upgrader, "service_restart", lambda: None)
    assert upgrader.switch_to("v1.8.0") is True
    assert os.readlink(versions_dir / "current") == str(versions_dir / "1.8.0")


def test_switch_to_rejects_not_downloaded(versions_dir, monkeypatch):
    monkeypatch.setattr(upgrader, "service_restart", lambda: None)
    assert upgrader.switch_to("9.9.9") is False


def test_list_local_versions_marks_current(versions_dir):
    for v in ("1.7.4", "1.8.0"):
        (versions_dir / v).mkdir()
        (versions_dir / v / "socket_server").write_bytes(b"x")
    os.symlink(str(versions_dir / "1.7.4"), str(versions_dir / "current"))
    local = dict(upgrader.list_local_versions())
    assert local == {"1.7.4": True, "1.8.0": False}
