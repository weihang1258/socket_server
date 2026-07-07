import os
import sys
import subprocess
import shutil
import gzip
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


def _strip_sudo_if_root(args):
    """以 root 运行时剥离简单的 'sudo ' 前缀。

    服务端通过 systemd 以 root 运行，提权是多余的；且目标机 sudo 可能因
    libldap/OpenSSL ABI 不匹配而损坏，导致所有 sudo 命令失败。剥离后命令
    直接以 root 执行，行为等价且不受 sudo 损坏影响。

    仅剥离裸 'sudo <cmd>'；带 sudo 自身选项（如 'sudo -E'、'sudo -u'）的
    命令保留原样，避免改变语义。非 root 运行时不剥离。
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0 or not isinstance(args, str):
        return args
    s = args.strip()
    if not (s.startswith("sudo ") or s.startswith("sudo\t")):
        return args
    after = s[5:].lstrip()
    if not after or after.startswith("-"):
        return args  # 带 sudo 选项或空命令，不剥离
    logger.info(f"以 root 运行，剥离 sudo 前缀: {args!r} -> {after!r}")
    return after


def _clean_subprocess_env(env):
    """清除 PyInstaller 打包注入的环境变量，避免污染子进程。

    socket_server 是 PyInstaller 打包的二进制，运行时会把临时解包目录(_MEIxxxx)
    注入多个环境变量。若子进程继承这些指向临时目录的变量，会出两类问题：
    1. 加载错误版本动态库——DPI 的 xsa 因 LD_LIBRARY_PATH 指向 _MEI 而加载到
       打包的 libcrypto.so.1.1(OpenSSL 1.1.1f)，启动即崩，被 dpi_monitor 反复重启；
    2. 持有失效路径——_MEI 目录在 socket_server 退出即删，长驻子进程(如 dpi_monitor)
       继承的 PLAYWRIGHT_BROWSERS_PATH=/tmp/_MEIxxxx/ms-playwright 变成悬空引用。

    清理策略：
    - 移除 LD_LIBRARY_PATH（显式，即便非打包模式也清）；
    - 移除所有 _PYI_* 内部变量（前缀匹配）；
    - 移除任何值以 sys._MEIPASS 开头的变量（覆盖 PLAYWRIGHT_BROWSERS_PATH 及未来
      新增的同源变量，无需逐个枚举）。
    PATH 显式保留——子进程找命令靠它；即便其值引用了 _MEIPASS 也不动。
    进程内代码(如 playwright)仍可直接读 os.environ，不受影响：本函数只改传给
    子进程的环境副本，不修改 os.environ。
    """
    env = dict(os.environ if env is None else env)
    meipass = getattr(sys, "_MEIPASS", None)
    for k in list(env.keys()):
        if k == "LD_LIBRARY_PATH" or k.startswith("_PYI_"):
            del env[k]
        elif k != "PATH" and meipass and isinstance(env[k], str) and env[k].startswith(meipass):
            del env[k]
    return env


def exec_cmd_subprocess(args, cwd=None, env=None, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", wait=True, use_run=False):
    try:
        if shell and isinstance(args, str):
            args = _strip_sudo_if_root(args)
        env = _clean_subprocess_env(env)
        if use_run:
            result = subprocess.run(args=args, cwd=cwd, env=env, shell=shell, stdout=stdout, stderr=stderr, encoding=encoding)
            return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        else:
            p = subprocess.Popen(args=args, cwd=cwd, env=env, shell=shell, stdout=stdout, stderr=stderr, encoding=encoding)
            if wait:
                out, err = p.communicate()
                return {"code": p.returncode, "stdout": out, "stderr": err}
            else:
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


def isfile(file):
    return os.path.isfile(file)


def isdir(dir):
    return os.path.isdir(dir)


def mkdir(dir):
    return os.makedirs(dir)


def mtu(eth, value=2000):
    if not ensure_command("ifconfig"):
        raise RuntimeError("请检查系统是否存在命令：ifconfig")
    # 用 list args + shell=False 执行，避免 eth 被客户端注入 shell 元字符；
    # 在 Python 里解析 mtu 值，替代原 ifconfig|grep|awk 管道。
    res = exec_cmd_subprocess(args=["ifconfig", eth], shell=False)
    if res["code"] != 0:
        raise RuntimeError(f"执行 ifconfig {eth} 失败: {res.get('stderr', '')}")
    mtu_val = None
    for line in res["stdout"].splitlines():
        lower = line.lower()
        if "mtu" in lower:
            # 兼容两种格式：新式 "eth0: flags=...  mtu 1500" / 老式 "...  MTU:1500  ..."
            parts = line.replace("MTU:", " mtu ").split()
            for i, p in enumerate(parts):
                if p.lower() == "mtu" and i + 1 < len(parts):
                    mtu_val = parts[i + 1]
                    break
            if mtu_val is not None:
                break
    if mtu_val is None:
        raise RuntimeError(f"无法从 ifconfig {eth} 输出中解析 mtu")
    if int(mtu_val) != value:
        # socket_server 以 root 运行，ifconfig 无需 sudo
        exec_cmd_subprocess(args=["ifconfig", eth, "mtu", str(value)], shell=False)
        time.sleep(5)


def wait_until(func, expect_value, step=2, timeout=60, *args, **kwargs):
    cur_time = time.time()
    flag = False
    while time.time() - cur_time <= timeout:
        try:
            act_value = func(*args, **kwargs)
        except Exception as e:
            logger.error(e)
            time.sleep(step)  # 异常后也要等待，避免 CPU 空转
            continue
        if act_value == expect_value:
            flag = True
            return flag
        else:
            pass
        time.sleep(step)
    return flag


def wait_not_until(func, expect_value, step=2, timeout=60, *args, **kwargs):
    cur_time = time.time()
    flag = False
    while time.time() - cur_time <= timeout:
        try:
            act_value = func(*args, **kwargs)
        except Exception as e:
            logger.error(e)
            time.sleep(step)  # 异常后也要等待，避免 CPU 空转
            continue
        if act_value != expect_value:
            flag = True
            return flag
        else:
            pass
        time.sleep(step)
    return flag


def cur_time(mode=2):
    t = time.time()
    if mode == 1:
        return t
    elif mode == 2:
        return (int(t))
    elif mode == 3:
        return (int(round(t * 1000)))
    elif mode == 4:
        from datetime import datetime
        return (datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    else:
        return 0


def ensure_command(cmd: str, install_cmd: str = None):
    if shutil.which(cmd):
        print(f"[OK] {cmd} exists")
        return True
    else:
        print(f"[NO] {cmd} not found")
        if install_cmd:
            print(f"Installing: {install_cmd}")
            subprocess.run(install_cmd, shell=True)
            if shutil.which(cmd):
                print(f"[OK] {cmd} exists")
                return True
        return False


def detect_ip_version(host):
    import socket
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


def compress_gzip(content):
    compressed_data = gzip.compress(content)
    return compressed_data


def decompress_gzip(compressed_data):
    content = gzip.decompress(compressed_data)
    return content


def unzip(file, outdir=None, passwd=None, overwrite=True):
    check = exec_cmd_subprocess(args=["unzip", "-v"], shell=False)
    if check["code"] != 0:
        logger.error(check["stderr"])
        return
    dir = os.path.dirname(file)
    filename = os.path.basename(file)
    # 用 list args + shell=False，避免 filename/passwd/outdir 被客户端注入 shell 元字符
    cmd = ["unzip"]
    if overwrite:
        cmd.append("-o")
    if passwd:
        cmd += ["-P", passwd]
    cmd.append(filename)
    if outdir:
        cmd += ["-d", outdir]
    exec_cmd_subprocess(args=cmd, shell=False, cwd=dir or None)


def python_cmd(*args):
    res = eval(args[0])
    if len(args) > 1:
        for arg in args[1:]:
            res = eval(f"res.{arg}")
    return res
