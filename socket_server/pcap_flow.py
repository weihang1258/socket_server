import os
import struct
import socket
import logging

logger = logging.getLogger(__name__)

# HTTP 请求方法关键字（用于 HTTP 方向识别）
_HTTP_REQUEST_METHODS = (
    b"GET ", b"POST ", b"PUT ", b"HEAD ", b"DELETE ",
    b"OPTIONS ", b"PATCH ", b"CONNECT ", b"TRACE ",
)


def extract_pcap_flow_five_tuples(pcap_dir):
    """提取 pcap 目录下所有 pcap 文件的方向化五元组流。

    全包遍历每个 pcap（纯 struct 解析，不依赖 scapy），按双向流分组，
    按以下规则确定每条流的方向（每条流至多调整一次，确定后即锁定）：
      1. 默认方向：首包 src→dst（首包 src 视为客户端）
      2. TCP 三次握手修正：
         - SYN 包（SYN=1, ACK=0）→ SYN 发起方为客户端
         - SYN+ACK 包（SYN=1, ACK=1）→ SYN+ACK 接收方为客户端
      3. HTTP 修正（TCP 载荷）：
         - 请求方法开头（GET/POST/...）→ 请求方为客户端
         - HTTP/1. 响应行开头 → 响应接收方为客户端

    一旦方向被任一信号确定即锁定，后续信号不再调整。

    Args:
        pcap_dir: pcap 文件所在目录（递归查找 *.pcap）

    Returns:
        list[dict]: 每条流一项，方向化后 src=客户端：
            {pcap, srcIp, srcPort, destIp, destPort, protoType}
            protoType: 1=TCP, 2=UDP, 3=SCTP, 4=ICMP
            ICMP 无端口，srcPort/destPort 为 "0"
    """
    results = []

    pcap_files = []
    if os.path.isdir(pcap_dir):
        for root, _, files in os.walk(pcap_dir):
            for name in files:
                if name.endswith(".pcap"):
                    pcap_files.append(os.path.join(root, name))

    for pcap_path in pcap_files:
        pcap_name = os.path.basename(pcap_path)
        flows = {}
        try:
            with open(pcap_path, 'rb') as f:
                f.read(24)  # 跳过 pcap 全局头
                while True:
                    pkt_hdr = f.read(16)
                    if len(pkt_hdr) < 16:
                        break
                    _, _, incl_len, _ = struct.unpack('=IIII', pkt_hdr)
                    pkt_data = f.read(incl_len)
                    if len(pkt_data) < incl_len:
                        break

                    if len(pkt_data) < 14:
                        continue
                    eth_type = struct.unpack("!H", pkt_data[12:14])[0]
                    payload = pkt_data[14:]
                    if eth_type == 0x8100:  # VLAN
                        if len(payload) < 4:
                            continue
                        eth_type = struct.unpack("!H", payload[2:4])[0]
                        payload = payload[4:]

                    if eth_type == 0x0800:  # IPv4
                        if len(payload) < 20:
                            continue
                        ihl = (payload[0] & 0x0F) * 4
                        proto = payload[9]
                        ip_src = socket.inet_ntoa(payload[12:16])
                        ip_dst = socket.inet_ntoa(payload[16:20])
                        trans = payload[ihl:]
                    elif eth_type == 0x86DD:  # IPv6
                        if len(payload) < 40:
                            continue
                        proto = payload[6]
                        ip_src = socket.inet_ntop(socket.AF_INET6, payload[8:24])
                        ip_dst = socket.inet_ntop(socket.AF_INET6, payload[24:40])
                        trans = payload[40:]
                    else:
                        continue

                    if proto == 6:
                        proto_type = "1"  # TCP
                    elif proto == 17:
                        proto_type = "2"  # UDP
                    elif proto == 132:
                        proto_type = "3"  # SCTP
                    elif proto in (1, 58):
                        proto_type = "4"  # ICMP / ICMPv6
                    else:
                        continue

                    if proto_type in ("1", "2", "3"):
                        if len(trans) < 4:
                            continue
                        sport, dport = struct.unpack("!HH", trans[:4])
                        sport_s, dport_s = str(sport), str(dport)
                        flags = trans[13] if proto == 6 and len(trans) > 13 else 0
                    else:
                        sport_s, dport_s = "0", "0"
                        flags = 0

                    src_tuple = (ip_src, sport_s)
                    dst_tuple = (ip_dst, dport_s)
                    canonical = tuple(sorted([src_tuple, dst_tuple])) + (proto_type,)

                    flow = flows.get(canonical)
                    if flow is None:
                        flow = {
                            "first_src": src_tuple,
                            "first_dst": dst_tuple,
                            "client": src_tuple,
                            "finalized": False,
                            "proto_type": proto_type,
                        }
                        flows[canonical] = flow

                    if not flow["finalized"]:
                        signaled_client = None
                        if proto == 6:
                            syn = bool(flags & 0x02)
                            ack = bool(flags & 0x10)
                            if syn and not ack:
                                signaled_client = src_tuple
                            elif syn and ack:
                                signaled_client = dst_tuple
                        if signaled_client is None and proto == 6 and len(trans) >= 14:
                            data_offset = (trans[12] >> 4) * 4
                            http_payload = trans[data_offset:]
                            if http_payload.startswith(_HTTP_REQUEST_METHODS):
                                signaled_client = src_tuple
                            elif http_payload.startswith(b"HTTP/1."):
                                signaled_client = dst_tuple
                        if signaled_client is not None:
                            flow["client"] = signaled_client
                            flow["finalized"] = True

            for flow in flows.values():
                if flow["client"] == flow["first_src"]:
                    src_ip, src_port = flow["first_src"]
                    dst_ip, dst_port = flow["first_dst"]
                else:
                    src_ip, src_port = flow["first_dst"]
                    dst_ip, dst_port = flow["first_src"]
                results.append({
                    "pcap": pcap_name,
                    "srcIp": src_ip,
                    "srcPort": src_port,
                    "destIp": dst_ip,
                    "destPort": dst_port,
                    "protoType": flow["proto_type"],
                })
        except Exception as e:
            logger.error(f"extract_pcap_flow_five_tuples 解析 {pcap_path} 失败: {e}")

    return results
