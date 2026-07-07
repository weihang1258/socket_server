import os
import sys
import time
import json
import shutil
import threading
import asyncio
import logging
import subprocess
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

logger = logging.getLogger(__name__)

# 屏蔽 pyppeteer 及 requests 的 DEBUG 日志
logging.getLogger("pyppeteer").setLevel(logging.WARNING)
logging.getLogger("pyppeteer.connection").setLevel(logging.WARNING)
logging.getLogger("pyppeteer.launcher").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

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

# 全局变量 抓包工具命令_sniff_command（由 __init__.init_capture 同步）
_sniff_command = None

_nic_cpu_binding = {}  # 网卡 -> CPU 映射
_allocated_cpus = set()  # 已分配的CPU集合
_cpu_binding_lock = threading.Lock()  # 线程锁

# chromium 自动下载相关
CHROME_INSTALL_DIR = "/opt/socket/chrome-linux"
NPMMIRROR_BASE = "https://registry.npmmirror.com/-/binary/chromium-browser-snapshots/Linux_x64"


def _read_config_proxy():
    """从 /opt/socket/config 读 proxy= 字段，复用 upgrader 的约定"""
    try:
        with open("/opt/socket/config") as f:
            for line in f:
                line = line.strip()
                if line.startswith("proxy="):
                    val = line.split("=", 1)[1].strip()
                    return val or None
    except Exception:
        pass
    return None


def _fetch_latest_chromium_revision():
    """查询 npmmirror Linux_x64 目录，返回最新的 revision 号（字符串）"""
    proxies = {"http": p, "https": p} if (p := _read_config_proxy()) else None
    resp = requests.get(NPMMIRROR_BASE + "/", timeout=20, proxies=proxies)
    resp.raise_for_status()
    entries = resp.json()
    revs = [e["name"].rstrip("/") for e in entries if e["name"].rstrip("/").isdigit()]
    if not revs:
        raise RuntimeError("npmmirror 未返回任何 chromium revision")
    return max(revs, key=int)


def _download_chromium_zip(revision, dest_zip):
    """下载指定 revision 的 chrome-linux.zip 到 dest_zip"""
    proxies = {"http": p, "https": p} if (p := _read_config_proxy()) else None
    url = f"{NPMMIRROR_BASE}/{revision}/chrome-linux.zip"
    logger.info(f"下载 chromium r{revision}: {url}")
    resp = requests.get(url, stream=True, timeout=300, proxies=proxies)
    resp.raise_for_status()
    with open(dest_zip, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)


def _print_manual_download_hint(revision):
    """下载失败时打印手动下载方法"""
    hint = (
        "\n========== chromium 自动下载失败，请手动下载 ==========\n"
        f"1. 在能联网的机器下载:\n"
        f"   wget {NPMMIRROR_BASE}/{revision}/chrome-linux.zip\n"
        f"   (或浏览器打开上述 URL)\n"
        f"2. 解压到靶机 {CHROME_INSTALL_DIR} (最终路径需为 {CHROME_INSTALL_DIR}/chrome):\n"
        f"   mkdir -p /opt/socket && cd /opt/socket\n"
        f"   unzip chrome-linux.zip   # 解压出 chrome-linux/ 目录\n"
        f"3. 赋予执行权限:\n"
        f"   chmod +x {CHROME_INSTALL_DIR}/chrome\n"
        f"4. 验证:\n"
        f"   {CHROME_INSTALL_DIR}/chrome --headless --no-sandbox --dump-dom about:blank\n"
        f"======================================================"
    )
    logger.error(hint)
    print(hint)


def ensure_chromium(chromium_path=CHROME_INSTALL_DIR + "/chrome"):
    """确保 chromium 可执行文件存在，不存在则自动下载。

    返回 True 表示可用（已存在或下载成功），False 表示下载失败需手动处理。
    线程安全：用 _browser_lock 复用，避免并发拨测重复下载。
    """
    if os.path.isfile(chromium_path) and os.access(chromium_path, os.X_OK):
        return True

    with _browser_lock:
        # double-check：可能在等锁期间已被其他线程下载好
        if os.path.isfile(chromium_path) and os.access(chromium_path, os.X_OK):
            return True

        try:
            revision = _fetch_latest_chromium_revision()
            logger.info(f"npmmirror 最新 chromium revision: {revision}")
        except Exception as e:
            logger.error(f"查询 chromium 最新版本失败: {e}")
            _print_manual_download_hint("latest")
            return False

        import tempfile
        staging = tempfile.mkdtemp(prefix="chrome_dl_")
        dest_zip = os.path.join(staging, "chrome-linux.zip")
        try:
            _download_chromium_zip(revision, dest_zip)
            logger.info(f"chromium 下载完成，开始解压到 {CHROME_INSTALL_DIR}...")
            # 解压：zip 内顶层是 chrome-linux/，解压到 /opt/socket/ 得到 /opt/socket/chrome-linux/
            import zipfile
            parent = os.path.dirname(CHROME_INSTALL_DIR)
            with zipfile.ZipFile(dest_zip) as zf:
                zf.extractall(parent)
            os.chmod(chromium_path, 0o755)
            if os.path.isfile(chromium_path) and os.access(chromium_path, os.X_OK):
                logger.info(f"chromium 安装成功: {chromium_path}")
                return True
            logger.error(f"解压后未找到可执行的 chrome: {chromium_path}")
            _print_manual_download_hint(revision)
            return False
        except Exception as e:
            logger.error(f"chromium 自动下载失败: {e}")
            _print_manual_download_hint(revision)
            return False
        finally:
            shutil.rmtree(staging, ignore_errors=True)


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
        if hasattr(browser, "is_closed"):
            return browser.is_closed()
        elif hasattr(browser, "_connection"):
            return browser._connection is None
        return True

    @staticmethod
    async def close_browser():
        global _browser
        if _browser is not None:
            try:
                if not BrowserManager.is_browser_closed(_browser):
                    await _browser.close()
            except Exception as e:
                logger.warning(f"关闭 browser 失败: {e}")
            _browser = None

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
    def __init__(self, url="http://www.123.com/", timeout=3, chromium_path="/opt/socket/chrome-linux/chrome"):
        self.url = url
        self.timeout = timeout
        self.chromium_path = chromium_path
        self.r_checker = RequestChecker(url=self.url, timeout=self.timeout)
        self.p_checker = None  # 延迟初始化，避免 import 时 chromium 不存在导致崩溃

    def update(self):
        self.r_checker.url = self.url
        self.r_checker.timeout = self.timeout
        self.r_checker.total = 0
        self.r_checker.success = 0
        self.r_checker.fail = 0
        self.r_checker.time_consumed = 0
        if self.p_checker:
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
        # 延迟初始化 p_checker
        if self.p_checker is None:
            self.p_checker = PyppeteerChecker(url=self.url, chromium_path=self.chromium_path, timeout=self.timeout)
        # 懒加载全局loop/browser
        loop, browser = get_global_loop_and_browser(chromium_path)
        future = asyncio.run_coroutine_threadsafe(self.p_checker.run_multiple(count, mode), loop)
        future.result()  # 阻塞等待
        pw_result = self.p_checker.get_result()
        print(f"pyppeteer拨测结果: {pw_result}")
        return pw_result
