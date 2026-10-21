import os
import re
import hashlib
import shutil
import logging

from .version import VERSION, REPO
from .supervisor import VERSIONS_DIR, service_restart

logger = logging.getLogger("socket_server")

INSTALL_DIR = "/opt/socket"

# v1.8.0 升级机制重构：版本查询与二进制下载收敛到中央节点（mcp_socket_server.upgrades），
# 靶机侧不再有任何 GitHub 联网——本模块只剩"本地落盘 + 本地切换"。
# 升级包由中央经 :9001 HTTP 文件服务推到 versions/<版本>/.staging/，随后下发 datatype 20，
# 由 finalize_pushed_version 校验落盘。历史的联网函数（get_latest_version_raw/get_latest/
# get_releases/_download_file/download_version/upgrade_now 等）随 1.8.0 删除。

_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def finalize_pushed_version(version, expected_sha256):
    """校验并落盘中央推送的升级包（纯本地，dt20 同步段调用）。

    包位于 versions/<version>/.staging/socket_server。sha256 与 dt20 参数一致才落盘，
    不一致清理残包并拒绝。返回 (True, None) 或 (False, 原因)。
    """
    version = str(version).strip().lstrip("v")
    if not _VERSION_RE.match(version):
        return False, f"version 非法: {version!r}"
    version_dir = os.path.join(VERSIONS_DIR, version)
    staging_dir = os.path.join(version_dir, ".staging")
    staging_binary = os.path.join(staging_dir, "socket_server")
    # 只判存在不判执行位：:9001 推上来的包是 0644，执行位由本函数 chmod 补
    # （wire 实测教训：照搬 is_version_downloaded 的 X_OK 前置检查会把新推的包拒掉）
    if not os.path.isfile(staging_binary):
        return False, f"未找到推送的升级包: {staging_binary}（需先经 :9001 上传）"
    h = hashlib.sha256()
    with open(staging_binary, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != str(expected_sha256).strip().lower():
        shutil.rmtree(staging_dir, ignore_errors=True)
        return False, f"sha256 校验失败: 期望 {expected_sha256}, 实际 {actual}（已清理残包）"
    final_binary = os.path.join(version_dir, "socket_server")
    os.chmod(staging_binary, 0o755)
    os.replace(staging_binary, final_binary)
    shutil.rmtree(staging_dir, ignore_errors=True)
    logger.info(f"升级包落盘: {final_binary} (sha256={actual[:16]}...)")
    return True, None


def is_version_downloaded(version):
    """检查版本是否已下载到本地 versions/"""
    version = str(version).strip().lstrip("v")
    binary = os.path.join(VERSIONS_DIR, version, "socket_server")
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


def switch_to(version):
    """切换到指定版本：原子改 current 软链 + 重启。仅接受本地已下载版本，不联网。"""
    version = str(version).strip().lstrip("v")
    if not is_version_downloaded(version):
        logger.error(f"版本 {version} 未下载到本地 {VERSIONS_DIR}，无法切换")
        return False

    current_link = os.path.join(VERSIONS_DIR, "current")
    target_dir = os.path.join(VERSIONS_DIR, version)

    # 原子更新符号链接
    tmp_link = current_link + ".tmp"
    if os.path.islink(tmp_link):
        os.unlink(tmp_link)
    os.symlink(target_dir, tmp_link)
    os.replace(tmp_link, current_link)  # 原子 rename

    # 日志必须打在 restart 之前：restart 会杀死本进程，之后的日志不会落盘
    logger.info(f"升级完成：v{VERSION} → v{version}，重启服务生效...")
    service_restart()  # systemd 环境：本进程在此被替换，不会返回
    return True


def list_local_versions():
    """列出本地 versions/ 已下载的版本号，返回 [(版本, 是否 current)]（不含 current 链接）"""
    local = []
    current_link = os.path.join(VERSIONS_DIR, "current")
    current_target = os.path.basename(os.readlink(current_link)) if os.path.islink(current_link) else None
    if os.path.isdir(VERSIONS_DIR):
        for d in sorted(os.listdir(VERSIONS_DIR)):
            if d == "current":
                continue
            if os.path.isfile(os.path.join(VERSIONS_DIR, d, "socket_server")):
                local.append((d, d == current_target))
    return local


def list_versions():
    """列出本地已下载的版本（CLI list 子命令；1.8.0 起不再查询 GitHub）"""
    local = list_local_versions()
    if not local:
        print(f"本地 {VERSIONS_DIR} 没有已下载版本")
        return
    print(f"{'版本':<12} 说明")
    print("-" * 40)
    for v, is_current in local:
        mark = " <- current 软链指向" if is_current else ""
        print(f"v{v:<11}{mark}")


def switch_version(version=None):
    """切换到本地已下载的版本（1.8.0 起不再从 GitHub 下载缺失版本）"""
    local = [v for v, _ in list_local_versions()]
    if version is None:
        if not local:
            print("本地没有已下载版本，无法交互选择")
            return
        for i, v in enumerate(local, 1):
            print(f"{i}. v{v}")
        try:
            choice = input("\n请输入序号选择版本: ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(local):
                print("无效序号")
                return
        except (ValueError, EOFError):
            print("已取消")
            return
        version = local[idx]
    else:
        version = str(version).strip().lstrip("v")
        if version not in local:
            print(f"版本 {version} 不在本地 {VERSIONS_DIR}，无法切换（1.8.0 起本机不再联网下载）。"
                  f"已下载: {', '.join('v' + v for v in local) or '无'}")
            return
    if switch_to(version):
        print(f"已切换到版本 {version} 并重启服务")


def show_current():
    """显示当前版本号 + 本地烧入的版本说明（纯本地，不联网）"""
    print(f"当前版本: v{VERSION}")
    print(f"仓库: {REPO}")
    try:
        from .release_notes import VERSION as _v, RELEASE_NOTES
        if RELEASE_NOTES:
            print(f"\n版本说明 (v{_v}):\n{RELEASE_NOTES}")
    except ImportError:
        pass
