import os
import time
import struct
import socket
import errno
import logging
import threading

logger = logging.getLogger(__name__)

# 全局变量 抓包工具命令（保留供 __init__.init_capture 赋值，进程内 AF_PACKET 实现不再读取）
_sniff_command = None

# 全局变量 CPU绑定记录
_nic_cpu_binding = {}
_allocated_cpus = set()
_cpu_binding_lock = threading.Lock()

# 进程内抓包实例注册表：path -> AFPacketCapture，支持同机多 path 并发抓包
_captures = {}
_captures_lock = threading.Lock()

# Linux AF_PACKET 相关 socket 常量（Python socket 模块未导出，用数值）
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1
PACKET_AUXDATA = 8             # setsockopt(SOL_PACKET, PACKET_AUXDATA,1) → 内核在 cmsg 附带 tpacket_auxdata（含被剥离的 VLAN）
SO_TIMESTAMPNS = 35          # 内核纳秒时间戳
SO_ATTACH_FILTER = 26        # 经典 BPF 过滤器
# tpacket_auxdata.tp_status 位：内核在收包前已把 802.1Q 标签剥到 skb 元数据，auxdata 携带还原信息
TP_STATUS_VLAN_VALID = 0x10       # bit4: tp_vlan_tci 有效
TP_STATUS_VLAN_TPID_VALID = 0x40  # bit6: tp_vlan_tpid 有效
ETH_P_8021Q = 0x8100             # auxdata 无有效 tpid 时的默认 TPID（对齐 libpcap VLAN_TPID 宏）
DLT_EN10MB = 1               # Ethernet 链路类型
PCAP_MAGIC_US_LE = 0xA1B2C3D4   # 微秒，小端（标准 libpcap TCPDUMP_MAGIC）
PCAP_MAGIC_US_BE = 0xD4C3B2A1   # 微秒，大端（字节序互换）
PCAP_MAGIC_NS_LE = 0xA1B23C4D   # 纳秒，小端
PCAP_MAGIC_NS_BE = 0x4D3CB2A1   # 纳秒，大端


from .netutils import routeinfo


class AFPacketCapture:
    """进程内 AF_PACKET 抓包：单线程 recv + 内核纳秒时间戳，直写 pcap。

    设计要点：
    - 不起子进程、不发信号、不查 PID：停止 = 关 socket + join 线程 + 关文件。
      正常路径确定性；join 超时（线程仍存活）则不关文件避免写入竞态，返回 False 可重试。
    - 不改网卡状态（队列/IRQ/RPS），靠抓完按时间戳稳定重排保序（见 _sort_pcap_by_timestamp）。
    - 内核纳秒时间戳经 SO_TIMESTAMPNS 取回，落盘截断为微秒（兼容 replayer/pcap_flow 的 =IIII 解析）。
    - promisc 默认开（兼容本机流量与 SPAN 镜像口），失败只 warn 不 abort。
    - 每路抓包一个实例，按 path 注册到 _captures，天然支持同机多 path 并发。
    """

    def __init__(self, eth, path, extended=""):
        self.eth = eth
        self.path = path
        self.extended = (extended or "").strip()
        self._sock = None
        self._f = None
        self._thread = None
        self._stopped = threading.Event()
        self._exc = None  # 收包线程内异常（start 后检查）
        self._pkt_count = 0
        self._dropped = 0

    def _open(self):
        # proto=0 创建：packet_create（af_packet.c:3393）仅在 proto!=0 时 __register_prot_hook
        # 注册收包。proto=0 不注册，socket 此时不收包。所有 setsockopt + BPF 先就位，
        # 最后 bind(ETH_P_ALL) 才注册收包 -> 第一个包起即被 BPF 过滤，杜绝"bind 后 BPF
        # 附上前"的未过滤窗口（10.12.131.32 实测：bind 到 BPF 附加间隔 ~412ms，期间
        # ARP/UDP/非目标 flag 包漏进缓冲污染抓包结果）。
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
        # 纳秒时间戳：内核为每个包打 SO_TIMESTAMPNS，ancdata 取回
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        except OSError as e:
            logger.warning(f"网卡 {self.eth} 不支持 SO_TIMESTAMPNS: {e}，回退到用户态时间戳（保序精度降低）")
        # 请求内核在 ancdata 附带 tpacket_auxdata（含被剥离的 VLAN TCI/TPID）。
        # AF_PACKET 收包时内核已把 802.1Q 标签从帧里剥到 skb 元数据（net/core/dev.c 的
        # skb_vlan_untag / af_packet.c 的 tp_vlan_tci），不开 AUXDATA 就拿不到，落盘 pcap
        # 丢 VLAN 层。开了之后 _restore_vlan 据 cmsg 还原在线帧（对齐 libpcap pcap-linux.c:2732）。
        # 失败（老内核 ENOPROTOOPT）只 warn，退化为现状（不还原 VLAN）。
        try:
            s.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
        except OSError as e:
            logger.warning(f"网卡 {self.eth} 不支持 PACKET_AUXDATA: {e}，VLAN 标签将无法还原")
        # 增大接收缓冲，降低突发丢包。Linux 会静默把 val 截断到 net.core.rmem_max，
        # 不报错。setsockopt 之后必须 getsockopt 验证实际生效值——若被截断，记 warning
        # 提示 sysctl，否则 boce 突发下 AF_PACKET socket 缓冲溢出、recv 线程来不及
        # drain 的包会被内核丢，且 stop 路径下 tp_drops 才能被准确读到。
        REQUEST_RCVBUF = 4 * 1024 * 1024
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, REQUEST_RCVBUF)
            actual = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            # Linux 把 val 翻倍用于协议开销，所以 getsockopt 返回值 >= 请求值*2。
            # 若实际 < 请求值*2，说明被 rmem_max 截断。
            if actual < REQUEST_RCVBUF * 2:
                try:
                    with open("/proc/sys/net/core/rmem_max") as f:
                        rmem_max = int(f.read().strip())
                except OSError:
                    rmem_max = None
                logger.warning(
                    f"网卡 {self.eth} SO_RCVBUF 被截断：请求 {REQUEST_RCVBUF//1024}KB，"
                    f"实际 {actual//1024}KB"
                    + (f"（net.core.rmem_max={rmem_max}）。建议 sysctl -w net.core.rmem_max=8388608"
                       if rmem_max else "。建议调大 net.core.rmem_max")
                )
        except OSError as e:
            logger.warning(f"网卡 {self.eth} 设置 SO_RCVBUF 失败: {e}")
        # promisc（兼容本机流量 + SPAN 镜像口）；失败不阻断
        try:
            ifindex = socket.if_nametoindex(self.eth)
            mreq = struct.pack("IHH8s", ifindex, PACKET_MR_PROMISC, 0, b"")
            s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            logger.warning(f"网卡 {self.eth} 开启 promisc 失败: {e}（仍可抓本机流量）")
        # BPF 过滤（extended 非空时）：失败视为启动失败抛出，不静默抓全部流量
        # （调用方明确要过滤，静默抓全部是行为错误且可能抓到非预期流量）
        if self.extended:
            try:
                from scapy.arch.linux import attach_filter
                attach_filter(s, self.extended, self.eth)
                logger.info(f"网卡 {self.eth} 已附加 BPF 过滤: {self.extended}")
            except Exception as e:
                raise RuntimeError(f"BPF 过滤编译失败({self.extended}): {e}") from e
        # 最后 bind：此时才注册 prot_hook 开始收包，BPF 已附上，无未过滤窗口。
        # proto=0 创建后 po->num=0，bind 必须显式给 ETH_P_ALL（bind(0) 会 fallback 到
        # po->num=0 不注册收包）。Python AF_PACKET bind 自动 htons 主机序 proto。
        s.bind((self.eth, ETH_P_ALL))
        # 阻塞 recv 加超时，让 stop 的 Event 能及时被检查到
        s.settimeout(1.0)
        self._sock = s
        # pcap 全局头：微秒 magic，snaplen 65535，Ethernet
        global_header = struct.pack("<IHHIIII", PCAP_MAGIC_US_LE, 2, 4, 0, 0, 65535, DLT_EN10MB)
        self._f = open(self.path, "wb")
        self._f.write(global_header)
        self._f.flush()

    def _recv_loop(self):
        # 纯收包循环：内核统计由 stop() 在 join 之后、close 之前调 _read_stats 读取
        # （recv 线程在此不去碰 self._sock 上的 getsockopt，避开 close 顺序竞争）
        try:
            while not self._stopped.is_set():
                try:
                    data, ancdata, _flags, _addr = self._sock.recvmsg(65535, 1024)
                except socket.timeout:
                    continue
                except OSError as e:
                    # 区分"socket 被 stop 关闭的正常退出"与"网卡 down / 网卡消失"。
                    # ENETDOWN(网卡down) / ENODEV(网卡消失)：给出明确原因，否则会被吞成
                    # 含糊的"线程未存活"，掩盖真实启动失败原因（见 10.12.131.35 故障）。
                    if e.errno in (errno.ENETDOWN, errno.ENODEV):
                        logger.error(
                            f"抓包网卡 {self.eth} 不可用（{e.strerror}），请检查网卡是否 up/插线：{self.path}"
                        )
                        self._exc = e
                    # socket 已关闭或网卡不可用，退出循环
                    break
                if not data:
                    break
                ts_ns = self._extract_ts_ns(ancdata)
                frame = self._restore_vlan(data, ancdata)
                self._write_packet(frame, ts_ns)
        except Exception as e:
            self._exc = e
            logger.exception(f"抓包线程异常 {self.eth} -> {self.path}")

    def _extract_ts_ns(self, ancdata):
        """从 ancdata 取 SO_TIMESTAMPNS 的 timespec(秒, 纳秒)，回退到 time.time_ns()。"""
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level == socket.SOL_SOCKET and cmsg_type == SO_TIMESTAMPNS:
                # struct timespec { time_t tv_sec; long tv_nsec; }，@ll 按平台 long 宽度对齐
                tv_sec, tv_nsec = struct.unpack("@ll", cmsg_data)
                return tv_sec * 1_000_000_000 + tv_nsec
        return time.time_ns()

    def _restore_vlan(self, data, ancdata):
        """若内核 auxdata 标记帧带 VLAN，把被剥离的 802.1Q 标签插回 offset 12，还原在线帧。

        内核在 __netif_receive_skb_core（入向 skb_vlan_untag）/ dev_queue_xmit_nit
        （出向 clone 早于 validate_xmit_vlan）路径把 802.1Q 标签从帧剥到 skb 元数据
        （vlan_tci/vlan_proto），AF_PACKET tap 看到的是无 VLAN 的帧。开了 PACKET_AUXDATA
        后内核把 tpacket_auxdata 放进 cmsg，据此还原。严格对齐 libpcap pcap-linux.c:4302-4327。
        非 VLAN 帧原样返回，一字节不加。
        """
        # 找 PACKET_AUXDATA cmsg
        aux = None
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level == SOL_PACKET and cmsg_type == PACKET_AUXDATA:
                aux = cmsg_data
                break
        if aux is None:
            return data  # 内核没给 auxdata（未开或老内核），原样返回
        # tpacket_auxdata: tp_status(u32) tp_len(u32) tp_snaplen(u32) tp_mac(u16)
        # tp_net(u16) tp_vlan_tci(u16) tp_vlan_tpid(u16)，本机字节序无 padding，共 20 字节。
        # cmsg 长度不足（ABI 不一致的老内核）则跳过还原，不崩。
        need = struct.calcsize("=IIIHHHH")
        if len(aux) < need:
            return data
        tp_status, _tp_len, _tp_snaplen, _tp_mac, _tp_net, tp_vlan_tci, tp_vlan_tpid = \
            struct.unpack("=IIIHHHH", aux[:need])
        # VLAN_VALID 宏（pcap-linux.c:315）：tp_vlan_tci!=0 或 TP_STATUS_VLAN_VALID 置位才算 VLAN 帧
        if not (tp_vlan_tci != 0 or (tp_status & TP_STATUS_VLAN_VALID)):
            return data
        # VLAN_TPID 宏（pcap-linux.c:329）：tpid 非零或 TPID_VALID 置位则用之，否则默认 0x8100
        if tp_vlan_tpid != 0 or (tp_status & TP_STATUS_VLAN_TPID_VALID):
            tpid = tp_vlan_tpid
        else:
            tpid = ETH_P_8021Q
        # DLT_EN10MB 的 vlan_offset = 2*ETH_ALEN = 12。帧不足 12B（连 dst+src MAC 都不全）无法插入。
        if len(data) < 12:
            return data
        # offset 12（src MAC 之后、ethertype 之前）插 4 字节 VLAN 标签：TPID(2) + TCI(2)，网络序。
        # 原 offset12-13 的 ethertype 后移到 16-17 成为内层 ethertype = 标准 802.1Q 帧结构。
        # 等价 libpcap 的 memmove(bp, bp+4, 12) + 在 offset12 写 tag。
        return data[:12] + struct.pack("!HH", tpid, tp_vlan_tci) + data[12:]

    def _write_packet(self, data, ts_ns):
        ts_sec = ts_ns // 1_000_000_000
        ts_usec = (ts_ns % 1_000_000_000) // 1000
        caplen = len(data)
        # 记录头：微秒，小端（匹配 replayer/pcap_flow 的 =IIII）
        self._f.write(struct.pack("<IIII", ts_sec, ts_usec, caplen, caplen))
        self._f.write(data)
        self._pkt_count += 1

    def _read_stats(self):
        # tpacket_stats = {u32 tp_packets; u32 tp_drops;} = 8 字节（非 3 个 u32）
        try:
            stats = self._sock.getsockopt(SOL_PACKET, 6, struct.calcsize("II"))
            _pkts, _drops = struct.unpack("II", stats)
            self._dropped = _drops
            if _drops:
                logger.warning(f"抓包 {self.eth} -> {self.path} 内核丢弃 {_drops} 包")
        except (OSError, struct.error):
            pass

    def start(self):
        self._open()
        self._thread = threading.Thread(target=self._recv_loop, name=f"afpacket-{self.eth}", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):
        self._stopped.set()
        # 顺序关键：先让线程退出循环（不能再 recv），再读内核统计（必须在 close 前），
        # 最后才 close socket。原顺序 close→join 会让线程 finally 在已关闭 fd 上
        # getsockopt → OSError 被 except 吞 → _dropped 永远 0，"内核丢弃 0 包"假信号。
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        alive = self._thread is not None and self._thread.is_alive()
        if alive:
            # 线程未在超时内退出：不读 stats、不关文件，避免与仍在 _write_packet 的写入竞态
            # （socket 仍开着让 recv 线程自然退出，本轮写入不应被 close 打断）
            logger.error(f"停止抓包线程超时(>{timeout}s)：{self.eth} -> {self.path}")
            return False
        # 线程已退出、socket 还活着：在 close 前抓最后一次 tpacket_stats（真实值）
        if self._sock is not None:
            self._read_stats()
            try:
                self._sock.close()
            except OSError:
                pass
        # 线程已退出，安全 flush/close（替代旧的轮询文件大小稳定）
        if self._f is not None:
            try:
                self._f.flush()
                self._f.close()
            except Exception:
                pass
        logger.info(f"抓包完成 {self.eth} -> {self.path}：{self._pkt_count} 包，内核丢弃 {self._dropped} 包")
        return True


def tcpdump_start(eth=None, path="/home/tmp/tmp.pcap", extended="", single_queue=False):
    """开启抓包（进程内 AF_PACKET）。

    签名兼容 datatype 5 / MCP capture_start：eth/path/extended/single_queue。
    single_queue 默认 False（零网卡侵入）；显式 True 时才调 SingleQueueRxThread。
    """
    if not eth:
        rt = routeinfo().get("0.0.0.0") or {}
        eth = rt.get("Iface")
        if not eth:
            logger.error("未指定网卡且系统无默认路由，无法确定抓包网卡")
            return False

    # 可选：单队列 + CPU 绑定（opt-in，默认不动网卡）
    if single_queue:
        logger.info(f"配置网卡 {eth} 为单队列模式（opt-in）")
        try:
            SingleQueueRxThread(eth=eth, count=1)
        except Exception as e:
            logger.warning(f"单队列配置失败(忽略，仍可抓包): {e}")

    # 清旧文件
    try:
        if os.path.lexists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"清理旧 pcap {path} 失败: {e}")

    # 同 path 已在抓：先停旧的（避免重复注册）
    with _captures_lock:
        old = _captures.get(path)
    if old is not None:
        logger.warning(f"path {path} 已有抓包在运行，先停止旧实例")
        old.stop()
        with _captures_lock:
            _captures.pop(path, None)

    cap = AFPacketCapture(eth=eth, path=path, extended=extended)
    try:
        cap.start()
    except Exception as e:
        logger.exception(f"启动抓包失败 {eth} -> {path}: {e}")
        return False
    # 起后短暂等待，确认线程存活且无异常（对照旧实现起后检查语义）
    time.sleep(0.5)
    # 先查 _exc：_recv_loop 对网卡 down/消失已记过明确原因，这里补一句启动失败即可，
    # 避免再打含糊的"线程未存活"掩盖真实原因（见 10.12.131.35 故障）。
    if cap._exc is not None:
        logger.error(f"抓包启动失败 {eth} -> {path}: {cap._exc}")
        cap.stop()
        return False
    if cap._thread is None or not cap._thread.is_alive():
        logger.error(f"抓包线程未存活 {eth} -> {path}")
        cap.stop()
        return False
    with _captures_lock:
        _captures[path] = cap
    logger.info(f"已开启抓包 {eth} -> {path}（extended={extended!r}, single_queue={single_queue}）")
    return True


def tcpdump_stop(path="/home/tmp/tmp.pcap"):
    """停止抓包并按时间戳稳定重排 pcap。

    幂等：path 未在抓直接返回 True。停止 = 关 socket + join 线程，无信号无 PID。
    抓完调 _sort_pcap_by_timestamp 保证时间戳单调（根治多队列/RSS 乱序）。

    停止失败（join 超时）：实例保留在 _captures 不移除，避免孤立线程泄漏；
    调用方可重试 stop 直到成功。
    """
    with _captures_lock:
        cap = _captures.get(path)
    if cap is None:
        return True  # 幂等：未在抓
    ok = cap.stop()
    if not ok:
        # 保留注册表条目，调用方可重试 stop；isrun 仍返回 True
        logger.error(f"停止抓包未完成（线程可能仍在运行），保留实例可重试：{path}")
        return False
    with _captures_lock:
        _captures.pop(path, None)
    # 抓完按时间戳稳定重排（核心保序步骤）
    if os.path.isfile(path):
        if not _sort_pcap_by_timestamp(path):
            logger.error(f"pcap 时间戳重排失败，保留原文件：{path}")
            return False
    return True


def tcpdump_isrun(path="/home/tmp/tmp.pcap"):
    """返回 (running, [path])。查 _captures 注册表，替代旧的 pgrep/proc 解析。"""
    with _captures_lock:
        cap = _captures.get(path)
    if cap is not None and cap._thread is not None and cap._thread.is_alive():
        return True, [path]
    return False, []


def _sort_pcap_by_timestamp(path):
    """按 pcap 记录头时间戳稳定重排，原子写覆盖。

    仅处理微秒 magic（LE/BE）；纳秒/pcapng 等格式跳过（记 warn，不破坏文件）。
    稳定排序：同时间戳保持原序（Python sorted 默认稳定）。
    """
    try:
        with open(path, "rb") as f:
            global_header = f.read(24)
            if len(global_header) < 24:
                logger.warning(f"pcap 全局头不完整，跳过重排：{path}")
                return True
            magic = struct.unpack("=I", global_header[:4])[0]
            if magic == PCAP_MAGIC_US_LE:
                endian = "<"
            elif magic == PCAP_MAGIC_US_BE:
                endian = ">"
            elif magic in (PCAP_MAGIC_NS_LE, PCAP_MAGIC_NS_BE):
                logger.warning(f"pcap 为纳秒格式(magic={hex(magic)})，跳过重排：{path}")
                return True
            else:
                logger.warning(f"pcap magic 不识别({hex(magic)})，跳过重排：{path}")
                return True

            records = []
            idx = 0
            while True:
                hdr = f.read(16)
                if len(hdr) < 16:
                    break
                ts_sec, ts_usec, incl_len, _orig_len = struct.unpack(endian + "IIII", hdr)
                data = f.read(incl_len)
                if len(data) < incl_len:
                    logger.warning(f"pcap 末尾记录截断，已读取 {len(records)} 包：{path}")
                    break
                records.append((ts_sec, ts_usec, idx, data))
                idx += 1

        # 稳定排序：(ts_sec, ts_usec) 升序，同时间戳按原 idx 保持
        records.sort(key=lambda r: (r[0], r[1], r[2]))

        tmp = path + ".sorted"
        with open(tmp, "wb") as f:
            f.write(global_header)
            for ts_sec, ts_usec, _idx, data in records:
                f.write(struct.pack(endian + "IIII", ts_sec, ts_usec, len(data), len(data)))
                f.write(data)
        os.replace(tmp, path)
        logger.info(f"pcap 时间戳重排完成：{path}（{len(records)} 包）")
        return True
    except Exception as e:
        logger.exception(f"pcap 重排异常 {path}: {e}")
        return False

# ========== 已弃用：Tcpdump_scapy 类 ==========
# 基于 scapy 内置 sniff 的抓包实现。配套 handlers.py 的 datatype 121/122/123。
# 弃用原因：
#   - 全局单实例设计（handlers.py 的 tcpdump_scapy 模块变量只有一份），
#     第二次 start 覆盖前一个，无法同靶机并发抓多个网卡/路径。
#   - 业务实际用 datatype 5/6（AFPacketCapture 进程内抓包），天然支持并发，保序更好。
# 保留代码注释备查。配套 handlers.py 的 121/122/123 已整段注释。
# ----------------------------------------------------------------
# class Tcpdump_scapy:
#     def __init__(self, iface, filter=None, path=None, timeout=5):  # 初始化有__不知道为啥不显示
#         self.path = path
#         self.iface = iface  # 本地网卡名
#         self.filter = filter  # 过滤条件
#         self.timeout = timeout
#         self.e = False
#         self.pkts = list()
#         if not self.iface:
#             self.iface = routeinfo()["0.0.0.0"]["Iface"]
#
#     def _sniff(self):
#         from scapy.all import sniff, wrpcap
#         self.pkts = []
#         # 注意：不在此重置 self.e=False —— __init__ 已初始化为 False。
#         # 若 start()→stop() 竞态在冷导入期间已把 self.e 置 True，
#         # 保留 True 让 while 循环直接跳过，避免覆盖停止信号。
#         # 分段 sniff：每轮最多阻塞 1 秒，确保无包到达时也能及时检查 stop_filter。
#         # scapy 的 stop_filter 仅在收到包时触发，单次长 timeout 会让 stop() 卡死。
#         # 总时长受 self.timeout 约束（None 表示一直抓到 stop 为止）。
#         start = time.time()
#         while not self.e:
#             if self.timeout is not None:
#                 elapsed = time.time() - start
#                 if elapsed >= self.timeout:
#                     break
#                 remain = min(1, self.timeout - elapsed)
#             else:
#                 remain = 1
#             pkts = sniff(iface=self.iface, count=0,
#                          prn=lambda x: x.sprintf('{IP:%IP.src%->%IP.dst%}'),
#                          filter=self.filter, stop_filter=lambda x: self.e,
#                          timeout=remain)
#             if pkts:
#                 self.pkts.extend(pkts)
#         if self.path:
#             wrpcap(self.path, self.pkts)
#
#     def start(self):  # 开始抓包
#         self.mythread = threading.Thread(target=self._sniff)
#         self.mythread.start()
#         # self.mythread.join()
#
#     def stop(self):
#         self.e = True
#         t1 = time.time()
#         while time.time() - t1 < 30:
#             if not self.mythread.is_alive():
#                 return True
#             logger.info("停止抓包进程，线程存活状态:%s" % self.mythread.is_alive())
#             time.sleep(1)
#         logger.error("停止抓包进程失败：30秒超时")
#         return False
# ================================================================

class SingleQueueRxThread:
    """
    为抓包的网卡配置单队列收包线程
    使用全局变量记录CPU绑定关系，程序重启自动清空
    """
    def __init__(self, eth: str, count: int):
        global _nic_cpu_binding, _allocated_cpus, _cpu_binding_lock
        self.eth = eth
        self.count = count
        self.bound_cpu = None
        
        # 检查是否已绑定（先获取锁，检查后立即释放）
        with _cpu_binding_lock:
            if eth in _nic_cpu_binding:
                self.bound_cpu = _nic_cpu_binding[eth]
                logger.info(f"网卡 {eth} 已绑定到 CPU {self.bound_cpu}，复用现有绑定")
                return
        
        # 未绑定，执行绑定流程（锁已释放，不会死锁）
        self.single_queue_rx_thread()
    def get_rx_queue_count(self):
        """
        获取网卡接收队列数量
        """
        try:
            cmd = r"ethtool -l %s|grep 'Current hardware settings:' -A 5|grep Combined|awk -F ':' '{print $2}'" % self.eth
            response = os.popen(cmd).read().strip()
            return int(response)
        except Exception:
            return 1

    def get_tx_cpu(self):
        cmd = r"cat /proc/interrupts  | grep %s|awk -F ':' '{print $1}'" % self.eth
        response = os.popen(cmd).read().strip()
        if not response:
            logger.error(f"网卡 {self.eth} /proc/interrupts参数不存在，不处理")
            return []
        cpus = ",".join([os.popen(f"cat /proc/irq/{cpu}/smp_affinity_list").read().strip() for cpu in response.split()])
        cpu_list = list()
        for field in cpus.split(","):
            if "-" in field:
                s, e = field.split("-", 1)
                cpu_list += list(range(int(s), int(e) + 1))
            else:
                cpu_list.append(int(field))
        cpu_list = list(set(cpu_list))
        cpu_list.remove(0) if 0 in cpu_list else None  # 去除0
        return cpu_list

    def cache_cpus(self, cpu_list):
        """
        缓存CPU列表
        """
        with open("/tmp/cpus", "w") as f:
            f.write(",".join([str(cpu) for cpu in cpu_list]))

    def restore_cpus(self):
        """
        恢复CPU列表
        """
        if not os.path.exists("/tmp/cpus"):
            return []
        with open("/tmp/cpus", "r") as f:
            cpus = f.read().strip()
        return [int(cpu) for cpu in cpus.split(",")]

    def config_rx_thread_cpus(self, cpu_list: list):
        """
        配置网卡接收队列多线程数量
        """
        cmd = r"cat /proc/interrupts  | grep %s|awk -F ':' '{print $1}'" % self.eth
        response = os.popen(cmd).read().strip()
        if not response:
            raise RuntimeError(f"网卡 {self.eth} 不存在")
        response_list = response.split()
        if len(response_list) != 1:
            raise RuntimeError(f"网卡 {self.eth} 接收队列数量为 {len(response_list)}，不是单队列！")
        irq = response_list[0]
        # logger.info((','.join(list(map(lambda x: str(x),cpu_list))), irq))
        cmd = r"echo '%s'> /proc/irq/%s/smp_affinity_list" % (','.join(list(map(lambda x: str(x),cpu_list))), irq)
        logger.info(cmd)
        return os.system(cmd)

    def single_queue_rx_thread(self):
        logger.info(f"收包网卡：{self.eth} 配置接收队列数量为：{self.count}")

        # 1. 检查网卡是否存在
        if not os.popen(f"ip link show {self.eth} 2>/dev/null").read().strip():
            raise RuntimeError(f"网卡 {self.eth} 不存在")

        queue_count = self.get_rx_queue_count()
        logger.info(f"当前接收队列数量：{queue_count}")
        if queue_count == 1:
            logger.info(f"网卡 {self.eth} 已是单队列，无需配置")
            self._bind_single_queue_cpu()  # 仍然尝试绑定CPU
            return

        # 2. 缩减到单队列
        ret = os.system(f"ethtool -L {self.eth} combined {self.count} 2>/dev/null || "
                        f"ethtool -L {self.eth} rx {self.count} tx {self.count} 2>/dev/null")
        time.sleep(3)

        queue_count = self.get_rx_queue_count()
        logger.info(f"修改后接收队列数量：{queue_count}")

        # 3. 绑定CPU亲和性（用新方法）
        self._bind_single_queue_cpu()
        time.sleep(2)

    def _bind_single_queue_cpu(self):
        """
        通用CPU亲和性绑定：兼容传统中断和 mlx5/MSIX 网卡
        优先级：/proc/interrupts > /sys/class/net/{eth}/queues > ethtool中断名
        同时禁用RPS/RFS避免软中断乱序
        使用全局变量记录绑定关系，程序重启自动清空
        """
        global _nic_cpu_binding, _allocated_cpus, _cpu_binding_lock
        eth = self.eth

        # 方法A：通过 /sys/class/net 找到队列对应的中断号（mlx5兼容）
        irqs = self._get_irqs_via_sys(eth)

        # 方法B：fallback 到 /proc/interrupts grep
        if not irqs:
            irqs = self._get_irqs_via_proc(eth)

        if not irqs:
            logger.error(f"网卡 {eth} 无法获取中断号，跳过CPU绑定")
            return

        # 取非0的空闲CPU（排除已分配的CPU）
        target_cpu = self._pick_target_cpu()
        logger.info(f"将网卡 {eth} 的 {len(irqs)} 个中断绑定到 CPU{target_cpu}")

        # 保存绑定的CPU号
        self.bound_cpu = target_cpu

        for irq in irqs:
            affinity_path = f"/proc/irq/{irq}/smp_affinity_list"
            if os.path.exists(affinity_path):
                ret = os.system(f"echo {target_cpu} > {affinity_path}")
                logger.info(f"IRQ {irq} -> CPU {target_cpu}, ret={ret}")
            else:
                logger.warning(f"IRQ {irq} affinity路径不存在，跳过")

        # 记录绑定关系到全局变量
        with _cpu_binding_lock:
            _nic_cpu_binding[eth] = target_cpu
            _allocated_cpus.add(target_cpu)
            logger.info(f"记录绑定: {eth} -> CPU {target_cpu}")

        # 禁用RPS/RFS，避免软中断在多CPU间分发导致乱序
        self._disable_rps_rfs(eth)

    def _disable_rps_rfs(self, eth):
        """
        禁用网卡的RPS/RFS，确保单CPU处理，避免抓包乱序
        RPS (Receive Packet Steering): 软中断多CPU分发
        RFS (Receive Flow Steering): 基于流的多CPU分发
        """
        # 禁用RPS
        rps_path = f"/sys/class/net/{eth}/queues/rx-0/rps_cpus"
        if os.path.exists(rps_path):
            try:
                with open(rps_path, 'w') as f:
                    f.write('0')
                logger.info(f"已禁用网卡 {eth} 的 RPS")
            except Exception as e:
                logger.warning(f"禁用 RPS 失败: {e}")
        else:
            logger.debug(f"网卡 {eth} 不支持 RPS 或路径不存在")

        # 禁用RFS
        rfs_path = f"/sys/class/net/{eth}/queues/rx-0/rps_flow_cnt"
        if os.path.exists(rfs_path):
            try:
                with open(rfs_path, 'w') as f:
                    f.write('0')
                logger.info(f"已禁用网卡 {eth} 的 RFS")
            except Exception as e:
                logger.warning(f"禁用 RFS 失败: {e}")

    def _get_irqs_via_sys(self, eth):
        """
        通过 /sys/class/net/{eth}/device/msi_irqs 或
        /sys/class/net/{eth}/queues/rx-N/rps_cpus 路径获取中断号
        mlx5、i40e、ixgbe 等高速网卡走这条路
        """
        irqs = []

        # 路径1: msi_irqs 目录（ConnectX / mlx5 系列）
        msi_path = f"/sys/class/net/{eth}/device/msi_irqs"
        if os.path.isdir(msi_path):
            all_irqs = sorted([int(i) for i in os.listdir(msi_path) if i.isdigit()])
            # 只取 rx 相关的中断（mlx5一般前N个是rx队列中断）
            # 缩队列到1后，理论上只有少量rx中断，取前count个
            irqs = all_irqs[:max(self.count * 2, 4)]  # 适当多取几个做保险
            logger.info(f"[sys/msi_irqs] 网卡 {eth} 找到中断: {irqs}")

        # 路径2: 通过中断名匹配（/proc/interrupts 但用网卡名+队列号匹配）
        if not irqs:
            try:
                with open("/proc/interrupts") as f:
                    for line in f:
                        # mlx5 中断名格式: "mlx5_comp0@pci:..." 或 "enp94s0f1np1-..."
                        if eth in line or f"mlx5" in line.lower():
                            irq = line.strip().split(":")[0].strip()
                            if irq.isdigit():
                                irqs.append(int(irq))
                logger.info(f"[proc/interrupts name match] 网卡 {eth} 找到中断: {irqs[:10]}")
                irqs = irqs[:self.count]
            except Exception as e:
                logger.error(f"读取 /proc/interrupts 失败: {e}")

        return irqs

    def _get_irqs_via_proc(self, eth):
        """原有逻辑，作为fallback"""
        cmd = f"cat /proc/interrupts | grep {eth} | awk -F ':' '{{print $1}}'"
        response = os.popen(cmd).read().strip()
        if not response:
            return []
        return [int(i.strip()) for i in response.split() if i.strip().isdigit()]




    def _pick_target_cpu(self):
        """
        选择一个空闲CPU（排除负载100%的CPU、已分配的CPU和CPU 0）
        使用全局变量 _allocated_cpus 记录已分配的CPU
        """
        global _allocated_cpus, _cpu_binding_lock
        try:
            cpu_count = os.cpu_count() or 8
            busy_cpus = set()

            # 1. 获取已被分配的CPU（从全局变量）
            with _cpu_binding_lock:
                busy_cpus.update(_allocated_cpus)
                if _allocated_cpus:
                    logger.info(f"已分配的CPU: {sorted(_allocated_cpus)}")

            # 2. 读取每个CPU的负载
            try:
                with open('/proc/stat', 'r') as f:
                    for line in f:
                        if line.startswith('cpu') and not line.startswith('cpu '):
                            parts = line.split()
                            cpu_id = int(parts[0][3:])  # cpu0 -> 0

                            # user, nice, system, idle, iowait, irq, softirq
                            user = int(parts[1])
                            nice = int(parts[2])
                            system = int(parts[3])
                            idle = int(parts[4])
                            iowait = int(parts[5]) if len(parts) > 5 else 0
                            irq = int(parts[6]) if len(parts) > 6 else 0
                            softirq = int(parts[7]) if len(parts) > 7 else 0

                            total = user + nice + system + idle + iowait + irq + softirq

                            if total > 0:
                                usage = (total - idle) / total
                                if usage >= 0.99:  # 负载99%以上视为满载
                                    busy_cpus.add(cpu_id)
                                    logger.info(f"CPU {cpu_id} 负载 {usage*100:.1f}%，跳过")
            except Exception as e:
                logger.warning(f"读取CPU负载失败: {e}，使用默认策略")

            # 3. CPU 0 保留给系统
            busy_cpus.add(0)

            # 4. 选择第一个空闲CPU
            for cpu in range(1, cpu_count):
                if cpu not in busy_cpus:
                    logger.info(f"选择空闲 CPU {cpu}")
                    return cpu

            # 5. 如果都忙，返回最后一个CPU
            logger.warning("所有CPU都忙碌，选择最后一个CPU")
            return cpu_count - 1

        except Exception as e:
            logger.error(f"选择CPU失败: {e}")
            return 1

    def remove_cpusfile(self):
        """
        删除缓存的CPU列表
        """
        if os.path.exists("/tmp/cpus"):
            try:
                cmd = "ifconfig |grep flags|grep -v '^lo'|awk -F ':' '{print $1}'|xargs -I {} ethtool -l {}|grep 'Current hardware settings' -A4|grep Combined|awk -F ':' '{print $2}'"
                response = os.popen(cmd).read().strip()
                for flag in response.split():
                    if flag.strip() == "1":
                        # logger.info(f"网卡 {self.eth} 接收队列数量存在1，无法删除缓存的CPU列表")
                        return
                os.remove("/tmp/cpus")
                logger.info("删除缓存的CPU列表：/tmp/cpus")
            except Exception:
                return
