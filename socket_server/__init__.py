import os
import sys
import logging
from pathlib import Path

from .version import VERSION, REPO
from .netutils import ensure_command

# 全局 logger 配置
logger = logging.getLogger("socket_server")
logger.setLevel(logging.INFO)

_fh = None
_ch = None

def setup_logging(log_file="/var/log/socket_server.log"):
    """初始化日志 handler（仅在 serve 时调用）"""
    global _fh, _ch
    if _fh is not None:
        return  # 已初始化
    _fh = logging.FileHandler(log_file, encoding='utf-8')
    _fh.setLevel(logging.INFO)
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    _fh.setFormatter(formatter)
    _ch.setFormatter(formatter)
    logger.addHandler(_fh)
    logger.addHandler(_ch)


# 抓包工具初始化
_sniff_command = None
if ensure_command("dumpcap"):
    _sniff_command = "dumpcap"
elif ensure_command("tcpdump"):
    _sniff_command = "tcpdump"


def init_capture():
    """将 _sniff_command 同步到 capture 模块"""
    from . import capture
    capture._sniff_command = _sniff_command


def setup_environment():
    """serve 启动时的环境初始化（防火墙、java、chromium 等）"""
    setup_logging()
    logger.info(f"version:{VERSION}")
    logger.info(f"使用抓包工具：{_sniff_command}")
    init_capture()

    # java 路径
    cmd = "dirname `find /usr/local/ -type f -name java|head -n 1`"
    path_java = os.popen(cmd).read().strip()
    if path_java:
        os.environ["PATH"] = path_java + ":" + os.environ["PATH"]
        os.environ["JAVA_HOME"] = "/usr/local/jdk1.8.0_202"

    # 打包chromium驱动
    if hasattr(sys, '_MEIPASS'):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(sys._MEIPASS) / "ms-playwright")
    else:
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.expanduser("~/.cache/ms-playwright")

    logger.info(f"env:{os.environ}")

    # 关闭防火墙
    logger.info("关闭防火墙")
    cmds = [
        "sudo systemctl stop firewalld 2>/dev/null || true",
        "sudo systemctl disable firewalld 2>/dev/null || true",
        "sudo iptables -F 2>/dev/null || true",
        "sudo iptables -X 2>/dev/null || true",
        "sudo iptables -Z 2>/dev/null || true",
        "sudo ufw disable 2>/dev/null || true",
        "sudo sed -i 's/^SELINUX=.*/SELINUX=disabled/g' /etc/selinux/config",
        "sudo setenforce 0"
    ]
    for cmd in cmds:
        logger.info(cmd)
        os.system(cmd)
