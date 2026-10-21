#!/usr/bin/env bash
# socket_server 安装脚本（v1，Linux x86_64）
#
# 用法一（离线，推荐）：发布包解开后在包目录内执行（目录内需有 socket_server 二进制）
#   sudo ./install.sh
#
# 用法二（在线一条命令）：零参数自动安装最新发布版本（无需指定版本号）
#   curl -fsSL https://github.com/weihang1258/socket_server/releases/latest/download/install.sh | sudo bash
#
#   指定版本安装 / 回退旧版（重复执行即升级、降级或覆盖修复，配置自动保留）：
#   curl -fsSL <上述地址> | sudo bash -s -- 1.8.0
#
# 脚本行为：装 /opt/socket/versions/<版本>/ → current 软链 → 生成 /opt/socket/config
# （已存在绝不覆盖）→ 复用二进制自带 enable 子命令注册 systemd → 启动 → 打印结果。
set -euo pipefail

INSTALL_DIR=/opt/socket
VERSIONS_DIR=$INSTALL_DIR/versions
CONFIG_FILE=$INSTALL_DIR/config
SERVICE=socket_server
REPO_BASE=https://github.com/weihang1258/socket_server/releases/download
BURNED_VERSION="__RELEASE_VERSION__"

log()  { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }

# ---- 0. 前置检查 -----------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo ./install.sh）"
command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl：本脚本依赖 systemd"
[ "$(uname -m)" = "x86_64" ] || die "v1 仅支持 x86_64，当前 $(uname -m)"

# 最新 release 的 tag：releases/latest 302 跳转到 .../tag/v<版本>，取尾段剥 v。
# 不能带 -L：跟随重定向后 redirect_url 恒为空，必须只读 302 的 Location。
latest_tag() {
    local url
    url=$(curl -fsS -o /dev/null -w '%{redirect_url}' --connect-timeout 10 --max-time 30 \
        "${REPO_BASE%/download}/latest") && { url=${url##*/}; url=${url#v}; [ -n "$url" ] && printf '%s' "$url"; }
}

# 本地包模式判定：$0 必须是真实存在的 install.sh 文件，且同目录有 socket_server
# 二进制。curl|bash 管道模式 $0 是 "bash"，恒走在线分支。
SRC_DIR=""
if [ -f "$0" ] && [ "$(basename "$0")" = "install.sh" ]; then
    _d="$(cd "$(dirname "$0")" && pwd)"
    [ -f "$_d/socket_server" ] && [ -x "$_d/socket_server" ] && SRC_DIR="$_d"
fi

VERSION="${1:-}"
if [ -z "$SRC_DIR" ]; then
    # 版本解析只属在线分支：本地包安装可能是离线机器，不值得白等网络超时
    [ -n "$VERSION" ] || VERSION=$(latest_tag) || VERSION=""
    [ -n "$VERSION" ] || VERSION="$BURNED_VERSION"
    case "$VERSION" in v[0-9]*) VERSION="${VERSION#v}" ;; esac
    [ "$VERSION" != "__RELEASE_VERSION__" ] || die "在线模式版本解析失败，请显式传版本号：install.sh 1.8.0"
    TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
    log "下载 v$VERSION ..."
    # curl 必须带超时：下载地址不可达时无超时会永久挂死（实机安装测出）
    curl -fsSL --connect-timeout 10 --max-time 300 --retry 2 -o "$TMP/socket_server" \
        "$REPO_BASE/v$VERSION/socket_server" \
        || die "二进制下载失败（网络不可达或超时）。需代理访问 GitHub：sudo -E bash -c 'curl -fsSL <install.sh 地址> | bash'（-E 保留代理环境变量）"
    if curl -fsSL --connect-timeout 10 --max-time 60 -o "$TMP/socket_server.sha256" \
        "$REPO_BASE/v$VERSION/socket_server.sha256" 2>/dev/null; then
        # sha256 文件里路径是 dist/socket_server，替换成本地文件名再校验
        sed 's|dist/socket_server|socket_server|' "$TMP/socket_server.sha256" \
            | (cd "$TMP" && sha256sum -c -) || die "SHA256 校验失败，文件可能被篡改"
    else
        warn "无 sha256 校验文件，跳过校验"
    fi
    SRC_DIR="$TMP"
fi

# ---- 1.5 升级场景：记旧版本（current 软链名即版本号，零执行零网络）、停旧服务 ----
OLD_VERSION=""
if [ -L "$VERSIONS_DIR/current" ]; then
    OLD_VERSION=$(basename "$(readlink "$VERSIONS_DIR/current")")
fi
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}.service"; then
    systemctl stop "$SERVICE" 2>/dev/null || true
    if [ -n "$OLD_VERSION" ]; then
        log "已停止运行中的服务（原 v$OLD_VERSION），开始覆盖安装"
    fi
fi

# ---- 2. 安装二进制 ----------------------------------------------------------
mkdir -p "$VERSIONS_DIR/$VERSION"
install -m 0755 "$SRC_DIR/socket_server" "$VERSIONS_DIR/$VERSION/socket_server"
ln -sfn "$VERSIONS_DIR/$VERSION" "$VERSIONS_DIR/current"

# ---- 3. 配置（已存在绝不覆盖；proxy= 供拨测/中央下载参考，autoupgrade 接受下发）----
if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$INSTALL_DIR"
    printf 'autoupgrade=on\n' > "$CONFIG_FILE"
    log "已生成默认配置 $CONFIG_FILE"
else
    log "检测到已有配置 $CONFIG_FILE —— 保留不动（升级场景）"
fi

# ---- 4. 系统依赖（best-effort：失败仅告警；抓包为进程内 AF_PACKET，不依赖 tcpdump）----
if command -v yum >/dev/null 2>&1; then
    yum install -y ethtool net-tools >/dev/null 2>&1 \
        || warn "系统依赖安装失败（不影响核心功能，可手动 yum install -y ethtool net-tools）"
elif command -v apt-get >/dev/null 2>&1; then
    apt-get install -y ethtool net-tools >/dev/null 2>&1 \
        || warn "系统依赖安装失败（不影响核心功能，可手动 apt-get install -y ethtool net-tools）"
fi

# ---- 5. systemd（复用二进制自带 enable：写 unit + daemon-reload + enable 自愈）----
"$VERSIONS_DIR/current/socket_server" enable || die "systemd 注册失败"
systemctl restart "$SERVICE"
sleep 1
systemctl --quiet is-active "$SERVICE" || {
    journalctl -u "$SERVICE" -n 30 --no-pager
    die "服务启动失败，见上方日志"
}

# ---- 6. 就绪信息 -------------------------------------------------------------
ACTIVE=$(systemctl is-active "$SERVICE")
if [ -n "$OLD_VERSION" ] && [ "$OLD_VERSION" != "$VERSION" ]; then
    log "──────────────────────────────────────────────────────"
    log "✅ 升级完成：v$OLD_VERSION → v$VERSION"
else
    log "──────────────────────────────────────────────────────"
    log "✅ v$VERSION 安装完成（开机自启）"
fi
log ""
log "   服务状态 :  $ACTIVE （systemctl status $SERVICE 查看详情）"
log "   监听端口 :  TCP :9000（指令） / :9001（HTTP 文件服务，config 里 http_port=0 可关闭）"
log "   日志查询 :  tail -f /var/log/socket_server.log   或   journalctl -u $SERVICE -f"
log "   版本回退 :  $VERSIONS_DIR/current/socket_server switch <旧版本号>（仅限本地已下载版本）"
log "   后续升级 :  中央节点 MCP 工具 upgrade_run（推送+落地+确认一条命令）"
log "──────────────────────────────────────────────────────"
