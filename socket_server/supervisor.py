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


EXPECTED_EXEC = os.path.join(VERSIONS_DIR, "current", "socket_server")


def _write_unit(exec_path):
    """写入 systemd unit 文件并 daemon-reload，返回是否成功"""
    content = UNIT_CONTENT.format(exec_path=exec_path)
    try:
        with open(UNIT_PATH, "w") as f:
            f.write(content)
    except PermissionError:
        logger.error(f"写入 {UNIT_PATH} 需要 root 权限")
        return False
    _systemctl("daemon-reload")
    return True


def service_enable():
    """安装 systemd unit 并启用开机自启"""
    _ensure_dirs()
    exec_path = _find_current_binary()
    if not exec_path:
        logger.error("找不到 socket_server 二进制，请先部署")
        return False

    if not _write_unit(exec_path):
        return False

    rc, _, err = _systemctl("enable", SERVICE_NAME)
    if rc != 0:
        logger.error(f"enable 失败: {err}")
        return False
    logger.info(f"已安装 systemd unit 并启用开机自启")
    return True


def ensure_unit_correct():
    """启动时自愈：若 unit 的 ExecStart 未指向 current 链接则重写。

    历史版本曾用 os.path.realpath 把 current 链接解析成具体版本目录写进 unit，
    导致 switch_to 切换符号链接后 systemctl restart 仍跑旧版本。本函数在每次
    serve 启动时检查并修正，使旧靶机升级到本版本后下次重启即自愈。
    """
    if not os.path.isfile(EXPECTED_EXEC) or not os.access(EXPECTED_EXEC, os.X_OK):
        return  # current 链接无效，交给 service_enable 处理

    try:
        with open(UNIT_PATH) as f:
            existing = f.read()
    except FileNotFoundError:
        return  # unit 不存在，不在此创建（避免非部署场景误写）

    expected_content = UNIT_CONTENT.format(exec_path=EXPECTED_EXEC)
    if existing.strip() == expected_content.strip():
        return  # 已正确，无需改动

    if _write_unit(EXPECTED_EXEC):
        logger.info(f"已修正 systemd unit ExecStart -> {EXPECTED_EXEC}（自愈）")


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
    """找到当前运行的二进制路径（返回 current 符号链接路径，不解析）"""
    # 返回 current 符号链接路径，这样 switch_to 更新链接后重启即可生效
    current_link = os.path.join(VERSIONS_DIR, "current")
    binary = os.path.join(current_link, "socket_server")
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
