import os
import struct
import socket
import time
import logging
import queue
from collections import namedtuple
from multiprocessing import Value, Lock

logger = logging.getLogger(__name__)

class PcapStats:
    """数据包发送统计类"""

    def __init__(self):
        self.packet_counter = Value('i', 0)  # 发送的数据包总数
        self.byte_counter = Value('i', 0)  # 发送的字节总数
        self.file_counter = Value('i', 0)  # 处理的文件数量
        self.start_time = Value('d', 0.0)  # 开始发送时间
        self.end_time = Value('d', 0.0)  # 结束发送时间
        self.current_pps = Value('d', 0.0)  # 当前每秒包数
        self.current_mbps = Value('d', 0.0)  # 当前带宽(Mbps)
        self.should_stop = Value('i', 0)  # 终止标志
        self.total_packets = Value('i', 0)  # 总包数
        self.processed_packets = Value('i', 0)  # 已处理包数
        self._lock = Lock()

    def update_stats(self, packets: int, bytes_sent: int):
        """更新包统计信息"""
        with self._lock:
            self.packet_counter.value += packets
            self.byte_counter.value += bytes_sent
            self.processed_packets.value += packets

            duration = time.time() - self.start_time.value
            if duration > 0:
                self.current_pps.value = self.packet_counter.value / duration
                self.current_mbps.value = (self.byte_counter.value * 8) / (duration * 1000000)

            if self.processed_packets.value >= self.total_packets.value:
                self.should_stop.value = 1

    def increment_file_counter(self):
        """增加文件计数"""
        with self._lock:
            self.file_counter.value += 1

    def stop(self):
        """设置终止标志"""
        self.should_stop.value = 1

    def cleanup(self):
        """清理资源"""
        self.packet_counter.value = 0
        self.byte_counter.value = 0
        self.file_counter.value = 0
        self.processed_packets.value = 0
        self.total_packets.value = 0
        self.should_stop.value = 0
        self.current_pps.value = 0
        self.current_mbps.value = 0


class PacketBuffer:
    """数据包缓冲池"""

    def __init__(self, max_size_gb=2, chunk_size_mb=1):
        self.max_size = max_size_gb * 1024 * 1024 * 1024  # 转换为字节
        self.chunk_size = chunk_size_mb * 1024 * 1024  # 块大小(字节)
        self.current_size = 0  # 当前使用的内存大小
        self.buffer = queue.Queue(maxsize=1000)  # 存储数据包块的队列
        self.total_in = 0  # 进入缓冲池的总字节数
        self.total_out = 0  # 从缓冲池发出的总字节数
        self.total_packets_in = 0  # 进入缓冲池的总包数
        self.total_packets_out = 0  # 从缓冲池发出的总包数
        self.buffer_usage = 0  # 缓冲池使用率
        self.max_usage = 0  # 最大使用率
        self.buffer_hits = 0  # 缓冲池命中次数
        self.buffer_misses = 0  # 缓冲池未命中次数

    def put(self, chunk, chunk_size, packet_count):
        """放入数据包块"""
        self.buffer.put((chunk, chunk_size, packet_count))
        self.current_size += chunk_size
        self.total_in += chunk_size
        self.total_packets_in += packet_count
        self.buffer_usage = (self.current_size / self.max_size) * 100
        self.max_usage = max(self.max_usage, self.buffer_usage)
        if self.buffer_usage < 90:
            self.buffer_hits += 1
        else:
            self.buffer_misses += 1

    def get(self):
        """获取数据包块"""
        chunk, chunk_size, packet_count = self.buffer.get()
        self.current_size -= chunk_size
        self.total_out += chunk_size
        self.total_packets_out += packet_count
        self.buffer_usage = (self.current_size / self.max_size) * 100
        return chunk, chunk_size, packet_count


# 定义五元组结构，用于唯一标识一条流
FiveTuple = namedtuple("FiveTuple", ["mac_src", "mac_dst", "ip_src", "ip_dst", "sport", "dport", "proto"])


class FlowInfo:
    def __init__(self, direction, first_packet_time):
        self.direction = direction
        self.first_seen_time = first_packet_time
        self.modified_ip_src = None  # 缓存修改后的源 IP
        self.modified_ip_dst = None  # 缓存修改后的目的 IP
        self.modified_sport = None  # 缓存修改后的源端口
        self.modified_dport = None  # 缓存修改后的目的端口
        self.ip_version = None  # 缓存 IP 类型（4 或 6）


class PcapReplayer:
    def __init__(self, uplink_iface, downlink_iface=None, uplink_vlan=None, downlink_vlan=None, mbps=0, verbose=False,
                 force_ip_src=None, force_ip_dst=None, force_sport=None, force_dport=None, force_build_flow=False,
                 client_ip_hint=None, server_ip_hint=None, enable_pcap_cache=False, pcap_cache_dir="cached_pcaps"):
        # 上行和下行的物理接口名
        self.uplink_iface = uplink_iface
        self.downlink_iface = downlink_iface if downlink_iface else uplink_iface
        # 上行和下行的VLAN ID（可选）
        self.uplink_vlan = uplink_vlan
        self.downlink_vlan = downlink_vlan
        # 目标速率（Mbps），0表示不限速
        self.mbps = mbps
        # 是否输出详细日志
        self.verbose = verbose
        # 按五元组缓存流信息（方向、首次时间、五元组修改缓存等）
        self.flow_table = {}
        self.reset_stats()
        # 是否为"无方向"模式（即接口和VLAN都一样，所有包都视为上行）
        self.directionless = (uplink_iface == downlink_iface and uplink_vlan == downlink_vlan)
        # 是否不做五元组修改（所有force参数都为None）
        self.no_tuple_modification = all(x is None for x in [force_ip_src, force_ip_dst, force_sport, force_dport])
        # 新增：是否强制建流（默认True）。为False时，directionless且no_tuple_modification时不建流。
        self.force_build_flow = force_build_flow

        # 指定要强制修改的五元组字段（为空则表示不修改）
        self.force_ip_src = force_ip_src
        self.force_ip_dst = force_ip_dst
        self.force_sport = force_sport
        self.force_dport = force_dport

        # 新增：用于辅助判断上行/下行的客户端和服务器IP
        self.client_ip_hint = client_ip_hint
        self.server_ip_hint = server_ip_hint

        # 新增：用于缓存发包到pcap文件
        self._cache_enabled = enable_pcap_cache
        self._cache_dir = pcap_cache_dir
        self._uplink_pcap_writer = None
        self._downlink_pcap_writer = None
        self._single_pcap_writer = None
        self._pcap_global_header = b''
        # 新增：socket缓存
        self._uplink_socket = None
        self._downlink_socket = None

    def reset_stats(self):
        self.total_files = 0
        self.total_packets = 0
        self.total_bytes = 0
        self.uplink_packets = 0
        self.downlink_packets = 0
        self.uplink_bytes = 0
        self.downlink_bytes = 0
        self.flow_count = 0
        self.start_time = 0
        self.end_time = 0

    def parse_ethernet(self, data):
        dst, src, eth_type = struct.unpack("!6s6sH", data[:14])
        return {
            'dst': dst,
            'src': src,
            'eth_type': eth_type,
            'payload': data[14:]
        }

    def parse_vlan(self, payload):
        vlan_hdr = struct.unpack("!HH", payload[:4])
        vlan_id = vlan_hdr[0] & 0x0FFF
        eth_type = vlan_hdr[1]
        return vlan_id, eth_type, payload[4:]

    def parse_ip(self, payload):
        try:
            version = payload[0] >> 4
            if version == 4:
                ihl = payload[0] & 0xF
                src = socket.inet_ntoa(payload[12:16])
                dst = socket.inet_ntoa(payload[16:20])
                proto = payload[9]
                return 'IPv4', src, dst, proto, payload[ihl * 4:]
            elif version == 6:
                src = socket.inet_ntop(socket.AF_INET6, payload[8:24])
                dst = socket.inet_ntop(socket.AF_INET6, payload[24:40])
                proto = payload[6]
                return 'IPv6', src, dst, proto, payload[40:]
        except Exception as e:
            logger.error(f"[ERROR] IP 解析失败: {e}")
        return None, None, None, None, None

    def parse_transport(self, proto, payload):
        try:
            if proto == 6 or proto == 17:
                if len(payload) < 4:
                    return None, None, 0
                sport, dport = struct.unpack("!HH", payload[:4])
                flags = payload[13] if proto == 6 and len(payload) > 13 else 0
                return sport, dport, flags
        except Exception as e:
            logger.error(f"[ERROR] 传输层解析失败: {e}")
        return None, None, 0

    def get_five_tuple(self, eth, ip_ver, ip_src, ip_dst, sport, dport, proto):
        return FiveTuple(
            mac_src=eth['src'], mac_dst=eth['dst'],
            ip_src=ip_src, ip_dst=ip_dst,
            sport=sport, dport=dport,
            proto='TCP' if proto == 6 else 'UDP' if proto == 17 else 'OTHER'
        )

    def get_direction(self, key, proto, flags, timestamp):
        if key in self.flow_table:
            # 如果此特定方向的流已存在，直接返回其方向
            return self.flow_table[key].direction

        # 否则，这是此特定方向的新流。现在判断双向流是否为新流。
        direction = 'uplink'  # 默认方向，如果后续判断无法明确则使用此值

        # 1. 优先使用用户提供的 client_ip_hint 和 server_ip_hint
        if self.client_ip_hint and self.server_ip_hint:
            if key.ip_src == self.client_ip_hint and key.ip_dst == self.server_ip_hint:
                direction = 'uplink'
            elif key.ip_src == self.server_ip_hint and key.ip_dst == self.client_ip_hint:
                direction = 'downlink'
            else:
                # 如果IP不匹配hint，则记录警告并回退到其他判断逻辑
                logger.debug(f"[WARN] 流 {key} 的IP与客户端/服务器提示不匹配。尝试其他判断。")

        # 2. 如果没有提供hint，或者IP不匹配hint，尝试根据TCP SYN/ACK判断
        # TCP SYN包通常是上行（客户端发起连接）
        elif proto == 6 and (flags & 0x02 and not (flags & 0x10)):  # SYN flag set, ACK flag not set
            direction = 'uplink'

        # 3. 如果以上都无法判断，检查反向流是否已存在，并根据其方向推断
        else:
            # 构造反向五元组，用于查找是否已存在反向流
            reversed_key = FiveTuple(
                mac_src=key.mac_dst, mac_dst=key.mac_src,
                ip_src=key.ip_dst, ip_dst=key.ip_src,
                sport=key.dport, dport=key.sport,
                proto=key.proto
            )
            if reversed_key in self.flow_table:
                # 如果反向流是uplink，则当前流是downlink
                if self.flow_table[reversed_key].direction == 'uplink':
                    direction = 'downlink'
                # 如果反向流是downlink，则当前流是uplink
                elif self.flow_table[reversed_key].direction == 'downlink':
                    direction = 'uplink'
            # else: 保持默认的 'uplink' (即新流的第一个包，如果无法通过上述方式判断，则视为上行)

        # 在将当前方向的流信息添加到 flow_table 之前，判断是否是新的双向流
        reversed_key = FiveTuple(
            mac_src=key.mac_dst, mac_dst=key.mac_src,
            ip_src=key.ip_dst, ip_dst=key.ip_src,
            sport=key.dport, dport=key.sport,
            proto=key.proto
        )

        is_bidirectional_flow_new = False
        if reversed_key not in self.flow_table:  # 检查反向流是否存在
            is_bidirectional_flow_new = True

        self.flow_table[key] = FlowInfo(direction, timestamp)

        if is_bidirectional_flow_new:
            self.flow_count += 1
            if self.verbose:
                logger.info(f"[FLOW] 新建双向流 {key} (方向: {direction})")
        else:
            if self.verbose:
                logger.info(f"[FLOW] 现有双向流的新方向 {key} (方向: {direction})")

        return self.flow_table[key].direction

    def add_or_modify_vlan(self, eth_bytes, vlan_id):
        dst = eth_bytes[0:6]
        src = eth_bytes[6:12]
        eth_type = eth_bytes[12:14]
        payload = eth_bytes[14:]

        if vlan_id is None:
            return eth_bytes

        if eth_type == b'\x81\x00':
            new_header = dst + src + eth_type + struct.pack("!H", vlan_id) + payload[4:]
        else:
            new_header = dst + src + b'\x81\x00' + struct.pack("!H", vlan_id) + eth_type + payload
        return new_header

    def checksum(self, data):
        if len(data) % 2:
            data += b'\x00'
        s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
        s = (s >> 16) + (s & 0xffff)
        s += s >> 16
        return ~s & 0xffff

    def is_ipv4_address(self, addr):
        try:
            socket.inet_aton(addr)
            return True
        except OSError:
            return False

    def is_ipv6_address(self, addr):
        try:
            socket.inet_pton(socket.AF_INET6, addr)
            return True
        except OSError:
            return False

    def build_modified_packet(self, eth_bytes, flow_info, direction):
        try:
            eth_header = eth_bytes[:14]
            payload = eth_bytes[14:]
            version = payload[0] >> 4

            # 目标类型判断
            target_ip_src = flow_info.modified_ip_src
            target_ip_dst = flow_info.modified_ip_dst
            target_is_ipv4 = (target_ip_src and self.is_ipv4_address(target_ip_src)) or (
                        target_ip_dst and self.is_ipv4_address(target_ip_dst))
            target_is_ipv6 = (target_ip_src and self.is_ipv6_address(target_ip_src)) or (
                        target_ip_dst and self.is_ipv6_address(target_ip_dst))

            # ========== IPv4 ==========
            if version == 4 and target_is_ipv4:
                ihl = (payload[0] & 0x0F) * 4
                ip_header = bytearray(payload[:ihl])
                ip_payload = payload[ihl:]
                proto = ip_header[9]

                # 修改IP
                if target_ip_src:
                    ip_header[12:16] = socket.inet_aton(target_ip_src)
                if target_ip_dst:
                    ip_header[16:20] = socket.inet_aton(target_ip_dst)

                # TCP/UDP/SCTP/ICMP
                if proto in (6, 17, 132):  # TCP, UDP, SCTP
                    trans_header = bytearray(ip_payload[:4])
                    if flow_info.modified_sport is not None:
                        trans_header[0:2] = struct.pack("!H", flow_info.modified_sport)
                    if flow_info.modified_dport is not None:
                        trans_header[2:4] = struct.pack("!H", flow_info.modified_dport)
                    trans_full = trans_header + ip_payload[4:]
                elif proto == 1:  # ICMP
                    # ICMP无端口，支持type/code/identifier修改可扩展
                    trans_full = ip_payload
                elif proto == 47:  # GRE
                    # GRE头部处理可扩展
                    trans_full = ip_payload
                else:
                    trans_full = ip_payload

                # IP校验和
                ip_header[10:12] = b'\x00\x00'
                ip_header[10:12] = struct.pack("!H", self.checksum(ip_header))

                # TCP/UDP/SCTP/ICMP校验和
                if proto in (6, 17, 132):
                    pseudo = ip_header[12:20] + b'\x00' + bytes([proto]) + struct.pack("!H", len(trans_full))
                    if proto == 6:  # TCP
                        trans_full = bytearray(trans_full)
                        trans_full[16:18] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[16:18] = struct.pack("!H", chksum)
                    elif proto == 17:  # UDP
                        trans_full = bytearray(trans_full)
                        trans_full[6:8] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[6:8] = struct.pack("!H", chksum)
                    elif proto == 132:  # SCTP
                        # SCTP校验和为CRC32C，需专门实现
                        pass
                    new_payload = ip_header + trans_full
                elif proto == 1:  # ICMP
                    # ICMP校验和
                    trans_full = bytearray(trans_full)
                    trans_full[2:4] = b'\x00\x00'
                    chksum = self.checksum(trans_full)
                    trans_full[2:4] = struct.pack("!H", chksum)
                    new_payload = ip_header + trans_full
                else:
                    new_payload = ip_header + trans_full

                return eth_header + new_payload

            # ========== IPv6 ==========
            elif version == 6 and target_is_ipv6:
                ip6_header = bytearray(payload[:40])
                ip6_payload = payload[40:]
                next_header = ip6_header[6]

                # 修改IP
                if target_ip_src:
                    ip6_header[8:24] = socket.inet_pton(socket.AF_INET6, target_ip_src)
                if target_ip_dst:
                    ip6_header[24:40] = socket.inet_pton(socket.AF_INET6, target_ip_dst)

                # TCP/UDP/SCTP/ICMPv6
                if next_header in (6, 17, 132):  # TCP, UDP, SCTP
                    trans_header = bytearray(ip6_payload[:4])
                    if flow_info.modified_sport is not None:
                        trans_header[0:2] = struct.pack("!H", flow_info.modified_sport)
                    if flow_info.modified_dport is not None:
                        trans_header[2:4] = struct.pack("!H", flow_info.modified_dport)
                    trans_full = trans_header + ip6_payload[4:]
                elif next_header == 58:  # ICMPv6
                    trans_full = ip6_payload
                elif next_header == 47:  # GRE
                    trans_full = ip6_payload
                else:
                    trans_full = ip6_payload

                # TCP/UDP/SCTP/ICMPv6校验和
                if next_header in (6, 17, 132):
                    pseudo = (
                            ip6_header[8:24] + ip6_header[24:40] +
                            struct.pack("!I", len(trans_full)) +
                            b'\x00' * 3 + bytes([next_header])
                    )
                    if next_header == 6:
                        trans_full = bytearray(trans_full)
                        trans_full[16:18] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[16:18] = struct.pack("!H", chksum)
                    elif next_header == 17:
                        trans_full = bytearray(trans_full)
                        trans_full[6:8] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[6:8] = struct.pack("!H", chksum)
                    elif next_header == 132:
                        # SCTP校验和为CRC32C，需专门实现
                        pass
                    new_payload = ip6_header + trans_full
                elif next_header == 58:  # ICMPv6
                    trans_full = bytearray(trans_full)
                    trans_full[2:4] = b'\x00\x00'
                    chksum = self.checksum(trans_full)
                    trans_full[2:4] = struct.pack("!H", chksum)
                    new_payload = ip6_header + trans_full
                else:
                    new_payload = ip6_header + trans_full

                return eth_header + new_payload

            # ========== IPv6 -> IPv4 ==========
            elif version == 6 and target_is_ipv4:
                # 只处理TCP/UDP/SCTP/ICMPv6
                ip6_header = payload[:40]
                ip6_payload = payload[40:]
                next_header = ip6_header[6]
                if next_header not in (6, 17, 132, 58):
                    return eth_bytes
                # 端口/ICMP字段
                if next_header in (6, 17, 132):
                    sport, dport = struct.unpack("!HH", ip6_payload[:4])
                    sport = flow_info.modified_sport or sport
                    dport = flow_info.modified_dport or dport
                # 构造IPv4头
                total_length = 20 + len(ip6_payload)
                ipv4_header = bytearray(20)
                ipv4_header[0] = 0x45
                ipv4_header[2:4] = struct.pack("!H", total_length)
                ipv4_header[8] = 64
                ipv4_header[9] = next_header
                ipv4_header[12:16] = socket.inet_aton(flow_info.modified_ip_src)
                ipv4_header[16:20] = socket.inet_aton(flow_info.modified_ip_dst)
                ipv4_header[10:12] = b'\x00\x00'
                ipv4_header[10:12] = struct.pack("!H", self.checksum(ipv4_header))
                # TCP/UDP/SCTP/ICMP
                if next_header in (6, 17, 132):
                    trans_header = bytearray(ip6_payload[:4])
                    trans_header[0:2] = struct.pack("!H", sport)
                    trans_header[2:4] = struct.pack("!H", dport)
                    trans_full = trans_header + ip6_payload[4:]
                    pseudo = ipv4_header[12:20] + b'\x00' + bytes([next_header]) + struct.pack("!H", len(trans_full))
                    if next_header == 6:
                        trans_full[16:18] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[16:18] = struct.pack("!H", chksum)
                    elif next_header == 17:
                        trans_full[6:8] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[6:8] = struct.pack("!H", chksum)
                    elif next_header == 132:
                        # SCTP校验和为CRC32C，需专门实现
                        pass
                    new_payload = ipv4_header + trans_full
                elif next_header == 58:  # ICMPv6转ICMP
                    # 这里只能简单转发，复杂转换需协议适配
                    new_payload = ipv4_header + ip6_payload
                else:
                    new_payload = ipv4_header + ip6_payload
                return eth_header + new_payload

            # ========== IPv4 -> IPv6 ==========
            elif version == 4 and target_is_ipv6:
                ip_header = payload[:20]
                ip_payload = payload[20:]
                proto = ip_header[9]
                if proto not in (6, 17, 132, 1):
                    return eth_bytes
                if proto in (6, 17, 132):
                    sport, dport = struct.unpack("!HH", ip_payload[:4])
                    sport = flow_info.modified_sport or sport
                    dport = flow_info.modified_dport or dport
                # 构造IPv6头
                ipv6_header = bytearray(40)
                ipv6_header[0] = 0x60
                ipv6_header[6] = proto
                ipv6_header[7] = 64
                ipv6_header[4:6] = struct.pack("!H", len(ip_payload))
                ipv6_header[8:24] = socket.inet_pton(socket.AF_INET6, flow_info.modified_ip_src)
                ipv6_header[24:40] = socket.inet_pton(socket.AF_INET6, flow_info.modified_ip_dst)
                if proto in (6, 17, 132):
                    trans_header = bytearray(ip_payload[:4])
                    trans_header[0:2] = struct.pack("!H", sport)
                    trans_header[2:4] = struct.pack("!H", dport)
                    trans_full = trans_header + ip_payload[4:]
                    pseudo = (
                            ipv6_header[8:24] + ipv6_header[24:40] +
                            struct.pack("!I", len(trans_full)) +
                            b'\x00' * 3 + bytes([proto])
                    )
                    if proto == 6:
                        trans_full[16:18] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[16:18] = struct.pack("!H", chksum)
                    elif proto == 17:
                        trans_full[6:8] = b'\x00\x00'
                        chksum = self.checksum(pseudo + trans_full)
                        trans_full[6:8] = struct.pack("!H", chksum)
                    elif proto == 132:
                        # SCTP校验和为CRC32C，需专门实现
                        pass
                    new_payload = ipv6_header + trans_full
                elif proto == 1:  # ICMP转ICMPv6
                    new_payload = ipv6_header + ip_payload
                else:
                    new_payload = ipv6_header + ip_payload
                return eth_header + new_payload

            else:
                return eth_bytes

        except Exception as e:
            logger.error(f"[ERROR] build_modified_packet失败: {e}")
            return eth_bytes

    def modify_packet(self, eth_bytes, flow_key, direction):
        """
        根据流方向决定是否需要修改五元组字段，并调用 build_modified_packet。
        - direction: 'uplink' 表示上行（通常是会话发起方），'downlink' 表示下行（会话响应方）。
        - 按照"对称换向"原则：
            * 上行包：源IP/端口用 force_ip_src/force_sport，目的IP/端口用 force_ip_dst/force_dport
            * 下行包：源IP/端口用 force_ip_dst/force_dport，目的IP/端口用 force_ip_src/force_sport
        - 如果所有force参数都为None，则直接返回原始包，不做任何修改。
        """
        if flow_key is None:
            return eth_bytes
        if all(x is None for x in [self.force_ip_src, self.force_ip_dst, self.force_sport, self.force_dport]):
            return eth_bytes
        flow_info = self.flow_table[flow_key]
        try:
            eth = self.parse_ethernet(eth_bytes)
            payload = eth['payload']
            if eth['eth_type'] == 0x8100:
                _, eth_type, payload = self.parse_vlan(payload)
            else:
                eth_type = eth['eth_type']
            if eth_type not in (0x0800, 0x86DD):
                return eth_bytes
            ip_ver, ip_src, ip_dst, proto, trans_payload = self.parse_ip(payload)
            sport, dport, _ = self.parse_transport(proto, trans_payload)
            ip_version = 4 if ip_ver == 'IPv4' else 6
            flow_info.ip_version = ip_version

            # 默认用原始五元组
            mod_ip_src = ip_src
            mod_ip_dst = ip_dst
            mod_sport = sport
            mod_dport = dport

            # 按"对称换向"原则赋值
            if direction == 'uplink':
                if self.force_ip_src:
                    mod_ip_src = self.force_ip_src
                if self.force_ip_dst:
                    mod_ip_dst = self.force_ip_dst
                if self.force_sport:
                    mod_sport = self.force_sport
                if self.force_dport:
                    mod_dport = self.force_dport
            elif direction == 'downlink':
                if self.force_ip_src:
                    mod_ip_dst = self.force_ip_src
                if self.force_ip_dst:
                    mod_ip_src = self.force_ip_dst
                if self.force_sport:
                    mod_dport = self.force_sport
                if self.force_dport:
                    mod_sport = self.force_dport

            # 临时存入flow_info，供build_modified_packet使用
            flow_info.modified_ip_src = mod_ip_src
            flow_info.modified_ip_dst = mod_ip_dst
            flow_info.modified_sport = mod_sport
            flow_info.modified_dport = mod_dport

            return self.build_modified_packet(eth_bytes, flow_info, direction)
        except Exception as e:
            logger.error(f"[ERROR] 修改数据包失败: {e}")
            return eth_bytes

    def _setup_sockets(self):
        """
        初始化并缓存 socket，只创建一次，提升发包效率。
        """
        if self._uplink_socket is None:
            self._uplink_socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            self._uplink_socket.bind((self.uplink_iface, 0))
        if self._downlink_socket is None and self.uplink_iface != self.downlink_iface:
            self._downlink_socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            self._downlink_socket.bind((self.downlink_iface, 0))

    def _cleanup_sockets(self):
        """
        关闭 socket，释放资源。
        """
        if self._uplink_socket:
            self._uplink_socket.close()
            self._uplink_socket = None
        if self._downlink_socket:
            self._downlink_socket.close()
            self._downlink_socket = None

    def send_packet(self, raw_pkt, direction, key, original_packet_length, timestamp_for_cache):
        iface = self.uplink_iface if direction == 'uplink' else self.downlink_iface
        vlan_id = self.uplink_vlan if direction == 'uplink' else self.downlink_vlan

        pkt = self.modify_packet(raw_pkt, key, direction)
        pkt = self.add_or_modify_vlan(pkt, vlan_id)

        try:
            # 优化：复用 socket
            if direction == 'uplink':
                s = self._uplink_socket
            else:
                s = self._downlink_socket if self._downlink_socket else self._uplink_socket

            s.send(pkt)

            # 缓存发送的包
            if self._cache_enabled:
                if self.uplink_iface != self.downlink_iface:
                    # 缓存到两个文件
                    if direction == 'uplink' and self._uplink_pcap_writer:
                        self._write_pcap_packet(self._uplink_pcap_writer, pkt, timestamp_for_cache)
                    elif direction == 'downlink' and self._downlink_pcap_writer:
                        self._write_pcap_packet(self._downlink_pcap_writer, pkt, timestamp_for_cache)
                elif self._single_pcap_writer:
                    # 缓存到一个文件
                    self._write_pcap_packet(self._single_pcap_writer, pkt, timestamp_for_cache)

        except Exception as e:
            logger.error(f"[ERROR] 发送数据包失败: {e}")
            return

        if direction == 'uplink':
            self.uplink_packets += 1
            self.uplink_bytes += max(original_packet_length, 60)
        else:
            self.downlink_packets += 1
            self.downlink_bytes += max(original_packet_length, 60)
        self.total_bytes += max(original_packet_length, 60)

    def replay_file(self, file_path):
        packets_count = 0
        logger.info(f"[INFO] 开始回放文件: {file_path}")

        if not os.path.isfile(file_path):
            raise RuntimeError(f"pcap不存在：{file_path}")

        with open(file_path, 'rb') as f:
            f.read(24)  # 跳过pcap文件头
            last_send_time = time.monotonic()
            bits_per_second = self.mbps * 1_000_000

            while True:
                pkt_hdr = f.read(16)
                if not pkt_hdr:
                    break
                ts_sec, ts_usec, incl_len, orig_len = struct.unpack('=IIII', pkt_hdr)
                pkt_data = f.read(incl_len)
                timestamp = ts_sec + ts_usec / 1_000_000

                try:
                    eth = self.parse_ethernet(pkt_data)
                    payload = eth['payload']
                    if eth['eth_type'] == 0x8100:
                        _, eth_type, payload = self.parse_vlan(payload)
                    else:
                        eth_type = eth['eth_type']

                    if eth_type not in (0x0800, 0x86DD):
                        continue  # 只处理IPv4/IPv6包

                    ip_ver, ip_src, ip_dst, proto, trans_payload = self.parse_ip(payload)
                    if not ip_src:
                        continue

                    # 新增：如果不强制建流，且为"无方向+不做五元组修改"场景，直接发包，不建流
                    if self.directionless and self.no_tuple_modification and not self.force_build_flow:
                        self.send_packet(pkt_data, 'uplink', None, orig_len, timestamp)
                        packets_count += 1
                        # --- 每包速率控制 ---
                        if self.mbps > 0:
                            delay = (incl_len * 8) / bits_per_second
                            elapsed = time.monotonic() - last_send_time
                            if delay > elapsed:
                                time.sleep(delay - elapsed)
                            last_send_time = time.monotonic()
                        continue

                    # 需要建流和方向判定的场景
                    sport, dport, flags = self.parse_transport(proto, trans_payload)
                    key = self.get_five_tuple(eth, ip_ver, ip_src, ip_dst, sport, dport, proto)
                    direction = self.get_direction(key, proto, flags, timestamp)
                    self.send_packet(pkt_data, direction, key, orig_len, timestamp)

                    packets_count += 1
                    # --- 每包速率控制 ---
                    if self.mbps > 0:
                        delay = (incl_len * 8) / bits_per_second
                        elapsed = time.monotonic() - last_send_time
                        if delay > elapsed:
                            time.sleep(delay - elapsed)
                        last_send_time = time.monotonic()
                except Exception as e:
                    logger.warning(f"[WARN] Error processing packet: {e}")

        logger.info(f"[INFO] 文件回放完成: {file_path}")
        return packets_count

    def replay_files(self, file_paths):
        self.reset_stats()
        self._setup_pcap_caching()
        self._setup_sockets()  # 新增：初始化 socket
        self.start_time = time.time()
        total_bytes_sent = 0  # 新增：全局已发送字节数
        for file_path in file_paths:
            packets_before = self.total_packets
            bytes_before = self.total_bytes
            packets = self.replay_file(file_path)
            self.total_files += 1
            self.total_packets += packets
            # 统计本文件发送的字节数
            bytes_sent_this_file = self.total_bytes - bytes_before
            total_bytes_sent += bytes_sent_this_file
            # 全局速率控制：确保整体速率不超标
            if self.mbps > 0:
                elapsed = time.time() - self.start_time
                should_elapsed = (total_bytes_sent * 8) / (self.mbps * 1_000_000)
                if should_elapsed > elapsed:
                    time.sleep(should_elapsed - elapsed)
        self.end_time = time.time()
        self._cleanup_pcap_caching()
        self._cleanup_sockets()  # 新增：关闭 socket

        duration = self.end_time - self.start_time
        actual_mbps = (self.total_bytes * 8 / 1_000_000) / duration if duration > 0 else 0
        stats = {
            'total_files': self.total_files,
            'total_packets': self.total_packets,
            'total_bytes': self.total_bytes,
            'uplink_packets': self.uplink_packets,
            'uplink_bytes': self.uplink_bytes,
            'downlink_packets': self.downlink_packets,
            'downlink_bytes': self.downlink_bytes,
            'flow_count': self.flow_count,
            'duration_seconds': round(duration, 2),
            'target_mbps': self.mbps,
            'actual_mbps': round(actual_mbps, 2)
        }
        logger.info(f"[STATS] {stats}")
        return stats

    def _write_pcap_global_header(self, file_handle):
        # Standard pcap global header
        # magic_number (0xA1B2C3D4 for microsecond, 0xD4C3B2A1 for nanosecond)
        # version_major (2)
        # version_minor (4)
        # thiszone (0 for GMT)
        # sigfigs (0)
        # snaplen (65535 for no snap)
        # network (1 for Ethernet)
        global_header = struct.pack(
            "!LHHIIII",
            0xA1B2C3D4,  # magic_number (microsecond resolution)
            2,  # version_major
            4,  # version_minor
            0,  # thiszone (GMT)
            0,  # sigfigs
            65535,  # snaplen (max length of captured packets)
            1  # network (Ethernet)
        )
        file_handle.write(global_header)
        self._pcap_global_header = global_header  # 缓存起来，以防需要重新创建文件

    def _write_pcap_packet(self, file_handle, packet_data, timestamp):
        # pcap packet header
        ts_sec = int(timestamp)
        ts_usec = int((timestamp - ts_sec) * 1_000_000)
        incl_len = len(packet_data)
        orig_len = len(packet_data)
        packet_header = struct.pack(
            "!IIII",
            ts_sec,
            ts_usec,
            incl_len,
            orig_len
        )
        file_handle.write(packet_header)
        file_handle.write(packet_data)

    def _setup_pcap_caching(self):
        if not self._cache_enabled:
            return

        os.makedirs(self._cache_dir, exist_ok=True)
        logger.info(f"[INFO] Pcap 缓存目录已准备: {self._cache_dir}")

        if self.uplink_iface != self.downlink_iface:
            # 上行和下行接口不同，缓存到两个文件
            uplink_pcap_path = os.path.join(self._cache_dir, "uplink.pcap")
            downlink_pcap_path = os.path.join(self._cache_dir, "downlink.pcap")

            try:
                self._uplink_pcap_writer = open(uplink_pcap_path, 'wb')
                self._write_pcap_global_header(self._uplink_pcap_writer)
                logger.info(f"[INFO] 上行 pcap 缓存文件已创建: {uplink_pcap_path}")
            except IOError as e:
                logger.error(f"[ERROR] 无法创建上行 pcap 缓存文件: {e}")
                self._uplink_pcap_writer = None

            try:
                self._downlink_pcap_writer = open(downlink_pcap_path, 'wb')
                self._write_pcap_global_header(self._downlink_pcap_writer)
                logger.info(f"[INFO] 下行 pcap 缓存文件已创建: {downlink_pcap_path}")
            except IOError as e:
                logger.error(f"[ERROR] 无法创建下行 pcap 缓存文件: {e}")
                self._downlink_pcap_writer = None
        else:
            # 上行和下行接口相同，缓存到一个文件
            single_pcap_path = os.path.join(self._cache_dir, "combined.pcap")
            try:
                self._single_pcap_writer = open(single_pcap_path, 'wb')
                self._write_pcap_global_header(self._single_pcap_writer)
                logger.info(f"[INFO] 单一 pcap 缓存文件已创建: {single_pcap_path}")
            except IOError as e:
                logger.error(f"[ERROR] 无法创建单一 pcap 缓存文件: {e}")
                self._single_pcap_writer = None

    def _cleanup_pcap_caching(self):
        if not self._cache_enabled:
            return

        if self._uplink_pcap_writer:
            self._uplink_pcap_writer.close()
            logger.info("[INFO] 上行 pcap 缓存文件已关闭。")
        if self._downlink_pcap_writer:
            self._downlink_pcap_writer.close()
            logger.info("[INFO] 下行 pcap 缓存文件已关闭。")
        if self._single_pcap_writer:
            self._single_pcap_writer.close()
            logger.info("[INFO] 单一 pcap 缓存文件已关闭。")

