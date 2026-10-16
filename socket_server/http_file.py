"""HTTP 文件服务:靶机侧 :9001,GET 下载 / POST multipart 上传。

spec: docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md
- 独立旁路服务,不接 TCP client tracking(不影响 autoupgrade idle 判定)
- 路径 realpath 归一化,全盘可访问(与 TCP 9000 文件接口同级安全模型,仅内网)
"""
from __future__ import annotations

import logging
import os
import threading
from urllib.parse import parse_qs, unquote

logger = logging.getLogger("socket_server")

DEFAULT_HTTP_PORT = 9001


def read_http_port(config_path: str = "/opt/socket/config") -> int:
    """读 config 的 http_port= 行;缺省 9001;显式 0 = 关闭。

    读取模式同 upgrader._read_proxy / autoupgrade._read_autoupgrade_config 先例。
    注意:config 里历史遗留的 port=9000 行未被 serve 接线,勿参照本函数以外的方式读它。
    """
    try:
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("http_port="):
                        return int(line.split("=", 1)[1].strip())
    except Exception as e:
        logger.warning(f"读 http_port 配置失败({e}),用默认 {DEFAULT_HTTP_PORT}")
    return DEFAULT_HTTP_PORT
