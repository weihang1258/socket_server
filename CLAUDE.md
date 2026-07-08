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

### Release

```bash
# Bump version in socket_server/version.py + update CHANGELOG.md
git tag -a v<version> -m "message"
git push origin v<version>
# Create GitHub Release via API or web, upload dist/socket_server + sha256
```

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