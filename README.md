# socket_server

基于 TCP 的远程指令执行服务，支持命令执行、文件传输、抓包、发包、拨测。版本升级采用"中央推送"模式：查询/下载在中央节点（mcp_socket_server）完成，靶机零外呼。

## 安装（一键 / 覆盖安装）

```bash
# 在线一条命令装最新版（重复执行即升级、降级或覆盖修复；已有配置自动保留）
curl -fsSL https://github.com/weihang1258/socket_server/releases/latest/download/install.sh | sudo bash

# 指定版本安装 / 回退旧版
curl -fsSL https://github.com/weihang1258/socket_server/releases/latest/download/install.sh | sudo bash -s -- 1.8.0

# 离线安装（内网机器）：把 release 里的 socket_server 和 install.sh 放同一目录，然后
sudo ./install.sh
```

脚本行为：装 `/opt/socket/versions/<版本>/` → current 软链 → 生成 `/opt/socket/config`（已存在绝不覆盖）→ 注册 systemd 并启动。装完即可被中央节点管理。

<details>
<summary>手动安装（不用脚本时）</summary>

```bash
VER=1.8.0
curl -sLO https://github.com/weihang1258/socket_server/releases/download/v$VER/socket_server
curl -sLO https://github.com/weihang1258/socket_server/releases/download/v$VER/socket_server.sha256
sed 's|dist/socket_server|socket_server|' socket_server.sha256 | sha256sum -c

sudo mkdir -p /opt/socket/versions/$VER
sudo cp socket_server /opt/socket/versions/$VER/socket_server
sudo chmod +x /opt/socket/versions/$VER/socket_server
sudo ln -sfn /opt/socket/versions/$VER /opt/socket/versions/current
sudo /opt/socket/versions/current/socket_server enable
sudo /opt/socket/versions/current/socket_server start
```

</details>

## 启停服务

```bash
sudo /opt/socket/versions/current/socket_server start    # 启动
sudo /opt/socket/versions/current/socket_server stop     # 停止
```

## 升级（中央推送，1.8.0+）

靶机不再联网查询/下载（1.8.0 起）。升级由中央节点 MCP 工具一条命令完成：

```
upgrade_run(targets=["10.12.131.82"], version="1.8.0")
```

流程：确定版本（指定则零查询；留空自动查最新）→ 中央下载 → 经 :9001 推包到靶机 → datatype 20 校验落盘（sha256）→ 后台重启 → 轮询 dt14 确认。需靶机 1.8.0+（更早版本请先用 install.sh 装 1.8.0）。

本机命令只管本地切换（不再联网）：

```bash
/opt/socket/versions/current/socket_server switch 1.7.4    # 切换到本地已下载版本并重启
/opt/socket/versions/current/socket_server list            # 列出本地已下载版本
/opt/socket/versions/current/socket_server autoupgrade off # 关闭（拒绝中央升级下发）
/opt/socket/versions/current/socket_server autoupgrade on  # 开启（缺省）
```

config 里 `autoupgrade=` 键保留，语义为"是否接受升级下发"。

### 代理配置（内网环境）

systemd 启动的服务不继承 shell 的 `http_proxy` 环境变量。靶机侧 1.8.0 起升级不再联网；`/opt/socket/config` 的 `proxy=` 仍供拨测（datatype 131）等出网请求使用：

```
autoupgrade=on
proxy=http://10.12.186.204:7897
```

## 接口文档

客户端 TCP 协议接口详见 [docs/api-guide.md](docs/api-guide.md)。

## 系统依赖

服务运行需要以下系统工具和库（按需安装）：

```bash
# CentOS/RHEL
sudo yum install -y tcpdump ethtool net-tools
sudo yum install -y libX11 libXcomposite libXcursor libXdamage libXext \
    libXi libXtst cups-libs libXScrnSaver libXrandr GConf2 atk gtk3 \
    pango at-spi2-atk libwayland-client libwayland-cursor libwayland-egl \
    alsa-lib nss nspr

# Ubuntu/Debian
sudo apt-get install -y tcpdump ethtool net-tools
sudo apt-get install -y libx11-6 libxcomposite1 libxcursor1 libxdamage1 \
    libxext6 libxi6 libxtst6 libcups2 libxss1 libxrandr2 libgconf-2-4 \
    libatk1.0-0 libgtk-3-0 libpango-1.0-0 libpangocairo-1.0-0 \
    libwayland-client0 libwayland-cursor0 libasound2 libnss3 libnspr4 \
    libgbm1 libxshmfence1
```
