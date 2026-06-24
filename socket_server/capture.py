import os
import time
import struct
import logging
import threading
from scapy.all import sniff, wrpcap

logger = logging.getLogger(__name__)

# 全局变量 抓包工具命令
_sniff_command = None

# 全局变量 CPU绑定记录
_nic_cpu_binding = {}
_allocated_cpus = set()
_cpu_binding_lock = threading.Lock()


from .netutils import routeinfo, isfile, wait_not_until, wait_until, exec_cmd_subprocess
def tcpdump_stop(path="/home/tmp/tmp.pcap"):
    global _sniff_command
    # cmd = "kill -SIGINT `ps -ef|grep tcpdump|grep '%s'|grep -v grep|awk '{print $2}'`" % path
    if _sniff_command == "tcpdump":
        cmd = "pkill -SIGINT -f 'tcpdump.*%s'" % path
    elif _sniff_command == "dumpcap":
        cmd = "pkill -SIGINT -f 'dumpcap.*%s'" % path
    else:
        raise RuntimeError("请检查系统是否存在命令：dumpcap 或者 tcpdump")
    response = exec_cmd_subprocess(args=cmd)
    if response["code"]:
        return False
    if isfile(path):
        if wait_not_until(os.path.getsize, expect_value="0", step=1, timeout=20, filename=path) and wait_until(
                os.access, expect_value=True, step=1, timeout=20, path=path, mode=os.W_OK):
            return True
        else:
            return False
    else:
        return True

def tcpdump_isrun(path="/home/tmp/tmp.pcap"):
    global _sniff_command
    if _sniff_command == "tcpdump":
        cmd = "ps -ef|grep tcpdump|grep '%s'|grep -v grep|awk '{print $2}'" % path
    elif _sniff_command == "dumpcap":
        cmd = "ps -ef|grep dumpcap|grep '%s'|grep -v grep|awk '{print $2}'" % path
    else:
        raise RuntimeError("请检查系统是否存在命令：dumpcap 或者 tcpdump")
    response = exec_cmd_subprocess(args=cmd)
    if response["code"]:
        return False
    elif response["stdout"].strip():
        return True
    else:
        return False

def tcpdump_start(eth=None, path="/home/tmp/tmp.pcap", extended="", single_queue=True):
    global _sniff_command
    # if not ensure_command("tcpdump"):
    #     raise RuntimeError("请检查系统是否存在命令：tcpdump")
    if not eth:
        eth = routeinfo()["0.0.0.0"]["Iface"]
    # tcpdump_stop(path)
    # mtu(eth,2000)
    # 首次抓包需要配置网卡单队列模式
    bound_cpu = None
    if single_queue:
        logger.info(f"配置网卡 {eth} 为单队列模式")
        sq_handler = SingleQueueRxThread(eth=eth, count=1)
        bound_cpu = sq_handler.bound_cpu  # 获取绑定的CPU号
    else:
        logger.info(f"跳过网卡 {eth} 单队列配置")

    cmd = f"rm -rf {path}"
    exec_cmd_subprocess(args=cmd)
    if _sniff_command == "tcpdump":
        cmd = "sudo tcpdump -i %s -w %s %s -Z root" % (eth, path, extended)
    elif _sniff_command == "dumpcap":
        cmd = "sudo dumpcap -i %s -w %s -f '%s'" % (eth, path, extended)
    else:
        raise RuntimeError("请检查系统是否存在命令：dumpcap 或者 tcpdump")

    # 使用taskset绑定抓包进程到对应CPU，避免跨CPU数据传输导致乱序
    if bound_cpu is not None:
        cmd = f"taskset -c {bound_cpu} {cmd}"
        logger.info(f"抓包进程绑定到 CPU {bound_cpu}")

    logger.info(cmd)
    exec_cmd_subprocess(args=cmd, wait=False)
    time.sleep(2)
    if tcpdump_isrun(path=path):
        return True
    else:
        return False

class Tcpdump_scapy:
    def __init__(self, iface, filter=None, path=None, timeout=5):  # 初始化有__不知道为啥不显示
        self.path = path
        self.iface = iface  # 本地网卡名
        self.filter = filter  # 过滤条件
        self.timeout = timeout
        self.e = False
        self.pkts = list()
        if not self.iface:
            self.iface = routeinfo()["0.0.0.0"]["Iface"]

    def _sniff(self):
        self.e = False
        # self.pkts = sniff(iface=self.iface, count=0, prn=lambda x: x.sprintf('{IP:%IP.src%->%IP.dst%}'),filter=self.filter, stop_filter=lambda x: self.e, timeout=self.timeout) # 进行抓包操作
        self.pkts = sniff(iface=self.iface, count=0, prn=lambda x: x.sprintf('{IP:%IP.src%->%IP.dst%}'),
                          filter=self.filter, stop_filter=lambda x: self.e, timeout=self.timeout)  # 进行抓包操作
        if self.path:
            wrpcap(self.path, self.pkts)

    def start(self):  # 开始抓包
        self.mythread = threading.Thread(target=self._sniff)
        self.mythread.start()
        # self.mythread.join()

    def stop(self):
        self.e = True
        t1 = time.time()
        while time.time() - t1 < 30:
            if self.mythread.is_alive():
                logger.info("停止抓包进程，进制存活状态:%s" % self.mythread.is_alive())
                time.sleep(1)
            else:
                return
        raise RuntimeError("停止抓包进程失败！")

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
