import os
import subprocess
import logging

logger = logging.getLogger("socket_server")

SERVICE_NAME = "socket_server"
UNIT_PATH = "/etc/systemd/system/socket_server.service"
INSTALL_DIR = "/opt/socket"
VERSIONS_DIR = os.path.join(INSTALL_DIR, "versions")
CONFIG_PATH = os.path.join(INSTALL_DIR, "config")

UNIT_CONTENT = """\
[Unit]
Description=socket_server TCP Service
After=network.target

[Service]
Type=simple
ExecStart={exec_path} serve
Restart=always
RestartSec=5
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
"""


def _systemctl(*args):
    cmd = ["systemctl"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ensure_dirs():
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    # 初始化 config 文件
    if not os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            f.write("autoupgrade=on\n")


def service_enable():
    """安装 systemd unit 并启用开机自启"""
    _ensure_dirs()
    # 找到当前二进制
    exec_path = _find_current_binary()
    if not exec_path:
        logger.error("找不到 socket_server 二进制，请先部署")
        return False

    content = UNIT_CONTENT.format(exec_path=exec_path)
    try:
        with open(UNIT_PATH, "w") as f:
            f.write(content)
    except PermissionError:
        logger.error(f"写入 {UNIT_PATH} 需要 root 权限")
        return False

    _systemctl("daemon-reload")
    rc, _, err = _systemctl("enable", SERVICE_NAME)
    if rc != 0:
        logger.error(f"enable 失败: {err}")
        return False
    logger.info(f"已安装 systemd unit 并启用开机自启")
    return True


def service_disable():
    """禁用开机自启"""
    rc, _, err = _systemctl("disable", SERVICE_NAME)
    if rc != 0:
        logger.error(f"disable 失败: {err}")
        return False
    logger.info("已禁用开机自启")
    return True


def service_start():
    """启动服务"""
    rc, _, err = _systemctl("start", SERVICE_NAME)
    if rc != 0:
        logger.error(f"start 失败: {err}")
        return False
    logger.info("服务已启动")
    return True


def service_stop():
    """停止服务"""
    rc, _, err = _systemctl("stop", SERVICE_NAME)
    if rc != 0:
        logger.error(f"stop 失败: {err}")
        return False
    logger.info("服务已停止")
    return True


def service_restart():
    """重启服务"""
    rc, _, err = _systemctl("restart", SERVICE_NAME)
    if rc != 0:
        logger.error(f"restart 失败: {err}")
        return False
    logger.info("服务已重启")
    return True


def is_service_active():
    """检查服务是否在运行"""
    rc, _, _ = _systemctl("is-active", SERVICE_NAME)
    return rc == 0


def _find_current_binary():
    """找到当前运行的二进制路径"""
    # 优先找 versions/current 链接
    current_link = os.path.join(VERSIONS_DIR, "current")
    if os.path.islink(current_link):
        target = os.path.realpath(current_link)
        binary = os.path.join(target, "socket_server")
        if os.path.isfile(binary) and os.access(binary, os.X_OK):
            return binary

    # fallback: /opt/socket/socket_server
    fallback = os.path.join(INSTALL_DIR, "socket_server")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback

    # 当前可执行文件自身
    import shutil
    own = shutil.which("socket_server")
    return own
