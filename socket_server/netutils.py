import os
import subprocess
import shutil
import gzip
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


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
    cmd = "ifconfig %s|grep mtu|awk '{print $4}'" % eth
    mtu_val = int(exec_cmd_subprocess(args=cmd)["stdout"])
    if int(mtu_val) != value:
        cmd = f"sudo ifconfig {eth} mtu {value}"
        exec_cmd_subprocess(cmd)
        time.sleep(5)


def wait_until(func, expect_value, step=2, timeout=60, *args, **kwargs):
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


def python_cmd(*args):
    res = eval(args[0])
    if len(args) > 1:
        for arg in args[1:]:
            res = eval(f"res.{arg}")
    return res
