import os
import hashlib
import json
import shutil
import logging
import subprocess

import requests
from packaging.version import Version

from .version import VERSION, REPO
from .supervisor import VERSIONS_DIR, CONFIG_PATH, service_restart

logger = logging.getLogger("socket_server")

GITHUB_API = f"https://api.github.com/repos/{REPO}/releases"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/socket_server/version.py"
INSTALL_DIR = "/opt/socket"

# ETag 缓存：带 If-None-Match 请求，304 响应不计入 GitHub API 限额
_latest_etag = None
_latest_cache = None
_releases_etag = None
_releases_cache = []


def _read_proxy():
    """从 /opt/socket/config 读取 proxy= 字段，返回代理 URL 或 None。

    systemd 启动的服务不继承 shell 环境变量，故 http_proxy 等无法靠 env 透传。
    改由 config 文件显式配置，例如: proxy=http://10.12.186.204:7897
    """
    try:
        if not os.path.isfile(CONFIG_PATH):
            return None
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("proxy="):
                    val = line.split("=", 1)[1].strip()
                    return val or None
    except Exception:
        pass
    return None


def _get_proxies():
    """构造 requests 用的 proxies 字典：优先 config 文件，其次环境变量兜底"""
    proxy = _read_proxy()
    if proxy:
        return {"http": proxy, "https": proxy}
    # 兜底：环境变量（非 systemd 启动时，如手动 socket_server upgrade）
    return None


def _proxy_tag():
    """返回当前请求使用的代理标识字符串，用于日志"""
    proxy = _read_proxy()
    if proxy:
        return f"(via proxy {proxy})"
    env_proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy") or \
                os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        return f"(via env proxy {env_proxy})"
    return "(直连)"


def _github_headers():
    """构建 GitHub API 请求头，如果环境变量有 GITHUB_TOKEN 则带上认证"""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_latest_version_raw():
    """从 raw.githubusercontent.com 读 version.py 解析最新版本号。

    走 CDN，不占 GitHub API 限额，故失败可重试。返回版本号字符串或 None。
    仅用于自动升级的版本探测；确认有新版后调 get_latest() 拿 asset 信息。
    """
    last_err = None
    for attempt in range(1, 4):  # 最多 3 次，间隔递增
        try:
            logger.info(f"读取 raw version {RAW_VERSION_URL} {_proxy_tag()} (第{attempt}次)")
            resp = requests.get(RAW_VERSION_URL, timeout=8, headers=_github_headers(), proxies=_get_proxies())
            resp.raise_for_status()
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("VERSION") and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return val or None
            logger.error("raw version.py 未解析到 VERSION")
            return None
        except Exception as e:
            last_err = e
            logger.warning(f"读取 raw version 失败 {_proxy_tag()} (第{attempt}次): {e}")
            if attempt < 3:
                import time as _t
                _t.sleep(2 * attempt)  # 2s, 4s
    logger.error(f"读取 raw version 最终失败 {_proxy_tag()}: {last_err}")
    return None


def get_releases():
    """获取 GitHub 所有 Release 列表（带 ETag，304 不计限额）"""
    global _releases_etag, _releases_cache
    try:
        headers = _github_headers()
        if _releases_etag:
            headers["If-None-Match"] = _releases_etag
        resp = requests.get(GITHUB_API, timeout=10, headers=headers, proxies=_get_proxies())
        if resp.status_code == 304:
            logger.info(f"获取 Releases {resp.status_code} {_proxy_tag()} (未变化)")
            return _releases_cache or []
        resp.raise_for_status()
        logger.info(f"获取 Releases {resp.status_code} {_proxy_tag()}")
        _releases_etag = resp.headers.get("ETag")
        releases = resp.json()
        result = []
        for r in releases:
            tag = r.get("tag_name", "").lstrip("v")
            if not tag:
                continue
            result.append({
                "version": tag,
                "notes": r.get("body", "") or "",
                "assets": r.get("assets", []),
            })
        _releases_cache[:] = result
        return result
    except Exception as e:
        logger.error(f"获取 GitHub Releases 失败 {_proxy_tag()}: {e}")
        return _releases_cache or []


def get_latest():
    """获取最新 Release（带 ETag，304 不计限额）"""
    global _latest_etag, _latest_cache
    try:
        url = f"{GITHUB_API}/latest"
        headers = _github_headers()
        if _latest_etag:
            headers["If-None-Match"] = _latest_etag
        resp = requests.get(url, timeout=10, headers=headers, proxies=_get_proxies())
        if resp.status_code == 304:
            logger.info(f"获取 latest Release {resp.status_code} {_proxy_tag()} (未变化)")
            return _latest_cache
        resp.raise_for_status()
        logger.info(f"获取 latest Release {resp.status_code} {_proxy_tag()}")
        r = resp.json()
        tag = r.get("tag_name", "").lstrip("v")
        if not tag:
            return None
        result = {
            "version": tag,
            "notes": r.get("body", "") or "",
            "assets": r.get("assets", []),
        }
        _latest_etag = resp.headers.get("ETag")
        _latest_cache = result
        return result
    except Exception as e:
        logger.error(f"获取最新 Release 失败 {_proxy_tag()}: {e}")
        return None


def _find_binary_asset(assets):
    """从 assets 中找到主二进制文件"""
    for a in assets:
        name = a.get("name", "")
        if name == "socket_server" or (name.startswith("socket_server") and not name.endswith(".sha256")):
            return a
    return None


def _find_sha256_asset(assets):
    """从 assets 中找到 sha256 校验文件"""
    for a in assets:
        name = a.get("name", "")
        if name.endswith(".sha256"):
            return a
    return None


def _download_file(url, dest_path):
    """下载文件到指定路径，支持大文件流式写入。

    代理取自 /opt/socket/config 的 proxy= 字段（实时读取）。请求失败不重试，
    由调用方决定下个周期再试。
    """
    proxies = _get_proxies()
    logger.info(f"下载: {url} -> {dest_path} {_proxy_tag()}")
    resp = requests.get(url, stream=True, timeout=300, headers=_github_headers(), proxies=proxies)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def _verify_sha256(filepath, sha256_url):
    """校验文件 sha256"""
    try:
        logger.info(f"获取 sha256 校验文件 {sha256_url} {_proxy_tag()}")
        resp = requests.get(sha256_url, timeout=10, headers=_github_headers(), proxies=_get_proxies())
        resp.raise_for_status()
        expected = resp.text.strip().split()[0]  # 格式: "hash  filename"
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        actual = h.hexdigest()
        if actual == expected:
            logger.info(f"sha256 校验通过: {actual}")
            return True
        else:
            logger.error(f"sha256 校验失败: 期望 {expected}, 实际 {actual}")
            return False
    except Exception as e:
        logger.warning(f"sha256 校验跳过（无法获取校验文件） {_proxy_tag()}: {e}")
        return True  # 无校验文件时跳过校验


def download_version(version, assets):
    """下载指定版本到 versions/{version}/"""
    binary_asset = _find_binary_asset(assets)
    if not binary_asset:
        logger.error(f"版本 {version} 没有找到可下载的二进制文件")
        return False

    version_dir = os.path.join(VERSIONS_DIR, version)
    staging_dir = os.path.join(version_dir, ".staging")
    os.makedirs(staging_dir, exist_ok=True)

    binary_url = binary_asset["url"] if "url" in binary_asset else binary_asset["browser_download_url"]
    # GitHub API 返回的 url 需要 Accept header，browser_download_url 可直接下载
    download_url = binary_asset.get("browser_download_url", binary_url)

    staging_binary = os.path.join(staging_dir, "socket_server")
    try:
        _download_file(download_url, staging_binary)
    except Exception as e:
        logger.error(f"下载失败: {e}")
        shutil.rmtree(staging_dir, ignore_errors=True)
        return False

    # sha256 校验
    sha256_asset = _find_sha256_asset(assets)
    if sha256_asset:
        sha256_url = sha256_asset.get("browser_download_url", sha256_asset.get("url", ""))
        if sha256_url:
            if not _verify_sha256(staging_binary, sha256_url):
                logger.error("sha256 校验失败，删除已下载文件")
                shutil.rmtree(staging_dir, ignore_errors=True)
                return False

    # 原子移动到最终位置
    final_binary = os.path.join(version_dir, "socket_server")
    os.chmod(staging_binary, 0o755)
    os.replace(staging_binary, final_binary)
    shutil.rmtree(staging_dir, ignore_errors=True)
    logger.info(f"版本 {version} 下载完成: {final_binary}")
    return True


def is_version_downloaded(version):
    """检查版本是否已下载"""
    binary = os.path.join(VERSIONS_DIR, version, "socket_server")
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


def switch_to(version):
    """切换到指定版本：改符号链接 + 重启"""
    if not is_version_downloaded(version):
        logger.error(f"版本 {version} 未下载")
        return False

    current_link = os.path.join(VERSIONS_DIR, "current")
    target_dir = os.path.join(VERSIONS_DIR, version)

    # 原子更新符号链接
    tmp_link = current_link + ".tmp"
    if os.path.islink(tmp_link):
        os.unlink(tmp_link)
    os.symlink(target_dir, tmp_link)
    os.replace(tmp_link, current_link)  # 原子 rename

    logger.info(f"已切换到版本 {version}，重启服务...")
    service_restart()
    return True


def list_versions():
    """列出 GitHub 所有 Release 版本"""
    releases = get_releases()
    if not releases:
        print("未获取到任何版本信息")
        return

    # 获取本地已下载的版本
    local_versions = set()
    if os.path.isdir(VERSIONS_DIR):
        for d in os.listdir(VERSIONS_DIR):
            if os.path.isfile(os.path.join(VERSIONS_DIR, d, "socket_server")):
                local_versions.add(d)

    print(f"{'序号':<4} {'版本':<12} {'本地':<6} 说明")
    print("-" * 60)
    for i, r in enumerate(releases, 1):
        local_mark = "*" if r["version"] in local_versions else " "
        notes_line = r["notes"].split("\n")[0][:40] if r["notes"] else ""
        print(f"{i:<4} v{r['version']:<11} {local_mark:<6} {notes_line}")


def switch_version(version=None):
    """切换版本：指定版本号或交互选择"""
    releases = get_releases()
    if not releases:
        print("未获取到任何版本信息")
        return

    if version:
        # 直接切换
        target = None
        for r in releases:
            if r["version"] == version.lstrip("v"):
                target = r
                break
        if not target:
            print(f"版本 {version} 不存在于 GitHub Releases")
            return
        if not is_version_downloaded(target["version"]):
            print(f"正在下载版本 {target['version']}...")
            if not download_version(target["version"], target["assets"]):
                print("下载失败")
                return
        switch_to(target["version"])
        print(f"已切换到版本 {target['version']} 并重启服务")
    else:
        # 交互选择
        list_versions()
        try:
            choice = input("\n请输入序号选择版本: ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(releases):
                print("无效序号")
                return
        except (ValueError, EOFError):
            print("已取消")
            return

        target = releases[idx]
        if not is_version_downloaded(target["version"]):
            print(f"正在下载版本 {target['version']}...")
            if not download_version(target["version"], target["assets"]):
                print("下载失败")
                return
        switch_to(target["version"])
        print(f"已切换到版本 {target['version']} 并重启服务")


def upgrade_now():
    """手动触发升级：检查最新版，有则下载+切换"""
    latest = get_latest()
    if not latest:
        print("无法获取最新版本信息")
        return

    if Version(latest["version"]) <= Version(VERSION):
        print(f"当前版本 v{VERSION} 已是最新")
        return

    print(f"发现新版本: v{latest['version']} (当前: v{VERSION})")

    # 检查客户端连接
    try:
        from .server import get_idle_state
        active_clients, _ = get_idle_state()
        if active_clients > 0:
            print(f"当前有 {active_clients} 个活跃客户端连接，请等待客户端断开后再升级")
            return
    except ImportError:
        pass

    if not is_version_downloaded(latest["version"]):
        print(f"正在下载版本 v{latest['version']}...")
        if not download_version(latest["version"], latest["assets"]):
            print("下载失败")
            return

    switch_to(latest["version"])
    print(f"已升级到版本 v{latest['version']} 并重启服务")


def show_current():
    """显示当前版本号 + 版本信息"""
    print(f"当前版本: v{VERSION}")
    print(f"仓库: {REPO}")

    # 尝试获取当前版本的 notes
    try:
        url = f"{GITHUB_API}/tags/v{VERSION}"
        logger.info(f"获取版本说明 {url} {_proxy_tag()}")
        resp = requests.get(url, timeout=5, headers=_github_headers(), proxies=_get_proxies())
        if resp.status_code == 200:
            notes = resp.json().get("body", "")
            if notes:
                print(f"\n版本说明:\n{notes}")
    except Exception as e:
        logger.warning(f"获取版本说明失败 {_proxy_tag()}: {e}")
