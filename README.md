# socket_server

基于 TCP 的远程指令执行服务，支持命令执行、文件传输、抓包、发包、拨测等功能，内置 GitHub 版本管理与自动升级。

## 功能概览

| 类别 | 功能 |
|------|------|
| 远程执行 | Shell 命令执行、Python 表达式求值 |
| 文件操作 | 文件上传/下载、目录创建、文件/目录判断、zip解压 |
| 网络工具 | 路由查询、MTU修改、命令检测 |
| 抓包 | tcpdump 抓包、scapy 抓包/下载 |
| 发包 | pcap 回放、五元组流提取 |
| 拨测 | 基于 chromium 的网页拨测 |
| 版本管理 | GitHub Release 升级、版本切换、自动升级 |

## 环境要求

- CentOS 7/8 或 Ubuntu 18.04+ (x86_64)
- Python 3.8+（仅打包时需要，靶机无需安装 Python）
- glibc 2.17+（CentOS 7 自带，打包产物向下兼容）

## 快速安装

### 1. 下载

从 [GitHub Releases](https://github.com/weihang1258/socket_server/releases) 下载最新版本的 `socket_server` 和 `socket_server.sha256`。

### 2. 校验

```bash
sha256sum -c socket_server.sha256
```

### 3. 安装

```bash
VER=$(cat socket_server.sha256 | awk '{print $2}' | sed 's/socket_server-//')

sudo mkdir -p /opt/socket/versions/$VER
sudo cp socket_server /opt/socket/versions/$VER/socket_server
sudo chmod +x /opt/socket/versions/$VER/socket_server
sudo ln -sf /opt/socket/versions/$VER /opt/socket/versions/current
```

### 4. 安装系统依赖

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

### 5. 注册服务并启动

```bash
sudo /opt/socket/versions/current/socket_server enable   # 安装 systemd 服务 + 开机自启
sudo /opt/socket/versions/current/socket_server start     # 启动服务
```

### 6. 验证

```bash
/opt/socket/versions/current/socket_server current        # 查看版本号
systemctl status socket_server                            # 查看服务状态
```

## 服务管理

| 命令 | 说明 |
|------|------|
| `socket_server serve [-p PORT]` | 前台启动 TCP 服务（默认端口 9000） |
| `socket_server start` | 通过 systemd 启动服务 |
| `socket_server stop` | 通过 systemd 停止服务 |
| `socket_server enable` | 安装 systemd 服务 + 开机自启 |
| `socket_server disable` | 禁用开机自启 |

## 版本升级

### 手动升级

```bash
# 查看可用版本
/opt/socket/versions/current/socket_server list

# 升级到最新版
sudo /opt/socket/versions/current/socket_server upgrade

# 切换到指定版本
sudo /opt/socket/versions/current/socket_server switch 1.4.0
```

### 自动升级

服务启动后默认开启自动升级，每小时检查 GitHub：
- 有新版本 → 自动下载到 `/opt/socket/versions/<版本号>/`
- 无客户端连接且断开超过 30 分钟 → 自动切换并重启

```bash
# 关闭自动升级
/opt/socket/versions/current/socket_server autoupgrade off

# 开启自动升级
/opt/socket/versions/current/socket_server autoupgrade on
```

配置文件：`/opt/socket/config`，内容为 `autoupgrade=on`。

## 版本切换

所有版本存放在 `/opt/socket/versions/` 下，通过符号链接 `current` 指向当前使用的版本：

```
/opt/socket/versions/
├── current -> /opt/socket/versions/1.3.0
├── 1.3.0/
│   └── socket_server
└── 1.4.0/
    └── socket_server
```

切换版本时仅修改符号链接，再重启服务，旧版本文件保留不删除。

## 日志

```bash
tail -f /var/log/socket_server.log
```

## 接口文档

客户端 TCP 协议接口详见 [docs/api-guide.md](docs/api-guide.md)。

## 开发者

打包和发版流程详见 [docs/build-and-deploy.md](docs/build-and-deploy.md)。
