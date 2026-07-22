import os
import sys
import logging
from pathlib import Path

from .version import VERSION, REPO

# 全局 logger 配置
logger = logging.getLogger("socket_server")
logger.setLevel(logging.INFO)

_fh = None
_ch = None

def setup_logging(log_file="/var/log/socket_server.log"):
    """初始化日志 handler（仅在 serve 时调用）"""
    global _fh, _ch
    if _ch is not None:
        return  # 已初始化
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    _ch.setFormatter(formatter)
    logger.addHandler(_ch)
    # 文件 handler：目录不存在时跳过（CLI 子命令在非 Linux 环境下可能无 /var/log）
    log_dir = os.path.dirname(log_file)
    if os.path.isdir(log_dir):
        _fh = logging.FileHandler(log_file, encoding='utf-8')
        _fh.setLevel(logging.INFO)
        _fh.setFormatter(formatter)
        logger.addHandler(_fh)


# 抓包工具初始化（延迟到 setup_environment）
_sniff_command = None


def init_capture():
    """抓包初始化。

    抓包已改为进程内 AF_PACKET 实现（capture.py），不再依赖外部 tcpdump/dumpcap
    二进制，也无需检测。保留本函数（setup_environment 调用）仅为接口稳定。
    _sniff_command 仅由 setup_environment 用于日志展示，capture.py 内部不再读取。
    """
    global _sniff_command
    _sniff_command = "afpacket"
    logger.info("使用进程内 AF_PACKET 抓包（不依赖外部二进制）")


def _ensure_sock_buf_limits():
    """serve 启动时确保 net.core.rmem_max/wmem_max >= 8MB。

    boce 突发下 AF_PACKET socket 缓冲若被 rmem_max 静默截断（默认 212992 仅 416KB）
    会丢包（v1.5.2 已加 SO_RCVBUF 截断检测告警，但只告警不修）。serve 以 root 运行，
    写 /proc/sys 即时全局生效；随 systemd 每次启动重设，等价持久，不碰 /etc。
    <8MB 才写，>=8MB 不动；失败只 warn 不阻断 serve。
    """
    TARGET = 8 * 1024 * 1024
    for name in ("net/core/rmem_max", "net/core/wmem_max"):
        path = "/proc/sys/" + name
        try:
            with open(path) as f:
                cur = int(f.read().strip())
            if cur >= TARGET:
                continue
            with open(path, "w") as f:
                f.write(str(TARGET))
            logger.info(f"已将 {name.replace('/', '.')} 从 {cur} 调到 {TARGET}（降低抓包/发送缓冲丢包）")
        except (OSError, ValueError) as e:
            logger.warning(
                f"调整 {name.replace('/', '.')} 失败（不影响服务，"
                f"建议手动 sysctl -w {name.replace('/', '.')}={TARGET}）: {e}"
            )


def setup_environment():
    """serve 启动时的环境初始化（防火墙、java、chromium 等）"""
    setup_logging()
    logger.info(f"version:{VERSION}")
    init_capture()
    logger.info(f"使用抓包工具：{_sniff_command}")
    _ensure_sock_buf_limits()

    # java 路径
    cmd = "dirname `find /usr/local/ -type f -name java|head -n 1`"
    path_java = os.popen(cmd).read().strip()
    if path_java:
        os.environ["PATH"] = path_java + ":" + os.environ.get("PATH", "")
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
