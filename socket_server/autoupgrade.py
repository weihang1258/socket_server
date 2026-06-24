import os
import time
import logging
import threading

from packaging.version import Version

from .version import VERSION
from .upgrader import get_latest, is_version_downloaded, download_version, switch_to
from .supervisor import VERSIONS_DIR, CONFIG_PATH

logger = logging.getLogger("socket_server")

# 自动升级后台线程
_auto_thread = None
_auto_stop_event = threading.Event()

# 检查间隔（秒）
CHECK_INTERVAL = 3600  # 1 小时
# 空闲阈值（秒）
IDLE_THRESHOLD = 1800  # 30 分钟


def _read_autoupgrade_config():
    """读取 config 中的 autoupgrade 开关"""
    try:
        if not os.path.isfile(CONFIG_PATH):
            return True  # 默认 on
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("autoupgrade="):
                    return line.split("=", 1)[1].strip().lower() == "on"
        return True
    except Exception:
        return True


def set_autoupgrade(state):
    """设置自动升级开关"""
    # 读取现有配置
    config = {}
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()

    config["autoupgrade"] = state

    with open(CONFIG_PATH, "w") as f:
        for k, v in config.items():
            f.write(f"{k}={v}\n")

    print(f"自动升级已{'开启' if state == 'on' else '关闭'}")
    logger.info(f"自动升级已{'开启' if state == 'on' else '关闭'}")


def _autoupgrade_loop():
    """自动升级后台循环"""
    logger.info("自动升级线程启动")
    while not _auto_stop_event.is_set():
        try:
            # 检查开关
            if not _read_autoupgrade_config():
                logger.debug("自动升级已关闭，跳过检查")
                _auto_stop_event.wait(CHECK_INTERVAL)
                continue

            # 检查最新版
            latest = get_latest()
            if not latest:
                logger.debug("无法获取最新版本信息")
                _auto_stop_event.wait(CHECK_INTERVAL)
                continue

            new_ver = latest["version"]

            # 版本比较
            if Version(new_ver) <= Version(VERSION):
                logger.debug(f"当前版本 v{VERSION} 已是最新")
                _auto_stop_event.wait(CHECK_INTERVAL)
                continue

            logger.info(f"发现新版本: v{new_ver}")

            # 下载（如果未下载）
            if not is_version_downloaded(new_ver):
                logger.info(f"正在下载版本 v{new_ver}...")
                if not download_version(new_ver, latest["assets"]):
                    logger.error(f"版本 v{new_ver} 下载失败，等下次重试")
                    _auto_stop_event.wait(CHECK_INTERVAL)
                    continue

            # 检查客户端空闲状态
            try:
                from .server import get_idle_state
                active_clients, idle_seconds = get_idle_state()
                if active_clients > 0:
                    logger.info(f"当前有 {active_clients} 个活跃客户端，暂不升级")
                    _auto_stop_event.wait(CHECK_INTERVAL)
                    continue
                if idle_seconds < IDLE_THRESHOLD:
                    logger.info(f"客户端断开仅 {idle_seconds:.0f} 秒，需等待 {IDLE_THRESHOLD} 秒后升级")
                    _auto_stop_event.wait(CHECK_INTERVAL)
                    continue
            except ImportError:
                logger.warning("无法获取客户端状态，跳过空闲检查")

            # 执行切换
            logger.info(f"客户端空闲超过 {IDLE_THRESHOLD} 秒，开始升级到 v{new_ver}")
            switch_to(new_ver)
            return  # 升级后当前进程会被重启，退出循环

        except Exception as e:
            logger.error(f"自动升级检查异常: {e}")

        _auto_stop_event.wait(CHECK_INTERVAL)


def start_autoupgrade_thread():
    """启动自动升级后台线程"""
    global _auto_thread
    if _auto_thread is not None and _auto_thread.is_alive():
        return
    _auto_stop_event.clear()
    _auto_thread = threading.Thread(target=_autoupgrade_loop, daemon=True, name="autoupgrade")
    _auto_thread.start()


def stop_autoupgrade_thread():
    """停止自动升级后台线程"""
    _auto_stop_event.set()
    if _auto_thread and _auto_thread.is_alive():
        _auto_thread.join(timeout=5)
