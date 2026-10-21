import os
import re
import time
import json
import struct
import shutil
import logging
import threading
from io import BytesIO

from .version import VERSION, REPO
from .netutils import (
    exec_cmd_subprocess, routeinfo, isfile, isdir, mkdir, mtu,
    wait_until, wait_not_until, ensure_command,
    compress_gzip, decompress_gzip, unzip, python_cmd
)
from .capture import tcpdump_start, tcpdump_stop  # Tcpdump_scapy 已弃用，不再 import（见 datatype 121/122/123 注释）
from .boce import BoceChecker
from .socket_listen import SocketServerListen
from .pcap_flow import extract_pcap_flow_five_tuples
from .replayer import PcapReplayer

logger = logging.getLogger(__name__)

# 模块级全局状态（延迟初始化）
# tcpdump_scapy = None  # 已弃用：原 scapy 内置抓包单实例全局变量。见 datatype 121/122/123 注释。
ss = None
boce = None  # 延迟初始化，避免 import 时依赖 chromium
cache_sendpkts = None

# dt20 升级落地并发防护：同一时刻只允许一个落盘流程
_upgrade_lock = threading.Lock()
_UPGRADE_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def _apply_pushed_upgrade(data):
    """dt20 同步段：校验 + 落盘（纯本地，毫秒级）。返回 (响应字节, 需后台重启的版本或 None)。

    设计要点（评审定案）：
    - sha 校验/落盘同步做：失败当场回 {"error": 原因}，调用方无需轮询猜测；
    - 只有 service_restart 放后台：回包 sendall 返回即字节已入内核缓冲，
      后台 sleep 0.3s 再重启，响应必达；
    - 空闲判断用 active_clients > 1：该计数含 dt20 自身这条连接。
    """
    version = str(data.get("version") or "").strip().lstrip("v")
    expected_sha = str(data.get("sha256") or "").strip().lower()
    force = bool(data.get("force", False))
    if not _UPGRADE_VERSION_RE.match(version):
        return json.dumps({"error": "version 参数非法（期望如 1.8.0）"},
                          ensure_ascii=False).encode(), None
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        return json.dumps({"error": "缺少或非法 sha256（需 64 位十六进制）"},
                          ensure_ascii=False).encode(), None
    from .autoupgrade import _read_autoupgrade_config
    if not _read_autoupgrade_config():
        return json.dumps({"error": "本机 autoupgrade=off，已拒绝升级"
                                    "（/opt/socket/config 改为 on 后重试）"},
                          ensure_ascii=False).encode(), None
    if not _upgrade_lock.acquire(blocking=False):
        return json.dumps({"error": "升级进行中，请勿重复下发"},
                          ensure_ascii=False).encode(), None
    try:
        try:
            from .server import get_idle_state
            active_clients, _ = get_idle_state()
            if active_clients > 1 and not force:
                return json.dumps(
                    {"error": "还有 %d 个其它客户端连接，拒绝升级（force=true 可强制）"
                              % (active_clients - 1)},
                    ensure_ascii=False).encode(), None
        except ImportError:
            pass
        from .upgrader import finalize_pushed_version
        ok, err = finalize_pushed_version(version, expected_sha)
        if not ok:
            logger.error(f"升级落盘失败: {err}")
            return json.dumps({"error": err}, ensure_ascii=False).encode(), None
        return json.dumps({"ok": True, "version": version}).encode(), version
    finally:
        _upgrade_lock.release()


def _restart_after_upgrade(version):
    """dt20 后台段：切 current 软链 + 重启，使新版本生效。

    必须走 switch_to（原子软链 + 打"升级完成" + 重启），不能只 service_restart——
    unit 的 ExecStart 指向 current 软链，不切链重启回来还是旧版本（wire 实测教训）。
    switch_to 里的"升级完成"日志打在 restart 之前：restart 会杀死本进程。
    正常情况下 switch_to 不返回（进程被 systemd 替换）。
    """
    try:
        time.sleep(0.3)  # 让 dt20 响应先送达客户端的余量（字节已在内核缓冲）
        from .upgrader import switch_to
        if not switch_to(version):
            logger.error(f"升级切换失败：版本 {version} 落盘后仍不可切换（见上方日志）")
    except Exception as e:
        logger.error(f"升级切换失败: {e}")


def do(datatype, data: bytes, **kwargs):
    global ss, cache_sendpkts, boce

    # scapy 发包
    if datatype == 0:
        try:
            data = json.loads(s=data)
            logger.info("发包操作，类型：%s，参数：%s" % (datatype, data))
            if not data.get("pcaps") or not data.get("uplink_iface") or not data.get("downlink_iface"):
                return json.dumps({
                    'status': 'error',
                    'message': 'Missing required parameters: uplink_iface or downlink_iface'
                }).encode('utf-8')
            pcaps = data.pop("pcaps")
            replayer = PcapReplayer(**data)
            stats = replayer.replay_files(pcaps)
            return json.dumps(stats).encode('utf-8')
        except Exception as e:
            logger.error(f"发包错误: {str(e)}")
            return json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8')

    # subprocess 操作
    elif datatype == 1:
        data = json.loads(s=data)
        logger.info("os 操作，类型：%s，参数：%s" % (datatype, data))
        response = exec_cmd_subprocess(**data)
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        res_gzip = compress_gzip(res)
        res_gzip = struct.pack("i", len(res_gzip)) + res_gzip
        return res_gzip

    # 文件传输，下载文件
    elif datatype == 3:
        data = json.loads(s=data)
        logger.info("文件传输：下载文件，类型：%s，文件路径：%s" % (datatype, kwargs["filepath"]))
        with open(kwargs["filepath"], "rb") as f:
            if data.get("gzip") == True:
                content = compress_gzip(f.read())
            else:
                content = f.read()
        msg = struct.pack("<Q", len(content)) + content
        return msg

    # 路由信息查询routeinfo
    elif datatype == 4:
        logger.info("路由信息查询routeinfo，类型：%s" % datatype)
        response = routeinfo()
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        return res

    # 开启tcpdump抓包
    elif datatype == 5:
        data = json.loads(s=data)
        logger.info("开启tcpdump抓包，类型：%s，参数：%s" % (datatype, data))
        response = tcpdump_start(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 停止tcpdump抓包
    elif datatype == 6:
        data = json.loads(s=data)
        logger.info("停止tcpdump抓包，类型：%s，参数：%s" % (datatype, data))
        response = tcpdump_stop(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 是否文件
    elif datatype == 7:
        data = json.loads(s=data)
        logger.info("是否文件，类型：%s，参数：%s" % (datatype, data))
        response = isfile(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 是否目录
    elif datatype == 8:
        data = json.loads(s=data)
        logger.info("是否目录，类型：%s，参数：%s" % (datatype, data))
        response = isdir(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 创建目录
    elif datatype == 9:
        data = json.loads(s=data)
        logger.info("创建目录，类型：%s，参数：%s" % (datatype, data))
        response = mkdir(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 修改mtu
    elif datatype == 10:
        data = json.loads(s=data)
        logger.info("修改mtu，类型：%s，参数：%s" % (datatype, data))
        response = mtu(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 获取字节数
    elif datatype == 11:
        data = json.loads(s=data)
        logger.info("获取字节数，类型：%s，参数：%s" % (datatype, data))
        response = os.path.getsize(data["path"])
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # ========== 已弃用：scapy 内置抓包（datatype 121/122/123） ==========
    # 这套基于全局单实例 Tcpdump_scapy（capture.py 中的类）：
    #   - handlers 模块级变量 tcpdump_scapy 只有一份，第二次 start 会覆盖前一个，
    #     导致前一个抓包对象丢失、pcap 取不回、stop 只能停当前那个。
    #   - 不支持同靶机并发抓包（哪怕不同网卡/不同路径）。
    # 业务实际使用的是 datatype 5/6（tcpdump 命令行那套，tcpdump_start/stop），
    # 靠"不同网卡 + 不同 pcap 路径"区分多个 tcpdump 进程，天然支持并发。
    # 故 121/122/123 整段停用，Tcpdump_scapy 类也一并注释（见 capture.py）。
    # 如需恢复，取消本段注释 + capture.py 中类的注释 + handlers 顶部 import/global。
    # ----------------------------------------------------------------
    # # 开始抓包
    # elif datatype == 121:
    #     data = json.loads(s=data)
    #     logger.info("开始抓包，类型：%s，参数：%s" % (datatype, data))
    #     tcpdump_scapy = Tcpdump_scapy(**data)
    #     tcpdump_scapy.start()
    #     return b"ok"
    #
    # # 停止抓包
    # elif datatype == 122:
    #     logger.info("停止抓包，类型：%s" % datatype)
    #     if tcpdump_scapy is None:
    #         return json.dumps({"error": "未在抓包"}).encode("utf-8")
    #     ok = tcpdump_scapy.stop()
    #     if ok:
    #         return b"ok"
    #     return json.dumps({"error": "停止抓包进程失败"}).encode("utf-8")
    #
    # # 下载pcap包
    # elif datatype == 123:
    #     logger.info("下载pcap包，类型：%s" % datatype)
    #     if tcpdump_scapy is None:
    #         return json.dumps({"error": "未在抓包"}).encode("utf-8")
    #     from scapy.all import PcapWriter
    #     with BytesIO() as fl:
    #         PcapWriter(fl).write(tcpdump_scapy.pkts)
    #         fl.seek(0)
    #         content = fl.read()
    #         return struct.pack("<Q", len(content)) + content
    # ================================================================

    # 拨测开始
    elif datatype == 131:
        data = json.loads(s=data)
        logger.info("拨测开始，类型：%s，参数：%s" % (datatype, data))

        chromium_path = data.get("chromium_path", "/opt/socket/chrome-linux/chrome")

        # chromium 不存在时自动下载（用到才触发，失败则给出手动下载方法）
        if not (os.path.isfile(chromium_path) and os.access(chromium_path, os.X_OK)):
            logger.info(f"chromium 不存在: {chromium_path}，尝试自动下载...")
            from .boce import ensure_chromium
            if not ensure_chromium(chromium_path):
                res = json.dumps({"error": "chromium 自动下载失败，请查看日志中的手动下载方法"}).encode("utf-8")
                res = struct.pack("i", len(res)) + res
                return res

        test_result = exec_cmd_subprocess(
            args=[chromium_path, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom", "about:blank"],
            shell=False, use_run=True
        )
        if test_result['code'] != 0 and 'cannot open shared object file' in (test_result['stderr'] or ''):
            logger.info(f"chromium 缺少依赖库，尝试自动安装...\nstderr: {test_result['stderr']}")

            if shutil.which("yum"):
                install_cmd = (
                    "yum install -y libX11 libXcomposite libXcursor libXdamage libXext "
                    "libXi libXtst cups-libs libXScrnSaver libXrandr GConf2 atk gtk3 "
                    "pango at-spi2-atk libwayland-client libwayland-cursor libwayland-egl "
                    "alsa-lib nss nspr"
                )
            elif shutil.which("apt-get"):
                install_cmd = (
                    "apt-get install -y libx11-6 libxcomposite1 libxcursor1 libxdamage1 "
                    "libxext6 libxi6 libxtst6 libcups2 libxss1 libxrandr2 libgconf-2-4 "
                    "libatk1.0-0 libgtk-3-0 libpango-1.0-0 libpangocairo-1.0-0 "
                    "libwayland-client0 libwayland-cursor0 libasound2 libnss3 libnspr4 "
                    "libgbm1 libxshmfence1"
                )
            else:
                install_cmd = None
                logger.error("未找到 yum 或 apt-get，无法自动安装依赖")

            if install_cmd:
                logger.info(f"执行安装命令: {install_cmd}")
                install_result = exec_cmd_subprocess(args=install_cmd, use_run=True)
                logger.info(
                    f"安装结果 code={install_result['code']}\nstdout={install_result['stdout']}\nstderr={install_result['stderr']}")

                recheck = exec_cmd_subprocess(
                    args=[chromium_path, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom", "about:blank"],
                    shell=False, use_run=True
                )
                if recheck['code'] != 0:
                    logger.error(f"安装后仍然失败: {recheck['stderr']}")
                    res = json.dumps({"error": f"chromium依赖库安装失败: {recheck['stderr'][:200]}"}).encode("utf-8")
                    res = struct.pack("i", len(res)) + res
                    return res
                logger.info("chromium 依赖库安装成功，继续执行拨测")

        response = boce.boce(**data) if boce else None
        if response is None:
            boce = BoceChecker()
            response = boce.boce(**data)
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        res = struct.pack("i", len(res)) + res
        return res

    # 获版本号
    elif datatype == 14:
        logger.info("获版客户端版本号，类型：%s" % datatype)
        response = VERSION
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        return res

    # 查询服务端版本详情（纯本地，不联网）
    elif datatype == 19:
        logger.info("查询服务端版本详情，类型：%s" % datatype)
        try:
            from .release_notes import VERSION as _v, RELEASE_NOTES
            info = {
                "version": VERSION,
                "repo": REPO,
                "release_notes": RELEASE_NOTES,
                "latest_version": _v,
                "has_upgrade": _v != VERSION,
            }
        except ImportError:
            # release_notes.py 未生成（非打包场景，如直接 python 运行）
            info = {"version": VERSION, "repo": REPO, "release_notes": "", "latest_version": VERSION, "has_upgrade": False}
        res = json.dumps(info).encode("utf-8")
        return res

    # 升级推送落地（1.8.0+）：中央经 :9001 推包到 versions/<版本>/.staging/ 后下发。
    # 同步段只做本地校验+落盘（毫秒级），立即回终态；重启在后台线程执行，不占 TCP 连接。
    elif datatype == 20:
        data = json.loads(s=data)
        logger.info("升级推送落地，类型：%s，参数：%s" % (datatype, data))
        resp, restart_version = _apply_pushed_upgrade(data)
        if restart_version:
            threading.Thread(target=_restart_after_upgrade, args=(restart_version,),
                             daemon=True, name="upgrade-restart").start()
        return resp

    # 解压zip文件
    elif datatype == 15:
        data = json.loads(s=data)
        logger.info("解压zip文件，类型：%s，参数：%s" % (datatype, data))
        unzip(**data)
        return b"ok"

    # python操作
    elif datatype == 16:
        data = json.loads(s=data)
        logger.info("python操作，类型：%s，参数：%s" % (datatype, data))
        response = python_cmd(*data)
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        res_gzip = compress_gzip(res)
        res = struct.pack("i", len(res_gzip)) + res_gzip
        return res

    # socket监听-启动监听
    elif datatype == 171:
        data = json.loads(s=data)
        logger.info("socket监听-启动监听，类型：%s，参数：%s" % (datatype, data))
        if ss and ss.pid():
            pass
        elif ss and not ss.pid():
            ss.start_server()
        elif not ss:
            ss = SocketServerListen(**data)
            ss.start_server()
        pid = ss.pid()
        pid_list = [p for p in pid.splitlines() if p.strip()]
        logger.info(f"pid:{pid_list}")
        res = json.dumps({"pid": pid_list}).encode("utf-8")
        return res

    # socket监听-清理数据
    elif datatype == 172:
        logger.info("socket监听-清理数据，类型：%s" % datatype)
        if ss and ss.pid():
            ss.cleandata()
            logger.info("数据清理完成")
        else:
            logger.info("请先启动监听")
            return b"error"
        return b"ok"

    # socket监听-保存数据
    elif datatype == 173:
        data = json.loads(s=data)
        logger.info("socket监听-保存数据，类型：%s，参数：%s" % (datatype, data))
        if ss and ss.pid():
            ss.data_writefile(**data)
            logger.info(f"数据保存完成：{data['file']}")
        else:
            logger.info("请先启动监听")
            return b"error"
        return b"ok"

    # socket监听-传输数据
    elif datatype == 174:
        logger.info("socket监听-传输数据流，类型：%s" % datatype)
        if ss is None:
            return json.dumps({"error": "未启动监听"}).encode("utf-8")
        content = compress_gzip(ss.data)
        msg = struct.pack("<Q", len(content)) + content
        return msg

    # 确保系统中存在指定命令
    elif datatype == 18:
        data = json.loads(s=data)
        logger.info("查询系统是否存在指定命令，类型：%s，参数：%s" % (datatype, data))
        response = ensure_command(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res

    # 提取 pcap 目录下所有 pcap 的方向化五元组流
    elif datatype == 200:
        data = json.loads(s=data)
        logger.info("提取pcap五元组流，类型：%s，参数：%s" % (datatype, data))
        response = extract_pcap_flow_five_tuples(**data)
        logger.info(f"提取完成，共 {len(response)} 条流")
        res = json.dumps(response).encode("utf-8")
        res = struct.pack("i", len(res)) + res
        return res

    # socket监听-停止监听（独立分支，其余逻辑均为 v1.7.0 原样）
    elif datatype == 175:
        logger.info("socket监听-停止监听，类型：%s" % datatype)
        if ss is None:
            return json.dumps({"error": "未启动监听"}, ensure_ascii=False).encode("utf-8")
        ss.stop_server()
        stopped = False
        for _ in range(3):
            if not ss.pid():
                stopped = True
                break
            time.sleep(0.5)
        ss = None
        if stopped:
            logger.info("socket监听已停止")
            return b"ok"
        return json.dumps({"error": "停止监听失败，端口仍被占用"}, ensure_ascii=False).encode("utf-8")

    else:
        pass
