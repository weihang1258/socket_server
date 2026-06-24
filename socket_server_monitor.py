#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2024/2/2 13:58
# @Author  : weihang
# @File    : socket_server_monitor.py
import json
import logging
import os
import subprocess
import re
import psutil
import time

# 添加日志打印
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler("/var/log/socket_server_monitor.log")
fh.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

# 获取可以cpu信息
def numa_sh():
    numa2cpu = dict()
    pci_info = dict()
    cmd = "cd /opt/dpi/kmod;./numa.sh"
    response = os.popen(cmd).read()
    id_node = 0
    numa_flag = None
    for line in response.strip().split("\n"):
        # if line.startswith("NUMA ") and "CPU" not in line:
        #     count_node = int(line.split(":")[1])
        if line.startswith("NUMA ") and "CPU" in line:
            tmp_list = list()
            for ran in line.split(":")[1].strip().split(","):
                if "-" not in ran:
                    tmp_list.append(int(ran))
                else:
                    s, e = ran.split("-", 1)
                    tmp_list += list(range(int(s), int(e) + 1))
            numa2cpu[str(id_node)] = tmp_list
            id_node += 1
        if line.startswith("0000:"):
            pci, tmp = line.strip().split(maxsplit=1)
            name, tmp1 = tmp.split("'", maxsplit=2)[1:]
            pci_info[pci] = {"name": name}
            for field in tmp1.split():
                if "=" in field:
                    key, val = field.split("=", maxsplit=1)
                    pci_info[pci][key.strip()] = val.strip()
            numa_flag = pci
        if line.startswith("numa,"):
            pci_info[numa_flag]["numa"] = line.lstrip("numa,").strip()

    return {"numa2cpu": numa2cpu, "pci_info": pci_info}

def get_syscfg_cpu():
    tmp = list()
    res = list()
    syscfg = get_json(path="/opt/dpi/syscfg.json")
    tmp.append(str(syscfg["master_core"]))
    for port in syscfg["ports"]:
        tmp.append(port["io_cores"])
        tmp.append(port["wk_cores"])
    for task in syscfg["tasks"]:
        tmp.append(task["send_cores"])
        tmp.append(task["recv_cores"])
    for field in ",".join(tmp).split(","):
        if "-" in field:
            s, e = field.split("-", 1)
            res += list(range(int(s), int(e) + 1))
        else:
            res.append(int(field))
    return res

def get_cpuxsa_cpu():
    tmp = list()
    res = list()
    cpuxsa = get_json(path="/opt/dpi/xsaconf/cpuxsa.json")
    for k, v in cpuxsa.items():
        if k not in ("ver", "buildtime") and v not in (-1, "", "-1"):
            tmp.append(str(v))
    if not tmp:
        return []
    for field in ",".join(tmp).split(","):
        if "-" in field:
            s, e = field.split("-", 1)
            res += list(range(int(s), int(e) + 1))
        else:
            res.append(int(field))
    return res

def get_json(path):
    try:
        with open(path, 'r') as fr:
            return json.load(fr)
    except Exception as e:
        logger.error(e)
        print("\033[1;31m", "json文件%s中有错误格式，请修正后重新执行！！！(一般文件内容中有多余的逗号，vi编辑的时候会显示红色)" % path, "\033[0m")

available_cpu = list()
if os.path.isfile("/opt/dpi/syscfg.json"):
    numa_sh = numa_sh()
    xsa_json = get_json("/opt/dpi/xsaconf/xsa.json")
    if xsa_json:
        for i in list(set(get_syscfg_cpu() + get_cpuxsa_cpu())):
            for v in numa_sh["numa2cpu"].values():
                if i in v:
                    v.remove(i)
        for cpus in list(numa_sh["numa2cpu"].values()):
            if cpus:
                available_cpu += cpus

else:
    cmd = "lscpu |grep 'On-line CPU(s) list'|awk -F ':' '{print $2}'"
    response = os.popen(cmd).read().strip()
    available_cpu = list()
    for seg in response.split(","):
        a, b = seg.strip().split("-")
        available_cpu += list(range(int(a), int(b)+1))

if len(available_cpu) > 1:
    available_cpu.remove(0) if 0 in available_cpu else None
logger.info(f"可用CPU核心: {available_cpu}")

# 获取当前进程
# p = psutil.Process(os.getpid())
# 绑定到 CPU 核心 0 和 1（可以根据需要修改）
# p.cpu_affinity([7, 8])
# p.cpu_affinity(available_cpu)
# logger.info(f"绑定后的CPU核心: {p.cpu_affinity()}")


version = "1.0"
# logger.info(f"version:{version}")

path_base = "/opt/socket/"
# path_base = os.getcwd()
logger.info(f"运行base路径：{path_base}")
# logger.info(f"当前运行路径：{os.path.dirname(os.path.abspath(__file__))}")
os.chdir(path_base)
if not os.path.isdir(path_base):
    os.makedirs(path_base)

# 创建开机启动项文件
def get_systemversion():
    if os.path.isfile("/etc/system-release"):
        return os.popen("cat /etc/system-release").read().strip()
    else:
        response = os.popen("lsb_release -d").read().strip()
        res = response.strip().split(":", 1)[1]
        return res

# 没有rc.local
systemversion = get_systemversion()
logger.info(f"当前系统版本：\n{systemversion}")
if not os.path.isfile("/etc/rc.local") and "CentOS" in systemversion:
    os.system("touch /etc/rc.local")
    os.system("chmod +x /etc/rc.local")
    os.system("echo '#!/bin/bash' > /etc/rc.local")
elif not os.path.isfile("/etc/rc.local"):
    os.system("cp /lib/systemd/system/rc-local.service /etc/systemd/system/")
    os.system("echo '' >> /etc/systemd/system/rc-local.service")
    os.system("echo '[Install]' >> /etc/systemd/system/rc-local.service")
    os.system("echo 'WantedBy=multi-user.target' >> /etc/systemd/system/rc-local.service")
    os.system("echo 'Alias=rc-local.service' >> /etc/systemd/system/rc-local.service")
    os.system("touch /etc/rc.local")
    os.system("chmod +x /etc/rc.local")
    os.system("echo '#!/bin/bash' > /etc/rc.local")
else:
    os.system("chmod +x /etc/rc.local")
    if os.path.isfile("/etc/rc.d/rc.local"):
        os.system("chmod +x /etc/rc.d/rc.local")

# 更换启动方式
# # rc.local中没有启动项
# with open("/etc/rc.local", "r") as f:
#     content = f.read()
# item_start = False
# cmd = "/opt/socket/socket_server_monitor >/dev/null 2>&1 &"
# for line in content.strip().split("\n"):
#     if re.match(cmd, line.strip()):
#         item_start = True
#         break
# # 添加启动项
# if item_start is False:
#     logger.info(f"添加启动项：echo '{cmd}' >> /etc/rc.local")
#     os.system(f"echo '{cmd}' >> /etc/rc.local")

# 通过/etc/rc3.d来启动
content = '''#!/bin/bash

prog="/opt/socket/socket_server_monitor"

if [ `id -u` -ne 0 ]; then
    echo "-E- You must be root to run socket_server_monitor"
    exit 1
fi

start()
{
    echo "Starting $prog"

	sudo $prog >/dev/null 2>&1 &

}


print_stop_log()
{
        buildtime=`date +"%Y-%m-%d %H:%M:%S"`
        level=INFO
        opt=OPERATE
        ssh_conn=`env |grep SSH_CONNECTION|awk -F "=" '{print $2}' | sed 's/ /:/g' `
        log="power off,stop run /opt/socket/socket_server_monitor"

        echo $buildtime,INFO,OPERATE,$ssh_conn:socket_server_monitor~$log >> /var/log/socket_server_monitor.log
}



stop()
{
    echo "Stopping $prog"
    print_stop_log
    kill -9 `ps -ef|grep /opt/socket/socket_server_|grep -v grep|awk '{print $2}'`
    RETVAL=0
}

stopme()
{
    echo "Stopping $prog"
    killall -9 socket_server_monitor
    RETVAL=0
}

print_status()
{
    if [ -n "`pidof $prog`" ]; then
        echo "$prog is running."
        exit 0
    fi

    echo "$prog is not run!"
    exit 1
}


case "$1" in
    start)
		start 
        ;;
    stop)
        stop
        ;;
    stopme)
        stopme
        ;;

    status)
        print_status
        ;;
    restart)
        stop
        start
        ;;
    *)
        echo "Usage:"
        echo "    $0 {start|stop|status|restart|stopme}"
        RETVAL=1
esac
exit $RETVAL
'''
if os.path.isdir("/etc/rc.d/init.d"):
    tmp_etc = "/etc/rc.d/"
else:
    tmp_etc = "/etc/"
print(tmp_etc)
tmpfile = os.path.join(tmp_etc, "init.d/socket_server_monitor")
print(tmpfile)
with open(tmpfile, "w") as f:
    f.write(content)
os.system(f"chmod +x {tmpfile}")
if not os.path.isfile(f"{tmp_etc}rc3.d/S66socket_server_monitor"):
    os.system(f"cd {tmp_etc}rc3.d && ln -s ../init.d/socket_server_monitor S66socket_server_monitor")
if not os.path.isfile(f"{tmp_etc}rc3.d/K66socket_server_monitor"):
    os.system(f"cd {tmp_etc}rc3.d && ln -s ../init.d/socket_server_monitor K66socket_server_monitor")




# # 杀死所有的socket_server程序
# cmd = "kill `ps -ef|grep socket_server_|grep -v grep|awk '{print $2}'`"
# os.system(cmd)

# 自动创建config文件
if not os.path.isfile("config"):
    version = 0.0
    path_list = list()
    for path in os.listdir():
        logger.info(f"当前路径文件：{path}")
        if os.path.isfile(path) and re.match(r"socket_server_\d+\.\d+$", path):
            path_list.append(path)
    path_list.sort(key=lambda x: [int(x.rsplit("_", 1)[1].split(".", 1)[0]), int(x.rsplit("_", 1)[1].split(".", 1)[1])])
    version = path_list[-1].rsplit("_", 1)[1]   #排序取最大的版本号
    with open("config", "w") as f:
        f.write(f"version={version}\nport=9000")

config = dict()
latest_name = None
tmp_list1 = list()
while True:
    with open("config", "r") as f:
        content = f.read()
    for line in content.strip().split("\n"):
        if line:
            name, value = line.split("=")
            name = name.strip()
            value = value.strip()
            config[name] = value

    # 更新 latest_name
    for path in os.listdir():
        if os.path.isfile(path) and path == "socket_server_" + config["version"]:
            # logger.info(f"存在最新文件：{path}")
            if latest_name != path:
                logger.info(f"存在最新文件：{path}")
            latest_name = path
            break

    # 判断最新程序已经启动
    processes = psutil.process_iter()
    flag_start = False
    for process in processes:
        pid = process.pid
        pname = process.name()
        if pname == latest_name:
            flag_start = True
            str1 = f"{latest_name}程序已经启动"
            if str1 not in tmp_list1:
                logger.info(str1)
                tmp_list1.append(str1)
            break

    # 最新程序未启动，就先停掉socket_server相关进程，并重新启动最新程序
    if not flag_start:
        logger.info(f"{latest_name}程序未启动")
        processes = psutil.process_iter()
        for process in processes:
            pid = process.pid
            pname = process.name()
            # print(pname, pid)
            if re.match(r"socket_server_\d+\.\d+$", pname):
                logger.info(f"停止历史程序：{pname}")
                # 终止进程
                try:
                    process = psutil.Process(pid)
                    process.terminate()
                    time.sleep(5)
                    logger.info("进程已成功终止。")
                except psutil.NoSuchProcess:
                    logger.error("指定的进程ID不存在。")
                except psutil.AccessDenied:
                    logger.error("没有权限终止指定的进程。")

        logger.info(os.path.join(os.getcwd(), latest_name))
        logger.info(f"启动程序{latest_name}")
        os.chmod(latest_name, 0o700)
        subprocess.Popen([os.path.join(os.getcwd(), latest_name), '-p', config["port"]])

    time.sleep(5)
