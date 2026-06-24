#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2023/12/6 15:16
# @Author  : weihang
# @File    : socket_server_1.0.py_20241014
import os
import sys
from collections import namedtuple
from pathlib import Path
import queue
import multiprocessing
import socketserver
# pip3 install scapy -i http://10.128.5.124/pypi/web/simple
from multiprocessing import Process, cpu_count, Value, Lock, Queue, Manager
# from tqdm import tqdm
import requests
from scapy.all import *
import json
import struct
import argparse
from io import BytesIO
import logging
import time
import socket
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyppeteer import launch
import traceback
import shutil
import atexit
import subprocess
import urllib.parse


# 添加日志打印
logger = logging.getLogger()
logger.setLevel(logging.INFO)
fh = logging.FileHandler("/var/log/socket_server.log", encoding='utf-8')
fh.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

# 屏蔽 pyppeteer 及 requests 的 DEBUG 日志
logging.getLogger("pyppeteer").setLevel(logging.WARNING)
logging.getLogger("pyppeteer.connection").setLevel(logging.WARNING)
logging.getLogger("pyppeteer.launcher").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

version = "1.2.1"
logger.info(f"version:{version}")

cmd = "dirname `find /usr/local/ -type f -name java|head -n 1`"
path_java = os.popen(cmd).read().strip()
if path_java:
    os.environ["PATH"] = path_java + ":" + os.environ["PATH"]
    os.environ["JAVA_HOME"] = "/usr/local/jdk1.8.0_202"

# 打包chromium驱动
if hasattr(sys, '_MEIPASS'):
    # 在 PyInstaller 打包环境中运行
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(sys._MEIPASS) / "ms-playwright")
else:
    # 普通开发环境运行
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


class MyTCPHandler(socketserver.BaseRequestHandler):

    def setup(self):
        self.filepath = "tmp"
        self.content = b""
        self.bin_recv_flag = False
        self.length = 0

    def handle(self):
        try:
            self.bufsize = 10240
            datatotal = b""
            data_len = 0
            while True:
                try:
                    data = self.request.recv(self.bufsize)
                    if not data:
                        break
                    if data_len == 0:
                        data_len = struct.unpack("i", data[:4])[0]
                        logger.info(f"指定接收有效字节数：{data_len}")
                        datatotal += data[4:]
                        logger.info(f"本条接收长度{len(data[4:])}")
                    else:
                        datatotal += data
                        logger.info(f"本条接收长度{len(data)}")

                    if len(datatotal) < data_len:
                        logger.info(f"接收总长度{len(datatotal)}")
                        continue
                    elif len(datatotal) == data_len:
                        logger.info(f"接收总长度{len(datatotal)}")
                        data = datatotal
                        datatotal = b""
                        data_len = 0
                    else:
                        logger.error(f"收到了字节数大于发送字节数，收到{len(datatotal)},发送{data_len}")
                        datatotal = b""
                        data_len = 0
                        continue

                    logger.debug('->client: %s %s' % (len(data), data[:200].hex()))
                    # print(self.filepath, self.bin_recv_flag, len(self.content), self.length)
                    # 指令传输
                    if not self.bin_recv_flag:
                        datatype = struct.unpack("i", data[:4])[0]
                        data = data[4:]
                    # 文件传输
                    else:
                        datatype = 23

                    # 文件名上传，格式：【{"filepath":filepath}】
                    if datatype in (21,):
                        logger.info("文件名上传:%s %s" % (datatype, data))
                        fileinfo = json.loads(s=data)
                        self.filepath = fileinfo.get("filepath", None)
                        self.gzip = fileinfo.get("gzip", False)
                        self.request.sendall(b"21 ok")
                    elif datatype in (22,):  # 文件长度接收
                        logger.info("文件上传：文件长度%s %s" % (datatype, data.hex()))
                        self.length = struct.unpack("<Q", data)[0]
                        self.content = b""
                        self.bin_recv_flag = True  # 进入二进制文件接收状态
                        self.bufsize = 102400000
                        self.request.sendall(b"22 ok")
                    elif datatype in (23,):  # 文件内容接收
                        logger.info("文件内容上传，类型：%s" % datatype)
                        self.content += data
                        if len(self.content) == self.length:
                            self.bufsize = 1024
                            self.bin_recv_flag = False
                            self.request.sendall(b"23 ok")
                            continue
                        elif len(self.content) > self.length:
                            self.bufsize = 1024
                            self.bin_recv_flag = False
                            # logger.debug(self.content[-5:].hex())
                            raise RuntimeError(
                                f"接收字节数大于原始文件字节数：接收{len(self.content)}，原始{self.length}")
                        else:
                            pass
                    elif datatype in (24,):  # 写文件
                        logger.info("文件上传：写文件，类型：%s" % datatype)
                        str_decompress_gzip = decompress_gzip(self.content) if self.gzip else self.content
                        if str_decompress_gzip == b"^$":
                            str_decompress_gzip = b""
                        with open(self.filepath, "wb") as f:
                            f.write(str_decompress_gzip)
                        self.content = b""
                        self.request.sendall(b"24 ok")
                    res_do = do(datatype, data, filepath=self.filepath)
                    if res_do:
                        logger.info(f"response({len(res_do)})：%s" % res_do[:100].hex())
                        try:
                            self.request.sendall(res_do)
                        except Exception as e:
                            logger.error(f"发送错误：{e}")
                            logger.info("等待3秒，重新发送")
                            time.sleep(3)
                            self.request.sendall(res_do)
                    del res_do, datatype, data
                except ConnectionResetError:
                    break
        except Exception as e:
            logger.error(f"错误：{e}")
        finally:
            self.request.close()


class RequestChecker:
    def __init__(self, url, timeout=3):
        self.url = url
        self.timeout = timeout
        self.total = 0
        self.success = 0
        self.fail = 0
        self.time_consumed = 0

    def run_single(self):
        start = time.time()
        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            r = session.get(self.url, timeout=self.timeout, allow_redirects=False)
            session.close()
            duration = time.time() - start
            self.time_consumed += duration
            self.total += 1
            if r.status_code == 200:
                self.success += 1
            else:
                self.fail += 1
        except Exception as e:
            self.total += 1
            self.fail += 1
            self.time_consumed += time.time() - start

    def run_multiple(self, count, thread_count=1, interval=0):
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = []
            for i in range(count):
                futures.append(executor.submit(self.run_single))
                if interval > 0 and i < count - 1:
                    time.sleep(interval)
            for future in as_completed(futures):
                pass

    def get_result(self):
        success_ratio = (self.success / self.total * 100) if self.total else 0
        return {
            "total": self.total,
            "success": self.success,
            "fail": self.fail,
            "time": round(self.time_consumed, 4),
            "success_ratio": f"{success_ratio:.2f}%"
        }

def ensure_command(cmd: str, install_cmd: str = None):
    """
    确保系统中存在指定命令，如果不存在且提供了安装命令，则尝试自动安装。

    参数:
        cmd (str): 要检测的系统命令名称，例如 "ifconfig"、"curl" 等。
        install_cmd (str): 可选，当 cmd 不存在时要执行的安装命令（如 yum/apt 安装指令）。

    返回:
        True  - 命令存在，或已执行安装指令（不保证安装成功）
        False - 命令不存在，且未提供安装命令
    """

    # 使用 shutil.which 判断命令是否存在于系统 PATH 中
    if shutil.which(cmd):
        print(f"✅ {cmd} 存在")
        return True
    else:
        print(f"❌ {cmd} 不存在")

        # 如果提供了安装命令，尝试自动安装
        if install_cmd:
            print(f"尝试安装：{install_cmd}")
            # 使用 subprocess 运行安装命令，shell=True 允许执行 shell 语法
            subprocess.run(install_cmd, shell=True)
            if shutil.which(cmd):
                print(f"✅ {cmd} 存在")
                return True
        # 返回 False 表示命令不存在（即使已尝试安装，也不确定是否成功）
        return False

# 全局变量 抓包工具命令_sniff_command
_sniff_command = None

# 全局变量 CPU绑定记录（内存中，程序重启自动清空）
_nic_cpu_binding = {}  # 网卡 -> CPU 映射，如 {"eth0": 1, "eth1": 2}
_allocated_cpus = set()  # 已分配的CPU集合
_cpu_binding_lock = threading.Lock()  # 线程锁
if ensure_command("dumpcap"):
    _sniff_command = ("dumpcap")
elif ensure_command("tcpdump"):
    _sniff_command = ("tcpdump")
logger.info(f"使用抓包工具：{_sniff_command}")

# 全局 event loop 和 browser
_global_loop = None
_browser = None
_browser_lock = threading.Lock()
def _create_browser_sync(chromium_path):
    async def _create():
        from pyppeteer import launch
        import subprocess
        return await launch(
            executablePath=chromium_path,
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-dev-shm-usage',
                '--disable-setuid-sandbox',
                '--single-process',
                '--disable-accelerated-2d-canvas',
                '--disable-accelerated-jpeg-decoding',
                '--disable-accelerated-mjpeg-decode',
                '--disable-gpu-compositing',
                '--disable-gpu-sandbox',
            ],
            dumpio=False,
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    return _create

def get_global_loop_and_browser(chromium_path):
    global _global_loop, _browser
    with _browser_lock:
        if _global_loop is None:
            logger.info("[定位] 初始化全局 event loop ...")
            _global_loop = asyncio.new_event_loop()
            threading.Thread(target=_global_loop.run_forever, daemon=True).start()
            logger.info("[定位] event loop 启动完成，准备启动 browser ...")
        if _browser is None:
            # 用 run_coroutine_threadsafe 提交 browser 创建任务
            future = asyncio.run_coroutine_threadsafe(_create_browser_sync(chromium_path)(), _global_loop)
            _browser = future.result()
            logger.info("[定位] browser 启动完成")
    return _global_loop, _browser

class BrowserManager:
    @staticmethod
    def is_browser_closed(browser):
        # 兼容不同 pyppeteer 版本
        if hasattr(browser, "is_closed"):
            return browser.is_closed()
        elif hasattr(browser, "_connection"):
            return browser._connection is None
        return True

    @classmethod
    async def get_browser(cls, chromium_path=None):
        global _browser
        from pyppeteer import launch
        if _browser is None or cls.is_browser_closed(_browser):
            logger.info("[定位] browser未初始化或已关闭，重新启动browser")
            _browser = await launch(
                executablePath=chromium_path,
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--single-process',
                    '--disable-accelerated-2d-canvas',
                    '--disable-accelerated-jpeg-decoding',
                    '--disable-accelerated-mjpeg-decode',
                    '--disable-gpu-compositing',
                    '--disable-gpu-sandbox',
                ],
                dumpio=False,
                handleSIGINT=False,
                handleSIGTERM=False,
                handleSIGHUP=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        return _browser

def _close_browser_on_exit():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(BrowserManager.close_browser())
        else:
            loop.run_until_complete(BrowserManager.close_browser())
    except Exception:
        pass
atexit.register(_close_browser_on_exit)

class PyppeteerChecker:
    def __init__(self, url, chromium_path, timeout=3):
        self.url = url
        self.chromium_path = chromium_path
        self.total = 0
        self.success = 0
        self.fail = 0
        self.time_consumed = 0
        self.timeout = timeout
        self.run_single_exceptions = []  # 新增：记录所有异常
        self.run_single_request_counts = []  # 新增：记录每次run_single的request数
        if not chromium_path or not os.path.isfile(chromium_path):
            logger.error(f"chromium_path 不存在: {chromium_path}")
            raise RuntimeError(f"chromium_path 不存在: {chromium_path}")
        if not os.access(chromium_path, os.X_OK):
            logger.error(f"chromium_path 不可执行: {chromium_path}")
            raise RuntimeError(f"chromium_path 不可执行: {chromium_path}")

    async def run_single(self, mode, retry=False, idx=None):
        import time
        start_time = time.time()
        page = None
        request_count = 0  # 新增：统计本次run_single的request数
        try:
            browser = await BrowserManager.get_browser(self.chromium_path)
            page = await browser.newPage()
            log_prefix = f"[pyppeteer拨测] idx={idx} " if idx is not None else "[pyppeteer拨测] "
            logger.info(f"{log_prefix}newPage成功，准备goto: {self.url}")
            initial_url = self.url
            jumped_urls = []
            def on_navigate(frame):
                if frame.url != initial_url:
                    jumped_urls.append(frame.url)
                    logger.info(f"{log_prefix}跳转到: {frame.url}")
            def on_request(request):
                nonlocal request_count
                request_count += 1
                logger.info(f"{log_prefix}实际发起request: {request.url}")
            page.on('framenavigated', on_navigate)
            page.on('request', on_request)
            try:
                await asyncio.wait_for(page.goto(self.url, waitUntil='domcontentloaded'), timeout=self.timeout)
                logger.info(f"{log_prefix}page.goto完成，当前URL: {page.url}")
                await asyncio.sleep(3)
            except Exception as e:
                logger.warning(f"{log_prefix}page.goto异常: {e}")
                if "ERR_TOO_MANY_REDIRECTS" in str(e):
                    logger.info(f"{log_prefix}检测到循环重定向，jumped_urls={jumped_urls}, request_count={request_count}")
            final_url = page.url
            logger.info(f"{log_prefix}最终页面URL: {final_url}")
            logger.info(f"{log_prefix}本次run_single共发起{request_count}次request")
            jump_success = False
            if mode == "重定向":
                if jumped_urls:
                    jump_success = True
                    logger.info(f"{log_prefix}检测到跳转，目标URL: {jumped_urls[-1]}")
                elif request_count > 1:
                    jump_success = True
                    logger.info(f"{log_prefix}jumped_urls为空但request_count>1，判定为重定向成功")
                else:
                    logger.info(f"{log_prefix}未检测到跳转")
                if jump_success:
                    logger.info(f"{log_prefix}run_single成功")
                else:
                    logger.warning(f"{log_prefix}run_single未跳转，判定为失败")
                self.run_single_request_counts.append(request_count)
                return jump_success
            elif mode == "弹窗":
                logger.info(f"{log_prefix}弹窗模式未实现，判定为失败")
                self.run_single_request_counts.append(request_count)
                return False
            else:
                logger.info(f"{log_prefix}未知模式，判定为失败")
                self.run_single_request_counts.append(request_count)
                return False
        except Exception as e:
            log_prefix = f"[pyppeteer拨测] idx={idx} " if idx is not None else "[pyppeteer拨测] "
            logger.error(f"{log_prefix}run_single异常: {e}")
            self.run_single_exceptions.append(str(e))
            self.run_single_request_counts.append(request_count)
            if "Connection is closed" in str(e) and not retry:
                logger.warning(f"{log_prefix}检测到browser已关闭，尝试重启browser并重试一次")
                global _browser
                if _browser is not None:
                    try:
                        await _browser.close()
                    except Exception:
                        pass
                    _browser = None
                return await self.run_single(mode, retry=True, idx=idx)
            self.fail += 1
        finally:
            if page:
                try:
                    await page.close()
                except Exception as e2:
                    logger.warning(f"[pyppeteer拨测] idx={idx} Page close failed: {e2}")
        self.total += 1
        self.time_consumed += (time.time() - start_time)
        logger.info(f"[pyppeteer拨测] idx={idx} run_single结束, url={self.url}")

    async def run_multiple(self, count, mode):
        logger.info(f"[pyppeteer拨测] run_multiple启动, count={count}, mode={mode}")
        start_all = time.time()
        self.run_single_request_counts = []  # 新增：每次run_multiple前清空
        tasks = [self.run_single(mode, idx=i+1) for i in range(count)]
        results = await asyncio.gather(*tasks)
        logger.info(f"[pyppeteer拨测] run_multiple完成, count={count}, mode={mode}")
        self.success = sum(1 for r in results if r)
        self.fail = sum(1 for r in results if not r)
        self.total = len(results)
        end_all = time.time()
        self.time_consumed = end_all - start_all
        total_requests = sum(self.run_single_request_counts)
        logger.info(f"[pyppeteer拨测] run_multiple统计: success={self.success}, fail={self.fail}, total={self.total}, time={self.time_consumed:.4f}s, 实际发起request总数={total_requests}")
        logger.info(f"[pyppeteer拨测] 每次run_single发起request次数: {self.run_single_request_counts}")
        if self.run_single_exceptions:
            logger.warning(f"[pyppeteer拨测] run_multiple异常统计: {self.run_single_exceptions}")

    def get_result(self):
        success_ratio = (self.success / self.total * 100) if self.total else 0
        return {
            "total": self.total,
            "success": self.success,
            "fail": self.fail,
            "time": round(self.time_consumed, 4),
            "success_ratio": f"{success_ratio:.2f}%"
        }

def kill_all_chrome():
    if sys.platform.startswith("linux"):
        os.system("pkill -9 chrome || true")
        os.system("pkill -9 chromium || true")
    elif sys.platform.startswith("win"):
        os.system("taskkill /F /IM chrome.exe /T")
        os.system("taskkill /F /IM chromium.exe /T")

class BoceChecker:
    def __init__(self, url="http://www.123.com/",timeout=3, chromium_path="/opt/socket/chrome-linux/chrome"):
        self.url = url
        self.timeout = timeout
        self.chromium_path = chromium_path
        self.r_checker = RequestChecker(url=self.url, timeout=self.timeout)
        self.p_checker = PyppeteerChecker(url=self.url, chromium_path=self.chromium_path, timeout=self.timeout)

    def update(self):
        self.r_checker.url = self.url
        self.r_checker.timeout = self.timeout
        self.r_checker.total = 0
        self.r_checker.success = 0
        self.r_checker.fail = 0
        self.r_checker.time_consumed = 0
        self.p_checker.url = self.url
        self.p_checker.chromium_path = self.chromium_path
        self.p_checker.timeout = self.timeout
        self.p_checker.total = 0
        self.p_checker.success = 0
        self.p_checker.fail = 0
        self.p_checker.time_consumed = 0
    def boce(self, url, count=1, interval=0, thread_count=1, timeout=3, mode="封堵", chromium_path="/opt/socket/chrome-linux/chrome"):
        self.url = url
        self.thread_count = thread_count
        self.timeout = timeout
        self.chromium_path = chromium_path
        self.update()
        if mode == "封堵":
            print(f"只执行requests拨测（封堵模式）")
            self.r_checker.run_multiple(count, thread_count, interval)
            req_result = self.r_checker.get_result()
            print(f"requests拨测结果: {req_result}")
            return req_result  # 直接返回，不再执行pyppeteer
        else:
            print(f"跳过requests拨测（只对封堵模式执行）")
        print(f"开始pyppeteer {mode}拨测...")
        if not chromium_path:
            raise ValueError("必须指定 Chromium 可执行文件路径参数 chromium_path")
        # 懒加载全局loop/browser
        loop, browser = get_global_loop_and_browser(chromium_path)
        future = asyncio.run_coroutine_threadsafe(self.p_checker.run_multiple(count, mode), loop)
        future.result()  # 阻塞等待
        pw_result = self.p_checker.get_result()
        print(f"pyppeteer拨测结果: {pw_result}")
        return pw_result

tcpdump_scapy = None
ss = None
boce = BoceChecker()

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

    # 递归收集 pcap 文件
    pcap_files = []
    if os.path.isdir(pcap_dir):
        for root, _, files in os.walk(pcap_dir):
            for name in files:
                if name.endswith(".pcap"):
                    pcap_files.append(os.path.join(root, name))

    for pcap_path in pcap_files:
        pcap_name = os.path.basename(pcap_path)
        # flows: canonical_key -> 流状态
        # canonical_key: 双向归一化的 (ip,port) 对 + protoType
        # 流状态: first_src, first_dst, client(默认=first_src), finalized, proto_type
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

                    # 解析以太网头
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

                    # 协议类型映射
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

                    # 端口解析
                    if proto_type in ("1", "2", "3"):
                        if len(trans) < 4:
                            continue
                        sport, dport = struct.unpack("!HH", trans[:4])
                        sport_s, dport_s = str(sport), str(dport)
                        flags = trans[13] if proto == 6 and len(trans) > 13 else 0
                    else:
                        # ICMP 无端口
                        sport_s, dport_s = "0", "0"
                        flags = 0

                    src_tuple = (ip_src, sport_s)
                    dst_tuple = (ip_dst, dport_s)
                    # 双向归一化 key：排序两端 + protoType
                    canonical = tuple(sorted([src_tuple, dst_tuple])) + (proto_type,)

                    flow = flows.get(canonical)
                    if flow is None:
                        flow = {
                            "first_src": src_tuple,
                            "first_dst": dst_tuple,
                            "client": src_tuple,  # 默认 = 首包 src
                            "finalized": False,
                            "proto_type": proto_type,
                        }
                        flows[canonical] = flow

                    # 方向未锁定时，扫描信号确定客户端
                    if not flow["finalized"]:
                        signaled_client = None
                        # TCP 握手标志
                        if proto == 6:
                            syn = bool(flags & 0x02)
                            ack = bool(flags & 0x10)
                            if syn and not ack:
                                # SYN 发起方为客户端
                                signaled_client = src_tuple
                            elif syn and ack:
                                # SYN+ACK 接收方为客户端
                                signaled_client = dst_tuple
                        # HTTP 识别（TCP 载荷）
                        if signaled_client is None and proto == 6 and len(trans) >= 14:
                            data_offset = (trans[12] >> 4) * 4
                            http_payload = trans[data_offset:]
                            if http_payload.startswith(_HTTP_REQUEST_METHODS):
                                # 请求方为客户端
                                signaled_client = src_tuple
                            elif http_payload.startswith(b"HTTP/1."):
                                # 响应接收方为客户端
                                signaled_client = dst_tuple
                        if signaled_client is not None:
                            flow["client"] = signaled_client
                            flow["finalized"] = True

            # 输出该 pcap 的所有流
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


def do(datatype, data: bytes, **kwargs):
    global tcpdump_scapy
    global ss
    global cache_sendpkts
    # scapy 发包
    if datatype == 0:
        try:
            # 1. 解析参数并检查
            data = json.loads(s=data)
            logger.info("发包操作，类型：%s，参数：%s" % (datatype, data))
            if not data.get("pcaps") or not data.get("uplink_iface") or not data.get("downlink_iface"):
                return json.dumps({
                    'status': 'error',
                    'message': 'Missing required parameters: uplink_iface or downlink_iface'
                }).encode('utf-8')
            pcaps = data.pop("pcaps")

            # 2. 执行回放
            replayer = PcapReplayer(**data)
            stats = replayer.replay_files(pcaps)

            # 3. 返回结果
            return json.dumps(stats).encode('utf-8')

        except Exception as e:
            logger.error(f"发包错误: {str(e)}")
            return json.dumps({
                'status': 'error',
                'message': str(e)
            }).encode('utf-8')
    # subprocess 操作,格式：【{args=cmd, cwd=None, env=None,shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", wait=True}】
    elif datatype == 1:
        data = json.loads(s=data)
        logger.info("os 操作，类型：%s，参数：%s" % (datatype, data))
        response = exec_cmd_subprocess(**data)
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        res_gzip = compress_gzip(res)
        res_gzip = struct.pack("i", len(res_gzip)) + res_gzip
        return res_gzip

    # 文件传输，下载文件，格式：【filepath】
    elif datatype == 3:
        data = json.loads(s=data)
        logger.info("文件传输：下载文件，类型：%s，文件路径：%s" % (datatype, kwargs["filepath"]))
        with open(kwargs["filepath"], "rb") as f:
            if data.get("gzip") == True:
                content = compress_gzip(f.read())
            else:
                content = f.read()
        msg = struct.pack("<Q", len(content)) + content
        # logger.info([len(content), msg])
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
    # 开始抓包
    elif datatype == 121:
        data = json.loads(s=data)
        logger.info("开始抓包，类型：%s，参数：%s" % (datatype, data))
        tcpdump_scapy = Tcpdump_scapy(**data)
        tcpdump_scapy.start()
        return b"ok"
    # 停止抓包
    elif datatype == 122:
        logger.info("停止抓包，类型：%s" % datatype)
        tcpdump_scapy.stop()
        return b"ok"
    # 下载pcap包
    elif datatype == 123:
        logger.info("下载pcap包，类型：%s" % datatype)
        with BytesIO() as fl:
            PcapWriter(fl).write(tcpdump_scapy.pkts)
            fl.seek(0)
            content = fl.read()
            return struct.pack("<Q", len(content)) + content
    # 拨测开始
    elif datatype == 131:
        data = json.loads(s=data)
        logger.info("拨测开始，类型：%s，参数：%s" % (datatype, data))

        chromium_path = data.get("chromium_path", "/opt/socket/chrome-linux/chrome")

        # 检查并补装 chromium 依赖库
        test_result = exec_cmd_subprocess(
            args=f"{chromium_path} --headless --no-sandbox --disable-gpu --dump-dom about:blank",
            use_run=True
        )
        if test_result['code'] != 0 and 'cannot open shared object file' in (test_result['stderr'] or ''):
            logger.info(f"chromium 缺少依赖库，尝试自动安装...\nstderr: {test_result['stderr']}")

            # 检测包管理器并安装
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

                # 再次验证
                recheck = exec_cmd_subprocess(
                    args=f"{chromium_path} --headless --no-sandbox --disable-gpu --dump-dom about:blank",
                    use_run=True
                )
                if recheck['code'] != 0:
                    logger.error(f"安装后仍然失败: {recheck['stderr']}")
                    res = json.dumps({"error": f"chromium依赖库安装失败: {recheck['stderr'][:200]}"}).encode("utf-8")
                    res = struct.pack("i", len(res)) + res
                    return res
                logger.info("chromium 依赖库安装成功，继续执行拨测")

        response = boce.boce(**data)
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        res = struct.pack("i", len(res)) + res
        return res

    # # 拨测线程运行状态
    # elif datatype == 132:
    #     logger.info("拨测线程运行状态，类型：%s" % datatype)
    #     response = boce.is_run()
    #     logger.info(response)
    #     res = json.dumps({"res": response}).encode("utf-8")
    #     return res
    # # 获取拨测结果
    # elif datatype == 133:
    #     logger.info("获取拨测结果，类型：%s" % datatype)
    #     boce.wait_over()
    #     response = boce.get_result()
    #     logger.info(response)
    #     res = json.dumps(response).encode("utf-8")
    #     return res
    # 获版本号
    elif datatype == 14:
        logger.info("获版客户端版本号，类型：%s" % datatype)
        response = version
        logger.info(response)
        res = json.dumps(response).encode("utf-8")
        return res
    # 解压zip文件
    elif datatype == 15:
        data = json.loads(s=data)
        logger.info("解压zip文件，类型：%s，参数：%s" % (datatype, data))
        unzip(**data)
        return b"ok"
    # # python操作
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
        # logger.info(b"ss.data" + ss.data)
        content = compress_gzip(ss.data)
        msg = struct.pack("<Q", len(content)) + content
        # logger.info([len(content), msg])
        return msg
    # elif type == 175:
    #     logger.info("socket监听-缓存数据，类型：%s" % type)
    #     ss.cachedata()
    #     return b"ok"
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

    else:
        pass


import subprocess

def exec_cmd_subprocess(args, cwd=None, env=None, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", wait=True, use_run=False):
    try:
        if use_run:
            result = subprocess.run(args=args, cwd=cwd, env=env, shell=shell, stdout=stdout, stderr=stderr, encoding=encoding)
            return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        else:
            p = subprocess.Popen(args=args, cwd=cwd, env=env, shell=shell, stdout=stdout, stderr=stderr, encoding=encoding)
            if wait:
                out, err = p.communicate()
                return {"code": p.returncode, "stdout": out, "stderr": err}
            else:
                # return p
                return
    except Exception as e:
        return {"code": -1, "stdout": "", "stderr": str(e)}



def routeinfo():
    res = defaultdict(lambda: None)
    cmd = "route -n"
    response = exec_cmd_subprocess(cmd)
    if response["code"]:
        logger.error(f"执行：{cmd}，返回：{response}")
        raise RuntimeError(f"执行：{cmd}，返回：{response}")
    lines = response["stdout"].strip().split("\n")
    if len(lines) > 2:
        head_list = lines[1].strip().split()
        for line in lines[2:]:
            fields = line.strip().split()
            tmp_dict = dict(zip(head_list, fields))
            key = tmp_dict.pop("Destination")
            res[key] = tmp_dict
    return res


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


def isfile(file):
    # base, name = file.rstrip().rsplit("/", 1)
    # logger.info(base,name)
    # if exec_cmd_subprocess(f"find {base} -maxdepth 1 -type f -name {name}|wc -l")["stdout"] == "1\n":
    #     return True
    # else:
    #     return False
    return os.path.isfile(file)


def isdir(dir):
    return os.path.isdir(dir)
    # base, name = dir.rstrip("/").rsplit("/", 1)
    # if exec_cmd_subprocess(f"cd {base} && find ./ -maxdepth 1 -type d -name {name}|wc -l")["stdout"] == "1\n":
    #     return True
    # else:
    #     return False


def mkdir(dir):
    return os.makedirs(dir)


def mtu(eth, value=2000):
    if not ensure_command("ifconfig"):
        raise RuntimeError("请检查系统是否存在命令：ifconfig")
    cmd = "ifconfig %s|grep mtu|awk '{print $4}'" % eth
    mtu = int(exec_cmd_subprocess(args=cmd)["stdout"])
    if int(mtu) != value:
        cmd = f"sudo ifconfig {eth} mtu {value}"
        exec_cmd_subprocess(cmd)
        time.sleep(5)


def wait_until(func, expect_value, step=2, timeout=60, *args, **kwargs):
    '''通过循环查询，直到查询成功或者超时！'''
    cur_time = time.time()
    flag = False
    while time.time() - cur_time <= timeout:
        try:
            act_value = func(*args, **kwargs)
        except Exception as e:
            logger.error(e)
            continue
        if act_value == expect_value:
            flag = True
            return flag
        else:
            pass
        time.sleep(step)
    return flag


def wait_not_until(func, expect_value, step=2, timeout=60, *args, **kwargs):
    '''通过循环查询，直到查询成功或者超时！'''
    cur_time = time.time()
    flag = False
    while time.time() - cur_time <= timeout:
        try:
            act_value = func(*args, **kwargs)
        except Exception as e:
            logger.error(e)
            continue
        if act_value != expect_value:
            flag = True
            return flag
        else:
            pass
        time.sleep(step)
    return flag


def cur_time(mode=2):
    '''
    获取当前时间
    :param
    mode:1 原始时间数据 例如：1534600402.09
    mode:2 秒级时间戳  例如：1534600402
    mode:3 毫秒级时间戳 例如：1534600402086
    mode:4 时间格式化 例如：2018-08-18 21:53:22(年-月-日 时:分:秒)
    :return:
    '''
    t = time.time()
    if mode == 1:
        return t
    elif mode == 2:
        return (int(t))
    elif mode == 3:
        return (int(round(t * 1000)))
    elif mode == 4:
        return (datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    else:
        return 0


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

def unzip(file, outdir=None, passwd=None, overwrite=True):
    """对zip压缩文件解压"""
    check = exec_cmd_subprocess("unzip -v")
    if check["code"] != 0:
        logger.error(check["stderr"])
        return
    dir = os.path.dirname(file)
    filename = os.path.basename(file)
    str_overwrite = "-o" if overwrite else ""
    str_passwd = f"-P {passwd}" if passwd else ""
    str_outdir = f"-d {outdir}" if outdir else ""
    cmd = f"unzip {str_overwrite} {str_passwd} {filename} {str_outdir}"
    exec_cmd_subprocess(cmd, cwd=dir)


def compress_gzip(content):
    compressed_data = gzip.compress(content)
    return compressed_data


def decompress_gzip(compressed_data):
    content = gzip.decompress(compressed_data)
    return content


def python_cmd(*args):
    res = eval(args[0])
    if len(args) > 1:
        for arg in args[1:]:
            res = eval(f"res.{arg}")
    return res


def detect_ip_version(host):
    """检测host的IP版本，返回 4 或 6"""
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return 6
    except socket.error:
        pass
    try:
        socket.inet_pton(socket.AF_INET, host)
        return 4
    except socket.error:
        pass
    raise ValueError(f"无效的IP地址: {host}")


class SocketServerListen:
    # host=None 表示双栈模式，自动监听 0.0.0.0 + ::
    def __init__(self, host=None, port=30001):
        self.host = host
        self.port = port
        self.q = multiprocessing.Queue()
        self.data = b""
        self._stop_flag = True
        self._processes = []  # 支持多个监听进程（双栈场景）

    # ------------------------------------------------------------------ #
    #  确定需要监听哪些 (host, af) 组合
    # ------------------------------------------------------------------ #
    def _resolve_listeners(self):
        """
        返回 [(host, address_family), ...]
        - host=None  → 双栈：[(0.0.0.0, AF_INET), ('::', AF_INET6)]
        - 指定 host  → 自动识别版本
        """
        if self.host is None:
            return [
                ("0.0.0.0", socket.AF_INET),
                ("::",      socket.AF_INET6),
            ]

        version = detect_ip_version(self.host)
        af = socket.AF_INET if version == 4 else socket.AF_INET6
        return [(self.host, af)]

    # ------------------------------------------------------------------ #
    #  单个监听循环（运行在独立子进程中）
    # ------------------------------------------------------------------ #
    def _start_server(self, host, af):
        with socket.socket(af, socket.SOCK_STREAM) as s:
            # IPV6_V6ONLY=1：让 IPv6 socket 只处理 IPv6，避免与 IPv4 socket 冲突
            if af == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, self.port))
            s.listen(5)
            logger.info(f"Listening on [{host}]:{self.port} (IPv{'6' if af == socket.AF_INET6 else '4'})...")

            while self._stop_flag:
                conn, addr = s.accept()
                try:
                    with conn:
                        logger.info(f"Connected by {addr}")
                        while self._stop_flag:
                            data = conn.recv(10240)
                            if not data:
                                time.sleep(0.5)
                                logger.error("无效数据循环2...")
                                break
                            self.q.put(data)
                except Exception as e:
                    logger.error(e)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                logger.error("无效数据循环3...")
                time.sleep(0.5)

    # ------------------------------------------------------------------ #
    #  启动 / 停止
    # ------------------------------------------------------------------ #
    def start_server(self):
        try:
            listeners = self._resolve_listeners()

            # 为每个监听地址启动独立子进程
            for host, af in listeners:
                p = multiprocessing.Process(
                    target=self._start_server,
                    args=(host, af),
                    daemon=True,
                )
                p.start()
                self._processes.append(p)

            # 数据缓存线程（共用同一个 Queue）
            self.getdataprocess = threading.Thread(target=self.cachedata, daemon=True)
            self.getdataprocess.start()

            time.sleep(5)
            pids = self.pid()
            if pids:
                return True
        except Exception as e:
            logger.error(e)

    def stop_server(self):
        self._stop_flag = False

        # 优先通过 multiprocessing 终止子进程
        for p in self._processes:
            if p.is_alive():
                p.terminate()
                p.join(timeout=3)
                if p.is_alive():
                    p.kill()
        self._processes.clear()

        # 兜底：通过端口号 kill 残留进程
        pid_str = self.pid()
        if pid_str:
            for pid in pid_str.split():
                os.system(f"kill -9 {pid}")
            return True

    def pid(self):
        pid = os.popen(
            "ss -lptn 'sport = :%s'|grep users|awk -F ',' '{print $2}'|awk -F '=' '{print $2}'" % self.port
        ).read().strip()
        return pid

    # ------------------------------------------------------------------ #
    #  数据管理（与原版保持一致）
    # ------------------------------------------------------------------ #
    def cleandata(self):
        while self.q.qsize():
            time.sleep(1)
        time.sleep(1)
        self.data = b""
        os.system("rm -rf /tmp/socketbin/*")

    def cachedata(self):
        if not os.path.isdir("/tmp/socketbin"):
            os.makedirs("/tmp/socketbin")
        else:
            os.system("rm -rf /tmp/socketbin/*")
        datawrite = b""
        while True:
            if self.q.qsize():
                try:
                    tmp_q_get = self.q.get(timeout=1)
                except Exception:
                    continue
                datawrite += tmp_q_get
                self.data += tmp_q_get
            else:
                if datawrite:
                    with open(f"/tmp/socketbin/{int(time.time() * 1000000)}.bin", "wb") as f:
                        f.write(datawrite)
                    datawrite = b""
                time.sleep(1)

    def data_writefile(self, file):
        with open(file, "wb") as f:
            f.write(self.data)



# 添加PcapStats类和PcapSender类
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




# type 0 scapy 发包【0,发包参数{pcap=pcap, iface=iface, inter=inter,return_packets=true}转json】
# type 1 操作系统命令【{args=data, cwd=None, env=None,shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8"}转json】
# type 21 文件上传/下载：文件名【{"filepath":filepath}转json】
# type 22 文件上传：文件长度【4字节整形b""】
# type 23 文件上传：文件内容上传【b""】
# type 24 文件上传：内容写入文件【】
# type 3 文件下载：获取21对应的文件【】，返回二进制字符串
# type 4 路由信息查询routeinfo
# type 5 开启tcpdump抓包，【eth=None, path="/home/tmp/tmp.pcap", extended=""】
# type 6 停止tcpdump抓包
# type 7 是否文件【file=''】
# type 8 是否目录【dir=''】
# type 9 创建目录【dir=''】
# type 10 修改mtu【eth='', value=2000】
# type 11 文件大小【file=''】
# type 121 开启抓包【"iface": self.iface, "filter": self.filter, "path": self.remotepath, "timeout": self.timeout】
# type 122 停止抓包【】
# type 123 下载pcap包【】
# type 131 开始拨测【url, count=1, interval=0, thread_count=1, timeout=None】
# type 132 拨测线程运行状态【】
# type 133 获取拨测结果【】
# type 14 获取版本号【】
# type 15 zip文件解压【file, outdir=None, passwd=None】
# type 16 本地执行os命令，示例：python_cmd(f"os.popen('{cmd}')", "read()")
# type 171 socket监听-启动，【host="0.0.0.0", port=30001】
# type 172 socket监听-清理数据，【】
# type 173 socket监听-保存数据，【file="/tmp/socketserver.bin"】
# type 174 socket监听-取数据
# type 174 socket监听-缓存数据，数据提取前都需先要缓存数据
# tpye 18 确保系统中存在指定命令，如果不存在且提供了安装命令，则尝试自动安装。ensure_command(cmd="ifconfig", install_cmd="yum install -y net-tools")
# type 200 提取pcap五元组流，【pcap_dir】全包遍历 pcap 目录，返回方向化五元组列表 [{pcap,srcIp,srcPort,destIp,destPort,protoType}]

# 在文件最后添加主程序:
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, help="监听端口")
    args = parser.parse_args()
    port = args.port if args.port else 9000

    server4 = socketserver.ThreadingTCPServer(('0.0.0.0', port), MyTCPHandler)
    server6 = None

    logger.info(f"监听 0.0.0.0:{port} (IPv4)")

    # 尝试启动 IPv6 监听，失败则降级为仅 IPv4
    try:
        class ThreadingTCPServer6(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET6
            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        server6 = ThreadingTCPServer6(('::', port), MyTCPHandler)
        logger.info(f"监听 [::]:{port} (IPv6)")
    except Exception as e:
        logger.warning(f"IPv6 监听启动失败: {e}，降级为仅 IPv4 模式")
        server6 = None

    # IPv4 在子线程跑
    t4 = threading.Thread(target=server4.serve_forever, daemon=True)
    t4.start()

    if server6:
        try:
            server6.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务停止")
            server4.shutdown()
            server6.shutdown()
    else:
        # 仅 IPv4 模式，主线程跑 IPv4
        try:
            server4.serve_forever()
        except KeyboardInterrupt:
            logger.info("服务停止")
            server4.shutdown()

    # tcpdump_start(extended="port 8000")
    #
    # print(111)
    # time.sleep(20)
    # tcpdump_stop()
