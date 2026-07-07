# socket_server

基于 TCP 的远程指令执行服务，支持命令执行、文件传输、抓包、发包、拨测，内置 GitHub 版本管理与自动升级。

## 安装

```bash
# 下载最新版
curl -sL https://api.github.com/repos/weihang1258/socket_server/releases/latest \
  | grep '"tag_name"' | sed 's/.*"v\(.*\)".*/\1/' > /tmp/_sv
VER=$(cat /tmp/_sv)
curl -sLO https://github.com/weihang1258/socket_server/releases/download/v$VER/socket_server
curl -sLO https://github.com/weihang1258/socket_server/releases/download/v$VER/socket_server.sha256

# 校验（sha256文件中路径为dist/socket_server，需替换为当前文件名）
sed 's|dist/socket_server|socket_server|' socket_server.sha256 | sha256sum -c

# 安装
sudo mkdir -p /opt/socket/versions/$VER
sudo cp socket_server /opt/socket/versions/$VER/socket_server
sudo chmod +x /opt/socket/versions/$VER/socket_server
sudo ln -sf /opt/socket/versions/$VER /opt/socket/versions/current

# 注册服务并启动
sudo /opt/socket/versions/current/socket_server enable
sudo /opt/socket/versions/current/socket_server start

# 验证
/opt/socket/versions/current/socket_server current
```

## 启停服务

```bash
sudo /opt/socket/versions/current/socket_server start    # 启动
sudo /opt/socket/versions/current/socket_server stop     # 停止
```

## 升级

```bash
sudo /opt/socket/versions/current/socket_server upgrade          # 升级到最新版
sudo /opt/socket/versions/current/socket_server switch 1.4.0    # 切换到指定版本
/opt/socket/versions/current/socket_server list                  # 查看可用版本
```

自动升级默认开启，每小时检查 GitHub，无客户端连接超过30分钟时自动切换并重启。

```bash
/opt/socket/versions/current/socket_server autoupgrade off   # 关闭
/opt/socket/versions/current/socket_server autoupgrade on    # 开启
```

### 代理配置（内网环境）

systemd 启动的服务**不继承** shell 的 `http_proxy` 环境变量。内网代理环境下若 GitHub 下载失败，需在配置文件 `/opt/socket/config` 中显式指定代理：

```bash
echo "proxy=http://10.12.186.204:7897" | sudo tee -a /opt/socket/config
```

配置后，自动升级 / 手动升级的所有 GitHub 请求都会走该代理；下载直连失败时也会用此代理自动重试一次。`/opt/socket/config` 完整示例：

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
