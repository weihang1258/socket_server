# socket_server Linux 打包部署手册

## 1. 环境要求

- OS: CentOS 7/8 或 Ubuntu 18.04+ (x86_64)
- Python: 3.8+
- 网络: 能访问 github.com 和 pypi.org（或使用内网镜像）

## 2. 获取代码

```bash
# 安装 git（如果未安装）
sudo yum install -y git          # CentOS
# sudo apt-get install -y git   # Ubuntu

# 克隆仓库
cd /opt
sudo git clone https://github.com/weihang1258/socket_server.git
cd socket_server
```

如果有代理：
```bash
sudo http_proxy=http://10.12.186.204:7897 https_proxy=http://10.12.186.204:7897 git clone https://github.com/weihang1258/socket_server.git
```

更新代码：
```bash
cd /opt/socket_server
git pull
```

## 3. 创建打包虚拟环境

```bash
# 安装 Python3（如果未安装），CentOS 示例
sudo yum install -y python3

# 在项目目录下创建虚拟环境
cd /opt/socket_server
python3 -m venv venv

# 激活虚拟环境
source venv/bin/activate
```

## 4. 安装依赖

有代理：
```bash
pip3 install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --proxy http://10.12.186.204:7897 \
    --upgrade pip

pip3 install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --proxy http://10.12.186.204:7897 \
    -r requirements.txt
```

无代理（直连）：
```bash
pip3 install --upgrade pip
pip3 install -r requirements.txt
```

依赖清单（requirements.txt）：
```
scapy
requests
psutil
wheel
pyinstaller
pyppeteer
packaging
```

> 如果 pyppeteer 安装失败（需要 chromium 下载），可以先跳过：`pip3 install scapy requests psutil wheel pyinstaller packaging`

## 5. 打包

```bash
# 确保虚拟环境已激活
source venv/bin/activate

# 查看当前版本号
python3 -c "from socket_server.version import VERSION; print(VERSION)"

# 执行打包（使用 spec 文件）
pyinstaller packaging/socket_server.spec --clean
```

打包过程约 1-3 分钟，产物在 `dist/` 目录下：

```bash
ls -lh dist/socket_server
# -rwxr-xr-x 1 root root 30M socket_server
```

这是单个可执行文件，无外部依赖，可直接部署。

## 6. 部署

### 6.1 首次部署

```bash
# 读取当前版本号
VER=$(python3 -c "from socket_server.version import VERSION; print(VERSION)")

# 创建目录结构
sudo mkdir -p /opt/socket/versions/$VER

# 复制二进制到版本目录
sudo cp dist/socket_server /opt/socket/versions/$VER/socket_server
sudo chmod +x /opt/socket/versions/$VER/socket_server

# 创建 current 符号链接
sudo ln -sf /opt/socket/versions/$VER /opt/socket/versions/current

# 安装 systemd 服务 + 开机自启
sudo /opt/socket/versions/current/socket_server enable

# 启动服务
sudo /opt/socket/versions/current/socket_server start
```

### 6.2 验证

```bash
# 查看当前版本
/opt/socket/versions/current/socket_server current

# 查看服务状态
systemctl status socket_server

# 查看日志
tail -f /var/log/socket_server.log
```

### 6.3 CLI 子命令速查

| 命令 | 说明 |
|------|------|
| `socket_server serve [-p PORT]` | 启动 TCP 服务（默认端口 9000） |
| `socket_server current` | 显示当前版本号 |
| `socket_server list` | 列出 GitHub 所有 Release 版本 |
| `socket_server upgrade` | 手动检查并升级到最新版 |
| `socket_server switch [VERSION]` | 切换到指定版本（交互或直接指定） |
| `socket_server start` | 启动服务 (systemctl start) |
| `socket_server stop` | 停止服务 (systemctl stop) |
| `socket_server enable` | 安装 systemd unit + 开机自启 |
| `socket_server disable` | 禁用开机自启 |
| `socket_server autoupgrade on\|off` | 开关自动升级（默认 on） |

## 7. 版本升级

### 7.1 手动升级

```bash
# 查看可用版本
/opt/socket/versions/current/socket_server list

# 升级到最新版（自动下载 + 自动重启）
sudo /opt/socket/versions/current/socket_server upgrade

# 或切换到指定版本
sudo /opt/socket/versions/current/socket_server switch <版本号>
```

### 7.2 自动升级

服务启动后默认开启自动升级，每 1 小时检查 GitHub：
- 有新版本 → 自动下载
- 无客户端连接 + 断开超过 30 分钟 → 自动切换并重启

```bash
# 关闭自动升级
/opt/socket/versions/current/socket_server autoupgrade off

# 开启自动升级
/opt/socket/versions/current/socket_server autoupgrade on
```

配置文件位置：`/opt/socket/config`，内容：
```
autoupgrade=on
```

## 8. 发版流程（开发者）

在开发机上发布新版本到 GitHub：

```bash
# 1. 更新版本号
#    编辑 socket_server/version.py，修改 VERSION = "新版本号"

# 2. 提交并推送
git add socket_server/version.py
git commit -m "release: v新版本号"
git push

# 3. 在 Linux 打包机上拉取最新代码并打包
cd /opt/socket_server
git pull
source venv/bin/activate
pyinstaller packaging/socket_server.spec --clean

# 4. 读取版本号
VER=$(python3 -c "from socket_server.version import VERSION; print(VERSION)")

# 5. 生成 sha256 校验
sha256sum dist/socket_server > dist/socket_server.sha256

# 6. 创建 GitHub Release（需要 gh CLI 或在网页操作）
gh release create v$VER dist/socket_server dist/socket_server.sha256 \
    --title "v$VER" \
    --notes "版本更新说明"
```

靶机上的 `socket_server upgrade` 或自动升级线程会自动检测到新版本。

## 9. 靶机依赖安装

服务运行需要以下系统工具和库：

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

## 10. 目录结构

```
/opt/socket/
├── config                              # 配置文件（autoupgrade=on）
└── versions/
    ├── current -> /opt/socket/versions/<当前版本>   # 符号链接，指向当前使用的版本
    └── <版本号>/
        └── socket_server              # 可执行文件
```

```
/etc/systemd/system/socket_server.service   # systemd unit 文件
/var/log/socket_server.log                  # 服务日志
```
