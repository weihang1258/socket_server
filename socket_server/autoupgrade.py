import os
import logging

from .supervisor import CONFIG_PATH

logger = logging.getLogger("socket_server")

# v1.8.0 升级机制重构：每小时 GitHub 轮询线程已删除（版本查询/下载收敛到中央节点
# mcp_socket_server.upgrades，升级下发经 datatype 20，靶机零外呼）。
# 本模块只剩 config 开关读写。autoupgrade= 键保留兼容既有配置文件，缺省 on，
# 语义为"是否接受升级下发"（dt20 落地前检查；off 时中央推送会被拒绝）。


def _read_autoupgrade_config():
    """读取 config 中的 autoupgrade 开关（缺省 on；语义见文件头注释）"""
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
