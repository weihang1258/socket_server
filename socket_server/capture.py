import os
import time
import struct
import socket
import errno
import logging
import threading
import multiprocessing
import sys

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

# 最近一次 stop 的结果留存（B03：重复 stop 回放上轮真实结果，而非裸 true 掩盖 exitcode）
_last_stop_status = {}

# Linux AF_PACKET 相关 socket 常量（Python socket 模块未导出，用数值）
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1
PACKET_AUXDATA = 8             # setsockopt(SOL_PACKET, PACKET_AUXDATA,1) → 内核在 cmsg 附带 tpacket_auxdata（含被剥离的 VLAN）
SO_TIMESTAMPNS = 35          # 内核纳秒时间戳（L2 层）
SO_TIMESTAMPING = 37        # 内核多源时间戳（L0/L1 层，HW+SW）：SOF_TIMESTAMPING_* 组合
SO_ATTACH_FILTER = 26        # 经典 BPF 过滤器
# SO_TIMESTAMPING flags（linux/socket.h），请求内核打哪些时间戳
SOF_TIMESTAMPING_RX_SOFTWARE = 1 << 3   # 软中断 RX 时间戳（ts[1]，等同 SO_TIMESTAMPNS）
SOF_TIMESTAMPING_RX_HARDWARE = 1 << 2   # 硬件 RX 时间戳（ts[0]，网卡 PHC，亚纳秒）——核对 linux/net_tstamp.h:20
SOF_TIMESTAMPING_RAW_HARDWARE  = 1 << 6  # 配合 RX_HARDWARE，cmsg 给原始 PHC 时钟 cycles
SCM_TIMESTAMPING = 37        # SO_TIMESTAMPING 产出的 cmsg type（与 SO_TIMESTAMPING 同值，历史命名）
# scm_timestamping cmsg = struct[3] { timespec ts[0]HW ts[1]SW ts[2](unused) }，每 timespec 16B（@ll）
SCM_TIMESTAMPING_SIZE = 48
# tpacket_auxdata.tp_status 位：内核在收包前已把 802.1Q 标签剥到 skb 元数据，auxdata 携带还原信息
TP_STATUS_VLAN_VALID = 0x10       # bit4: tp_vlan_tci 有效
TP_STATUS_VLAN_TPID_VALID = 0x40  # bit6: tp_vlan_tpid 有效
ETH_P_8021Q = 0x8100             # auxdata 无有效 tpid 时的默认 TPID（对齐 libpcap VLAN_TPID 宏）
DLT_EN10MB = 1               # Ethernet 链路类型
PCAP_MAGIC_US_LE = 0xA1B2C3D4   # 微秒，小端（老文件兼容）
PCAP_MAGIC_US_BE = 0xD4C3B2A1   # 微秒，大端（字节序互换）
PCAP_MAGIC_NS_LE = 0xA1B23C4D   # 纳秒，小端（新版默认，标准 libpcap PCAP_NSEC_MAGIC）
PCAP_MAGIC_NS_BE = 0x4D3CB2A1   # 纳秒，大端（字节序互换）


from .netutils import routeinfo
from . import setup_child_logging


class AFPacketCapture:
    """进程内 AF_PACKET 抓包：单线程 recv + 内核纳秒时间戳，直写 pcap。

    v1.6.0 架构变更：本类实例不再跑在主 serve 进程内，而是由 tcpdump_start 起的
    独立子进程加载（见 _capture_process_main）。独立进程 = 独立 GIL，boce 拨测的
    10 个 worker 线程抢不到抓包进程的 GIL → recv 线程能全速 drain socket 队列，
    根治 v1.5.2 第 3 层丢包根因（A 站拨测发起方同机抓包内核丢 887 包）。本类内
    recv/VLAN/纳秒落盘/stop 语义全部不变，只是宿主从线程换成子进程内的线程。

    设计要点：
    - 不起子进程、不发信号、不查 PID：停止 = 关 socket + join 线程 + 关文件。
      正常路径确定性；join 超时（线程仍存活）则不关文件避免写入竞态，返回 False 可重试。
    - 单队列默认开（tcpdump_start single_queue=True）：缩队列+绑 CPU+关 RPS/RFS，
      确保收包序=线序（配合纳秒排序根治多队列 RSS 乱序）。注意：该配置会改动网卡状态，
      且 stop 不还原（持续生效到下次手动调整）；需零侵入时显式传 single_queue=False。
    - 内核纳秒时间戳经 SO_TIMESTAMPNS 取回，以纳秒落盘（PCAP_NSEC_MAGIC 0xA1B23C4D，
      不截断微秒），让 burst 内同微秒的包也能被时间戳区分。读取方据 magic 判定除数。
    - VLAN 还原：开 PACKET_AUXDATA，内核把被剥离的 802.1Q 标签放 tpacket_auxdata
      cmsg，_restore_vlan 据 cmsg 在 offset 12 插回（对齐 libpcap pcap-linux.c:4302）。
      recvmsg 路径天然支持 cmsg，零数据丢失。
    - snaplen 65535：不截断任何帧（含 jumbo），满足"抓包必抓原始数据、零信息丢失"。
    - promisc 默认开（兼容本机流量与 SPAN 镜像口），失败只 warn 不 abort。
    - 每路抓包一个子进程实例，按 path 注册到 _captures，天然支持同机多 path 并发。
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
        # 时间戳精度模式（_open 探测，_recv_loop 据此解码 cmsg）：
        # 'hw_ts'  -> SO_TIMESTAMPING 硬件 RX（网卡 PHC，亚纳秒，mlx5/ixgbe 物理网卡支持）
        # 'sw_ts'  -> SO_TIMESTAMPING 软中断 RX（纳秒，等同 SO_TIMESTAMPNS，virtio 等）
        # 'ns_ts'  -> SO_TIMESTAMPNS（纳秒，老内核/驱动不支持 SO_TIMESTAMPING 的兜底）
        # 'userspace' -> time.monotonic_ns() 在 recvmsg 返回时取（微秒级，全失败的兜底）
        self._ts_mode = 'userspace'
        # hw_ts 模式下若 cmsg ts[0] 全零（驱动/内核未填 PHC），退化软中断 ts[1]；
        # 首次退化 warn 一次，让 35 mlx5 这类声称支持 HW 的网卡能一眼看出 HW 是否真生效（不静默退化）。
        self._hw_degraded_warned = False

    def _open(self):
        # proto=0 创建：packet_create（af_packet.c:3393）仅在 proto!=0 时 __register_prot_hook
        # 注册收包。proto=0 不注册，socket 此时不收包。所有 setsockopt + BPF 先就位，
        # 最后 bind(ETH_P_ALL) 才注册收包 -> 第一个包起即被 BPF 过滤，杜绝"bind 后 BPF
        # 附上前"的未过滤窗口（10.12.131.32 实测：bind 到 BPF 附加间隔 ~412ms，期间
        # ARP/UDP/非目标 flag 包漏进缓冲污染抓包结果）。
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
        # 时间戳精度自动降级（v1.6.1）：优先硬件时间戳（网卡 PHC，亚纳秒），逐层降到软件
        # 纳秒、再降 SO_TIMESTAMPNS、最后用户态。一次性 setsockopt 探测存 self._ts_mode，
        # _recv_loop 据此解码 cmsg。SO_TIMESTAMPING 是 SO_TIMESTAMPNS 的超集（同时支持
        # HW+SW，cmsg 给 ts[3]），HW 是否可用由 cmsg ts[0] 是否非零判定（无需预探测硬件支持）。
        #   L0 hw_ts：setsockopt SOF_TIMESTAMPING_RX_HARDWARE|RX_SOFTWARE|RAW_HARDWARE
        #             -> cmsg ts[0] 非零则用 hw（亚纳秒 PHC），ts[0]==0 退 ts[1]（软中断纳秒）
        #   L1 sw_ts：L0 setsockopt 失败（ENOPROTOOPT）-> 退 SO_TIMESTAMPING 仅 RX_SOFTWARE
        #             -> cmsg ts[1]（等同 SO_TIMESTAMPNS）
        #   L2 ns_ts：L1 也失败 -> SO_TIMESTAMPNS（老内核 2.6.x，极罕见）
        #   L3 userspace：L2 也失败 -> recvmsg 返回时 time.monotonic_ns()（精度最低，兜底）
        ts_flags = SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RX_SOFTWARE | SOF_TIMESTAMPING_RAW_HARDWARE
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, ts_flags)
            self._ts_mode = 'hw_ts'  # 实际精度由 cmsg ts[0] 是否非零在 _decode_ts 内判定
        except OSError:
            try:
                s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, SOF_TIMESTAMPING_RX_SOFTWARE)
                self._ts_mode = 'sw_ts'
            except OSError:
                try:
                    s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
                    self._ts_mode = 'ns_ts'
                except OSError as e:
                    self._ts_mode = 'userspace'
                    logger.warning(f"网卡 {self.eth} 不支持任何内核时间戳（{e}），回退用户态时间戳（保序精度最低）")
        logger.info(f"网卡 {self.eth} 时间戳精度模式: {self._ts_mode}（hw=亚纳秒PHC/sw+ns=纳秒软中断/userspace=微秒）")
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
        # v1.6.0：抓包已移到独立子进程，GIL 不被 boce worker 抢（根治 v1.5.2 第 3 层
        # 丢包根因）。但 recv 线程 drain 间隙的瞬时积压仍靠 SO_RCVBUF 缓冲，这里设大
        # + __init__ 调大 rmem_max，双保险保"零数据丢失"。GIL 主因已解，此层退为抗抖动。
        # v1.6.3：32MB——镜像口环境有并发流量（llcj 批量回放）时，回放突发 + 并发流叠加
        # 可能瞬时填满 4MB 缓冲导致内核丢包（35 实测 65850 帧的并发场景）。32MB = 8MB
        # rmem_max 的 4 倍，recv 线程 12.5s 内足够吞下任意并发突发。
        REQUEST_RCVBUF = 32 * 1024 * 1024
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
        # pcap 全局头：纳秒 magic，snaplen 65535，Ethernet
        # 纳秒时间戳让 burst 内同微秒的包可被区分，配合单队列收包序=线序实现保序。
        # 兼容性：replayer/pcap_flow 读取时据 magic 决定除数（1e9 vs 1e6）。
        # 旧版微秒 magic 文件仍可读（见 _sort_pcap_by_timestamp 的纳秒回退逻辑）。
        global_header = struct.pack("<IHHIIII", PCAP_MAGIC_NS_LE, 2, 4, 0, 0, 65535, DLT_EN10MB)
        self._f = open(self.path, "wb")
        self._f.write(global_header)
        self._f.flush()

    def _recv_loop(self):
        # 纯收包循环：内核统计由 stop() 在 join 之后、close 之前调 _read_stats 读取
        # （recv 线程在此不去碰 self._sock 上的 getsockopt，避开 close 顺序竞争）
        # 停止语义：_stopped.set() 后**必须 drain 完队列剩余帧**再退出（而不是立即 break）。
        # recvmsg 阻塞超时(1s)保证循环能及时看到 _stopped，队列中的在途帧（stop 前已进
        # socket 缓冲、recv 线程尚未读走）在下一轮 recvmsg 全部读出后才退出——否则这些帧
        # 随 socket close 被内核丢弃，pcap 少最后一批包（35 实测 61612 vs 61613 差 1-2 帧，
        # 非内核丢包，是 stop 时队列未 drain 完）。
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
                ts_ns = self._decode_ts_ns(ancdata)
                frame = self._restore_vlan(data, ancdata)
                self._write_packet(frame, ts_ns)
            # 停止信号已置位：drain 完队列剩余帧（stop 前已进缓冲、尚未读走的在途帧），
            # 全部落盘后再退出。超时则退出（避免 stop join 卡死），剩帧下次 stop 重试。
            if self._stopped.is_set() and not self._exc:
                try:
                    self._sock.settimeout(1.0)
                    drain_deadline = time.monotonic() + 2.0   # 置位后最多再 drain 2s（B01：无时限下
                    # 高流量在途帧持续到达，队列永不空，线程不退 -> join(10s) 超时 -> 文件不收尾）
                    drained_after = 0
                    while True:
                        if time.monotonic() > drain_deadline:
                            logger.warning(
                                f"drain 超时(2s)，放弃残余在途帧：停止后已补收 {drained_after} 包 "
                                f"{self.eth} -> {self.path}")
                            break
                        try:
                            data, ancdata, _flags, _addr = self._sock.recvmsg(65535, 1024)
                        except socket.timeout:
                            logger.info(f"drain 完成：停止后补收 {drained_after} 包 {self.eth} -> {self.path}")
                            break  # 队列已空
                        except OSError:
                            break  # 网卡不可用/已关闭
                        if not data:
                            break
                        ts_ns = self._decode_ts_ns(ancdata)
                        frame = self._restore_vlan(data, ancdata)
                        self._write_packet(frame, ts_ns)
                        drained_after += 1
                except Exception:
                    pass
        except Exception as e:
            self._exc = e
            logger.exception(f"抓包线程异常 {self.eth} -> {self.path}")

    def _decode_ts_ns(self, ancdata):
        """据 self._ts_mode 从 ancdata 解码纳秒时间戳，回退到 time.monotonic_ns()。

        - 'hw_ts'/'sw_ts'：取 SCM_TIMESTAMPING cmsg（struct[3] timespec，ts[0]HW ts[1]SW ts[2]unused）。
          hw_ts 模式下 ts[0] 非零用硬件（亚纳秒 PHC），ts[0]==0 退 ts[1]（软中断纳秒）--由 cmsg 内容
          自适应，不依赖预探测硬件支持（驱动/内核不给 HW 就自动降级，与 setsockopt 时探测解耦）。
          sw_ts 模式下直接用 ts[1]。
        - 'ns_ts'：取 SO_TIMESTAMPNS cmsg（单 timespec）。
        - 'userspace'：cmsg 无时间戳，调用点用 time.monotonic_ns()（_recv_loop 已包此分支）。
        timespec 用 @ll 解（time_t + long，按平台 long 宽度对齐，与 SO_TIMESTAMPNS 同）。
        """
        if self._ts_mode in ('hw_ts', 'sw_ts'):
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.SOL_SOCKET and cmsg_type == SCM_TIMESTAMPING:
                    if len(cmsg_data) < SCM_TIMESTAMPING_SIZE:
                        break  # cmsg 长度不足（ABI 异常），退 userspace
                    # 3 个 timespec，每个 @ll = 16B
                    hw_sec, hw_nsec, sw_sec, sw_nsec, _u1, _u2 = struct.unpack("@llllll", cmsg_data[:SCM_TIMESTAMPING_SIZE])
                    if self._ts_mode == 'hw_ts' and (hw_sec or hw_nsec):
                        return hw_sec * 1_000_000_000 + hw_nsec  # 硬件时间戳（亚纳秒 PHC）
                    if sw_sec or sw_nsec:
                        # hw_ts 模式但 ts[0] 零 -> HW 未生效（网卡声称支持但驱动未填 PHC，如 virtio/lo），
                        # 退化软中断纳秒。首次 warn 一次，便于诊断 HW 是否真用上（35 mlx5 验证用）。
                        if self._ts_mode == 'hw_ts' and not self._hw_degraded_warned:
                            logger.warning(
                                f"网卡 {self.eth} 硬件时间戳未生效（ts[0]=0），退化软中断纳秒。"
                                f"ethtool -T 显示 hardware-receive 但驱动未填 PHC，HW 时间戳未真正启用"
                            )
                            self._hw_degraded_warned = True
                        return sw_sec * 1_000_000_000 + sw_nsec  # 软中断时间戳（纳秒）
                    break  # ts[0]/ts[1] 都零（内核未打），退 userspace
        elif self._ts_mode == 'ns_ts':
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.SOL_SOCKET and cmsg_type == SO_TIMESTAMPNS:
                    tv_sec, tv_nsec = struct.unpack("@ll", cmsg_data)
                    return tv_sec * 1_000_000_000 + tv_nsec
        # userspace 模式 / hw_ts|sw_ts 但 cmsg 缺失或 ts 全零 -> 调用点取单调纳秒
        return time.monotonic_ns()

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
        ts_nsec = ts_ns % 1_000_000_000
        caplen = len(data)
        # 记录头：纳秒，小端（PCAP_NSEC_MAGIC）。纳秒分辨率让 burst 内同微秒的包可被
        # 时间戳区分；配合单队列（收包序=线序），重排后保序更鲁棒。
        # 注意：replayer/pcap_flow 读记录头仍用 =IIII（4 个 u32），仅 ts 的单位不同，
        # 解析方据全局头 magic 判定除数（见 replayer.replay_file 的 _magic 检测）。
        self._f.write(struct.pack("<IIII", ts_sec, ts_nsec, caplen, caplen))
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
            logger.error(f"停止抓包线程超时(>{timeout}s)：{self.eth} -> {self.path}")
            # B06：线程未退出也必须收尾文件，否则 pcap 永久截断。
            # 竞态最坏损失=1 条记录（close 与写线程 write 瞬时交叉）；截断文件+明确失败信号
            # 优于永久无尾文件。
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            if self._f is not None:
                try:
                    self._f.flush()
                    self._f.close()
                except Exception:
                    pass
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


def _capture_process_main(eth, path, extended, single_queue, ready_conn, stop_evt):
    """抓包子进程入口（v1.6.0）。

    在独立进程内跑 AFPacketCapture，与主 serve 进程隔离 GIL（boce 拨测的 worker 线程
    抢不到本进程 GIL），recv 线程全速 drain，根治 v1.5.2 第 3 层丢包根因（A 站拨测
    发起方同机抓包内核丢 887 包）。

    与主进程的协议（multiprocessing）：
    - ready_conn（Pipe 的一端）：_open 成功发 ("ready", None)；失败发 ("error", str(exc))。
      替代旧主进程 time.sleep(0.5)+查_exc 的盲等：主进程不再 sleep 轮询 _exc，改为阻塞等
      子进程明确的就绪/失败信号。子进程内仍保留一处 sleep(0.5)（见下注释，非盲等）。
    - stop_evt（Event）：主进程 stop 时 set()。本入口主线程 wait() 醒后调 cap.stop()
      收尾（join recv 线程 -> 读内核统计 -> close socket -> flush/close pcap），再调
      _sort_pcap_by_timestamp 纳秒重排（抓包的子进程最清楚自己写的文件）。
    - pcap 文件：子进程直写 path（共享文件系统），主进程 join 后下载，现契约不变。

    单队列配置也在子进程做（内聚：网卡状态改动只影响抓包进程）。任何异常经 ready_conn
    回传后子进程退出；主进程读不到 ready 视为启动失败。
    """
    # 日志初始化：fork 下继承主进程 handler（复用），spawn 下重建（否则子进程日志静默丢失，
    # _recv_loop 网卡 down 诊断、tp_drops 告警全消失）。socket_listen 子进程已是此模式。
    setup_child_logging()

    # 单队列 + CPU 绑定（收包序=线序，根治多队列 RSS 乱序）
    if single_queue:
        logger.info(f"配置网卡 {eth} 为单队列模式（默认开启）")
        try:
            SingleQueueRxThread(eth=eth, count=1)
        except Exception as e:
            logger.warning(f"单队列配置失败(忽略，仍可抓包): {e}")

    def _send_and_close(msg):
        """回传 ready/error 并关闭写端，保证每条返回路径都不漏 fd。"""
        try:
            ready_conn.send(msg)
        except Exception:
            pass
        try:
            ready_conn.close()
        except Exception:
            pass

    cap = AFPacketCapture(eth=eth, path=path, extended=extended)
    try:
        cap.start()
    except Exception as e:
        logger.exception(f"启动抓包失败 {eth} -> {path}: {e}")
        _send_and_close(("error", str(e)))
        return
    # sleep(0.5)：给收包线程起跑 + 网卡 down/消失时 _recv_loop 的 ENETDOWN 立即抛 _exc 的窗口。
    # 不是"盲等"（盲等已由 ready 信号替代），而是确认 recv 线程稳定存活后再回报就绪，避免
    # 线程刚 start 尚未进入 recvmsg 循环就因网卡问题退出、却已报 ready 的假阳性。
    time.sleep(0.5)
    if cap._exc is not None:
        logger.error(f"抓包启动失败 {eth} -> {path}: {cap._exc}")
        cap.stop()
        _send_and_close(("error", str(cap._exc)))
        return
    if cap._thread is None or not cap._thread.is_alive():
        logger.error(f"抓包线程未存活 {eth} -> {path}")
        cap.stop()
        _send_and_close(("error", "抓包线程未存活"))
        return
    _send_and_close(("ready", None))  # 回传完毕关闭写端，避免 fd 泄漏（主进程 recv 后也关读端）
    logger.info(f"已开启抓包(子进程 PID={os.getpid()}) {eth} -> {path}（extended={extended!r}, single_queue={single_queue}）")

    # 轮询等停止信号或 recv 线程退出：纯 stop_evt.wait() 在 recv 线程崩溃/网卡 down 后仍阻塞，
    # 致子进程存活、tcpdump_isrun 误报 True（v1.5.6 查线程存活无此问题，此处恢复语义）。
    while not stop_evt.wait(1.0):
        if cap._thread is None or not cap._thread.is_alive():
            logger.error(f"抓包 recv 线程已退出 {eth} -> {path}（网卡 down 或异常），停止抓包")
            break
    # 收尾：cap.stop() 返回 False 表示 recv 线程 10s 未退出、文件未 flush/close--此时不应 sort
    # （避免读未 flush 数据 os.replace 覆盖致截断 pcap）。
    stop_ok = cap.stop()
    sort_ok = True
    if stop_ok and os.path.isfile(path):
        sort_ok = _sort_pcap_by_timestamp(path)
        if not sort_ok:
            logger.error(f"pcap 时间戳重排失败，保留原文件：{path}")
    if not stop_ok or not sort_ok:
        sys.exit(1)  # 退出码非 0 通知主进程本轮 pcap 可能不完整（主进程查 exitcode 返回 False）


def tcpdump_start(eth=None, path="/home/tmp/tmp.pcap", extended="", single_queue=True):
    """开启抓包（独立子进程 AF_PACKET，v1.6.0）。

    签名兼容 datatype 5 / MCP capture_start：eth/path/extended/single_queue。
    起独立子进程跑 AFPacketCapture，与主 serve 进程隔离 GIL，根治 boce 同机抓包
    丢包（v1.5.2 第 3 层 GIL 竞争根因）。single_queue 默认 True：收包序=线序。
    """
    if not eth:
        rt = routeinfo().get("0.0.0.0") or {}
        eth = rt.get("Iface")
        if not eth:
            logger.error("未指定网卡且系统无默认路由，无法确定抓包网卡")
            return False

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
        _stop_capture_entry(old)
        with _captures_lock:
            _captures.pop(path, None)

    # Pipe：子进程回传 ready/error（替代旧 sleep0.5 盲等）
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    stop_evt = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_capture_process_main,
        args=(eth, path, extended, single_queue, child_conn, stop_evt),
        daemon=True,
    )
    try:
        proc.start()
    except Exception as e:
        logger.exception(f"启动抓包子进程失败 {eth} -> {path}: {e}")
        for c in (parent_conn, child_conn):
            try:
                c.close()
            except Exception:
                pass
        return False
    # 子进程已 fork 继承 child_conn 副本，主进程的 child_conn 引用不再需要，显式关避免依赖 GC
    try:
        child_conn.close()
    except Exception:
        pass

    # 等子进程就绪信号（带超时兜底：进程崩溃/异常不卡死主流程）
    status = None
    err = None
    try:
        if parent_conn.poll(10):
            msg = parent_conn.recv()
            status, err = msg
        else:
            status, err = "error", "子进程启动超时(10s)未就绪"
    except (EOFError, OSError) as e:
        status, err = "error", f"读取子进程就绪信号失败: {e}"
    finally:
        try:
            parent_conn.close()  # 读端关闭，避免 fd 泄漏
        except Exception:
            pass

    if status != "ready":
        logger.error(f"抓包启动失败 {eth} -> {path}: {err}")
        # 回收子进程
        stop_evt.set()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
        return False

    with _captures_lock:
        _captures[path] = (proc, stop_evt)
    logger.info(f"已开启抓包(子进程 PID={proc.pid}) {eth} -> {path}（extended={extended!r}, single_queue={single_queue}）")
    return True


def _stop_capture_entry(entry):
    """停止单个抓包子进程（proc, stop_evt）。

    stop_evt.set() 通知子进程收尾（cap.stop + sort）；join 超时则 terminate 兜底。
    返回 True/False（对齐旧 cap.stop() 语义，失败保留实例可重试）。
    """
    proc, stop_evt = entry
    if not proc.is_alive():
        return True
    stop_evt.set()
    proc.join(timeout=30)  # = cap.stop 内 recv 线程 join 10s + sort 冗余，30s 足够
    if not proc.is_alive():
        return True
    # 超时未退出：强制终止（极端情况下可能丢最后一批未 flush 的包，属异常路径）
    logger.error(f"抓包子进程停止超时(>30s)，强制终止：{proc.pid}")
    proc.terminate()
    proc.join(timeout=5)
    return not proc.is_alive()


def tcpdump_stop(path="/home/tmp/tmp.pcap"):
    """停止抓包（独立子进程）。返回 dict 四态（v1.7.1，B02/B03）：
    正常停止 {"stopped":True,"exitcode":N,"pcap_complete":bool}
    从未 start {"stopped":False,"note":"no active capture for path"}
    重复 stop   上轮 _last_stop_status + note（回放真实结果）
    停止失败   {"stopped":False,"error":...}（实例保留可重试，既有行为）
    """
    with _captures_lock:
        entry = _captures.get(path)
    if entry is None:
        last = _last_stop_status.get(path)
        if last:
            return {**last, "note": "already stopped（重复 stop，回放上轮结果）"}
        return {"stopped": False, "note": "no active capture for path"}
    ok = _stop_capture_entry(entry)
    if not ok:
        status = {"path": path, "stopped": False,
                  "error": "停止未完成（停止线程超时），实例保留可重试"}
        _last_stop_status[path] = status
        logger.error(f"停止抓包未完成（子进程可能仍在运行），保留实例可重试：{path}")
        return status
    with _captures_lock:
        _captures.pop(path, None)
    # 子进程退出码反映 cap.stop()/sort 成败；非 0 则本轮 pcap 可能不完整/未排序
    code = entry[0].exitcode
    status = {"path": path, "stopped": True,
              "exitcode": code,
              "pcap_complete": code in (0, None)}
    _last_stop_status[path] = status
    if code not in (0, None):
        status["warning"] = "pcap 可能不完整/未排序"
        logger.warning(f"抓包子进程退出码 {code}，本轮 pcap 可能不完整/未排序：{path}")
    return status


def tcpdump_isrun(path="/home/tmp/tmp.pcap"):
    """返回 (running, [path])。查 _captures 子进程是否存活。"""
    with _captures_lock:
        entry = _captures.get(path)
    if entry is not None and entry[0].is_alive():
        return True, [path]
    return False, []


def _sort_pcap_by_timestamp(path):
    """按 pcap 记录头时间戳稳定重排，原子写覆盖。

    支持微秒 magic（0xA1B2C3D4 / 0xD4C3B2A1）和纳秒 magic（0xA1B23C4D / 0x4D3CB2A1）。
    微秒：按 (ts_sec, ts_usec, idx) 排序；纳秒：按 (ts_sec, ts_nsec, idx) 排序。
    纳秒分辨率让 burst 内同微秒的包也能被时间戳区分，配合单队列（收包序=线序）实现保序。
    稳定排序：同时间戳保持原序（Python sorted 默认稳定）。
    pcapng 等格式跳过（记 warn，不破坏文件）。
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
                divisor = 1_000_000  # 微秒文件
            elif magic == PCAP_MAGIC_US_BE:
                endian = ">"
                divisor = 1_000_000
            elif magic == PCAP_MAGIC_NS_LE:
                endian = "<"
                divisor = 1_000_000_000  # 纳秒文件
            elif magic == PCAP_MAGIC_NS_BE:
                endian = ">"
                divisor = 1_000_000_000
            else:
                logger.warning(f"pcap magic 不识别({hex(magic)})，跳过重排：{path}")
                return True

            records = []
            idx = 0
            while True:
                hdr = f.read(16)
                if len(hdr) < 16:
                    break
                ts_sec, ts_frac, incl_len, _orig_len = struct.unpack(endian + "IIII", hdr)
                data = f.read(incl_len)
                if len(data) < incl_len:
                    logger.warning(f"pcap 末尾记录截断，已读取 {len(records)} 包：{path}")
                    break
                records.append((ts_sec, ts_frac, idx, data))
                idx += 1

        # 稳定排序：(ts_sec, ts_frac) 升序，同时间戳按原 idx 保持
        records.sort(key=lambda r: (r[0], r[1], r[2]))

        tmp = path + ".sorted"
        with open(tmp, "wb") as f:
            f.write(global_header)
            for ts_sec, ts_frac, _idx, data in records:
                f.write(struct.pack(endian + "IIII", ts_sec, ts_frac, len(data), len(data)))
                f.write(data)
        os.replace(tmp, path)
        logger.info(f"pcap 时间戳重排完成：{path}（{len(records)} 包，divisor={divisor}）")
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
