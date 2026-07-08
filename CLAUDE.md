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