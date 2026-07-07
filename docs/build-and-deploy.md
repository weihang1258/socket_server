# socket_server Linux 打包部署手册

## 1. 环境要求

- OS: CentOS 7/8 或 Ubuntu 18.04+ (x86_64)
- glibc: ≥ 2.17（二进制最高需 `GLIBC_2.14`，CentOS 7.5 原生支持）
- 网络: 能访问 github.com 和 pypi.org（或使用内网代理，见第 7 节）

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

## 3. 打包环境（CentOS 7 容器）

> ⚠️ **重要**：打包机的 glibc 必须 **≤ 目标靶机的 glibc**。Ubuntu 20.04（glibc 2.31）打的二进制在 CentOS 7.5（glibc 2.17）上会因 `GLIBC_2.29 not found` 启动崩溃。必须用 CentOS 7 系环境打包。

项目提供 `packaging/Dockerfile.builder`，基于 CentOS 7 自编译 Python 3.8 `--enable-shared`，产物最高仅需 `GLIBC_2.14`，兼容 CentOS 7/8、Ubuntu 18.04+。

### 3.1 构建打包镜像（一次性）

```bash
cd /opt/socket_server

# 下载 Python 源码到 packaging/（容器内不下载，避免代理问题）
wget -q https://mirrors.tuna.tsinghua.edu.cn/python/3.8.10/Python-3.8.10.tgz \
  -O packaging/Python-3.8.10.tgz

# 构建镜像（约 3-5 分钟，含 Python 编译）
docker build --network host \
  -t socket_server-builder:centos7-py38 \
  -f packaging/Dockerfile.builder packaging/
```

构建成功后镜像可反复用，后续打包秒级启动。

> Docker daemon 若需走代理拉镜像/装包，参考 `packaging/Dockerfile.builder` 内的 yum proxy 与 pip `--proxy` 配置，按实际代理地址修改。

## 4. 打包

```bash
cd /opt/socket_server

# 清理旧产物
rm -rf dist build

# 容器内打包（挂载项目目录，产物直接落到宿主 dist/）
docker run --rm \
  -v /opt/socket_server:/work \
  -w /work \
  socket_server-builder:centos7-py38 \
  sh -c '
    pip3 install --proxy http://10.12.186.204:7897 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      scapy requests psutil wheel pyinstaller packaging pyppeteer
    pyinstaller packaging/socket_server.spec --clean --noconfirm
  '

# 查看产物
ls -lh dist/socket_server
# -rwxr-xr-x 1 root root 16M socket_server

# 验证 glibc 需求（应 ≤ 2.17，CentOS 7.5 能跑）
docker run --rm -v /opt/socket_server:/work socket_server-builder:centos7-py38 \
  sh -c 'readelf -V /work/dist/socket_server 2>/dev/null | grep -oE "GLIBC_[0-9.]+" | sort -u -V | tail -3'

# 验证版本号
./dist/socket_server current
```

产物为单文件可执行，无外部 Python 依赖，可直接部署。

> 依赖清单见 `requirements.txt`：scapy、requests、psutil、wheel、pyinstaller、pyppeteer、packaging。pyppeteer 若装失败可先跳过（运行时按需用）。

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
# enable 会把 ExecStart 写成 /opt/socket/versions/current/socket_server（指向 current 链接）
# 这样后续 switch_to 切换链接后重启即可生效
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
proxy=http://10.12.186.204:7897
```

`proxy` 字段可选。systemd 启动的服务不继承 shell 的 `http_proxy` 环境变量，
内网代理环境下需在此显式配置，自动升级/手动升级下载才会走代理。下载失败时会用此代理自动重试一次。

### 7.3 从 v1.3.2 及更早版本升级（一次性手动恢复）

v1.3.2 及更早版本的 `enable` 会把 systemd unit 的 `ExecStart` 写死成具体版本路径（如 `/opt/socket/versions/1.3.0/socket_server`），导致 `switch_to` 切换符号链接后重启仍跑旧版本，自动升级陷入"切换成功、重启后版本不变"的死循环。

v1.3.3+ 修复了此问题，且 `serve` 启动时会自愈检查并重写错误的 unit。但旧版本卡在循环里无法自动切到新版，需手动执行一次：

```bash
# 1. 确认新版已下载到磁盘（autoupgrade 会自动下载，或手动 switch 触发下载）
ls /opt/socket/versions/1.3.4/socket_server

# 2. 用新版二进制重写 systemd unit（指向 current 链接）
sudo /opt/socket/versions/1.3.4/socket_server enable

# 3. 重启
sudo systemctl restart socket_server

# 4. 验证已切到新版
/opt/socket/versions/current/socket_server current
```

执行一次后，新版启动时 `ensure_unit_correct()` 会确认 unit 正确，**今后所有自动升级全程自愈，无需再手动介入**。

## 8. 发版流程（开发者）

在打包机上发布新版本到 GitHub：

```bash
cd /opt/socket_server

# 1. 更新版本号
#    编辑 socket_server/version.py，修改 VERSION = "新版本号"
#    并在 CHANGELOG.md 顶部补充本版变更

# 2. 提交并推送
git add socket_server/version.py CHANGELOG.md
git commit -m "release: v新版本号"
git push

# 3. 打 tag 并推送
git tag -a v新版本号 -m "v新版本号 — 变更摘要"
git push origin v新版本号

# 4. 容器打包（见第 4 节）
rm -rf dist build
docker run --rm -v /opt/socket_server:/work -w /work \
  socket_server-builder:centos7-py38 \
  sh -c 'pip3 install --proxy http://10.12.186.204:7897 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      scapy requests psutil wheel pyinstaller packaging pyppeteer && \
    pyinstaller packaging/socket_server.spec --clean --noconfirm'

# 5. 生成 sha256 校验
sha256sum dist/socket_server > dist/socket_server.sha256

# 6. 创建 GitHub Release（本机 gh 不是标准 GitHub CLI，用 API 上传）
#    从 CHANGELOG 提取本版 notes，调用 releases API 创建并上传 assets
python3 <<'PY'
import json, os, re, urllib.request
# token 从 ~/.git-credentials 读取（credential.helper=store 已配置）
with open(os.path.expanduser('~/.git-credentials')) as f:
    token = re.search(r'ghp_[A-Za-z0-9]+', f.read()).group()
VER = "新版本号"   # ← 改成实际版本号
# ... 读 CHANGELOG 取 notes，POST /releases，再 POST upload_url 上传二进制和 sha256
PY
```

> 第 6 步也可在 GitHub 网页直接操作：Releases → Draft a new release → 选 tag → 粘贴 CHANGELOG notes → 上传 `dist/socket_server` 和 `dist/socket_server.sha256`。

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
