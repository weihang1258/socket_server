# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`socket_server` — TCP-based remote command execution service with traffic capture/replay, measurement (boce), built-in GitHub version management and auto-upgrade. Deployed as a single PyInstaller binary to CentOS 7+ / Ubuntu 18.04+ targets.

## Build & Release

### Build (CentOS 7 container required for glibc 2.17 compatibility)

```bash
# One-time: build builder image
docker build --network host -t socket_server-builder:centos7-py38 \
  -f packaging/Dockerfile.builder packaging/

# Build binary
rm -rf dist build
docker run --rm -v $(pwd):/work -w /work socket_server-builder:centos7-py38 \
  sh -c 'pip3 install --proxy http://10.12.186.204:7897 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    scapy requests psutil wheel pyinstaller packaging pyppeteer && \
  pyinstaller packaging/socket_server.spec --clean --noconfirm'
```

### Release（标准发版流程，逐步执行）

> **经验**：1.5.0/1.5.1 发版踩过坑——`gh release create` 带 `dist/socket_server`（16M 二进制）上传的组合，Claude Code 的 Bash 安全分类器会间歇性拒绝（Stage 2 classifier error），重试也不放行，浪费大量时间。**不要反复重试同一条被挡的命令**。处理见下方"分类器拒绝时"。

**1. 改版本号 + CHANGELOG + release_notes**
- `socket_server/version.py` 改 `VERSION`。
- `CHANGELOG.md` 顶部加新版本段（日期、兼容 DPI 版本、变更条目）。
- 跑 `python3 packaging/generate_release_notes.py` 重新生成 `socket_server/release_notes.py`（`.gitignore` 不入库，打包内嵌）。

**2. 本地验证（提交前必做）**
- `python3 -m py_compile` 改动的 .py 文件。
- 若改动是 bug 修复，构造最小复现确认旧 bug 消失、新行为正确（别只看编译通过）。

**3. 提交 + tag + push**
```bash
git add <改动的文件>
git commit -m "<type>: <version> <说明>"
git tag -a v<version> -m "<version>: <一句话>"
git push origin main
git push origin v<version>
```

**4. 构建（CentOS 7 builder）**
```bash
rm -rf dist build
docker run --rm -v $(pwd):/work -w /work socket_server-builder:centos7-py38 \
  sh -c 'pip3 install --proxy http://10.12.186.204:7897 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    scapy requests psutil wheel pyinstaller packaging pyppeteer && \
  pyinstaller packaging/socket_server.spec --clean --noconfirm'
```
- 看 exit code + `ls -lh dist/socket_server`（应 16M，ELF x86-64）。
- `./dist/socket_server current` 确认版本号。
- 改动涉及运行时行为的，起 `serve` 跑一次对应 datatype 端到端验证。

**5. sha256**
```bash
sha256sum dist/socket_server | tee dist/socket_server.sha256
```

**6. 生成 release notes 文件 + 创建 Release**
```bash
python3 -c "from socket_server.release_notes import RELEASE_NOTES; print(RELEASE_NOTES.strip())" > /tmp/release_notes_<version>.md
HTTPS_PROXY=http://10.12.186.204:7897 gh release create v<version> \
  -R weihang1258/socket_server --title "v<version>" \
  --notes-file /tmp/release_notes_<version>.md \
  dist/socket_server dist/socket_server.sha256
```
- `gh` 走代理（直连 GitHub API 偶发 EOF）。
- 验证：`HTTPS_PROXY=http://10.12.186.204:7897 gh release view v<version> -R weihang1258/socket_server --json tagName,url,assets`，确认 assets 含 `socket_server` + `socket_server.sha256`。

**分类器拒绝 `gh release create` 时（已多次发生）**

- 判定特征：Claude Code 返回 `Stage 2 classifier error` 或 `claude-sonnet-5 is temporarily unavailable, so auto mode cannot determine the safety of Bash`，**连续重试 2-3 次仍不放行**。
- 不要继续重试。直接让用户在**真实 bash 终端**（不是 Claude Code 输入框）手动跑第 6 步的命令——终端的 `!` 是历史展开会报 `event not found`，所以**去掉命令开头的 `!`**，直接 `HTTPS_PROXY=... gh release create ...` 即可。tag/二进制/sha256/notes 都已就绪，用户跑完这一条即发版完成。
- 仓库**没有 CI 自动发版**：`.github/workflows/` 不存在，所有 release 的 `created_via` 都是 `null`，全部手动 `gh release create` 发。不要假设有自动路径可抄。

**发版后清理**
- 跑完 `serve` 验证的，记得 `pkill -f "dist/socket_server serve"` 关掉残留进程。

## Architecture

### Entry point
- `packaging/entry.py` → `cli.py:main()` → subcommands (`serve`, `upgrade`, `switch`, `list`, `start`/`stop`/`enable`/`disable`)

### Core modules (4052 lines total)
- **cli.py** — CLI argument parsing, subcommand dispatch
- **protocol.py** — TCP protocol handler (`MyTCPHandler`): binary framing (4-byte length prefix + `datatype` + payload), file transfer, compression
- **handlers.py** — Datatype dispatch (`do(datatype, data)`): 0=scapy packet send, 14=version, 131=boce (browser measurement), file ops, capture start/stop, etc.
- **server.py** — TCP server launcher (IPv4+IPv6), client connection tracking for upgrade idle check
- **netutils.py** — Network utilities: `exec_cmd_subprocess` (always `shell=False` + list args — never shell injection), routeinfo, MTU, file ops, subprocess env cleanup
- **capture.py** — tcpdump/dumpcap integration (start/stop/isrun), scapy sniff fallback
- **replayer.py** — PCAP replay via tcpreplay (898 lines)
- **boce.py** — HTTP/HTTPS measurement via `requests` (simple) or `pyppeteer` (browser)
- **upgrader.py** — GitHub API integration: raw version check (CDN), ETag cache, binary download + sha256 verify, version switching
- **autoupgrade.py** — Auto-upgrade background thread: hourly check via raw CDN → download via API → switch when idle ≥30min
- **supervisor.py** — systemd unit management (enable/disable/start/stop/restart), unit self-heal on serve startup
- **socket_listen.py** — Port listening detection
- **pcap_flow.py** — PCAP flow extraction
- **version.py** — `VERSION` + `REPO`

### Key patterns
- **No shell injection**: All subprocess calls use `exec_cmd_subprocess(args=[...], shell=False)`. Never `shell=True` with string commands.
- **Proxy handling**: `/opt/socket/config`'s `proxy=http://host:port` — read live on every request. All HTTP(S) logs tagged with `(via proxy ...)` or `(直连)`.
- **GitHub API rate limit avoidance**: `get_latest_version_raw()` from raw CDN (0 quota). `get_latest()`/`get_releases()` use ETag (304 = 0 quota).
- **Thread safety**: `_browser_lock` for pyppeteer browser, `_client_lock` for connection tracking, `_cpu_binding_lock` for CPU pinning.
- **Binary glibc**: Must be built on CentOS 7 (glibc 2.17) to run on CentOS 7.5+ targets. Ubuntu builds produce glibc 2.31 binaries that crash on CentOS 7.

## MCP Integration

socket_server 作为执行单元被 LLM 平台调用时，通过独立的 MCP server 项目桥接，**不要把 MCP 代码塞进本仓库**。详见 `docs/mcp-integration.md`。

### 架构关系
```
LLM 客户端 ──MCP协议──► mcp_socket_server（独立项目，中央节点）
                              │ TCP 客户端（复用本仓库 test_e2e.py 逻辑）
                              ▼
                      socket_server（本项目，每台靶机 :9000）
```

### 关键约定
- **MCP server 是独立项目**，位于 `/opt/mcp_socket_server`（与本仓库同级），单独 git 仓库、单独部署、单独发版
- **共享契约是协议**：datatype 编号 + 参数 schema 的权威定义在本仓库，MCP server 据此生成工具 schema，勿手抄
- **客户端库复用**：MCP server 连接 socket_server 的 TCP 客户端逻辑，源自本仓库 `test_e2e.py`（`send_request`/`recv_raw`/`recv_file` 等）。若 socket_server 协议变更，同步更新 `test_e2e.py` 客户端逻辑
- **两套抓包**：业务用 datatype 5/6（tcpdump 命令行，按 网卡+pcap路径 区分，支持同靶机并发）；datatype 121/122/123（scapy 单实例）已弃用，MCP 层勿暴露
- **危险操作分级**：`version_switch`/`firewall_disable`/`cmd_exec` 需 MCP 层二次确认；只读类（version/isfile/routeinfo）可自由并发