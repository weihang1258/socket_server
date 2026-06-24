# socket_server 重构与升级功能设计

日期：2026-06-24
基线提交：`91f6dd9`（Initial commit: socket_server 1.2.1 + monitor）

## Context（背景与目标）

当前项目存在三个问题：

1. **两个程序文件职责重叠**：`socket_server.py`（2789 行）是 TCP 服务主体，`socket_server_monitor.py` 是 supervisor，负责选版本、拉起、崩溃重启、装自启脚本。monitor 含大量死代码（NUMA/CPU 亲和性算了但从未使用，106-136 行），自启逻辑用 init.d/rc3.d，冗余且分散。
2. **三处版本号不一致**：`socket_server.py` 内 `version="1.2.1"`、打包产物文件名 `socket_server_1.2`、monitor 的 `config` 各自独立，无法作为升级依据。
3. **无升级能力**：新版本只能人工 scp 替换，没有版本管理、无法查看可用版本、无法切换、无法从 GitHub 拉取。

目标：

- 将 monitor 合并进主程序，源码按职责多模块拆分（打包仍为单文件二进制，部署习惯不变）。
- 自启与崩溃重启交给 systemd，删除 init.d/rc3.d 与 monitor 死代码。
- 实现手动升级功能：单一二进制 + 子命令，从 GitHub Releases 获取版本，支持升级、list 查看版本与介绍、switch 切换版本、current 查看当前版本、start/stop/enable 手动启停与自启。
- 实现自动升级：默认开启，定时检查 GitHub，有客户端连接时不升级，无客户端且超 30 分钟无连接时升级并重启；可由 CLI 开关。
- 统一版本号到唯一来源。

部署前提：靶机（`/opt/socket/`）可访问 github.com。

## 一、项目结构

```
socket_server/
├── socket_server/                 # 主包
│   ├── __init__.py                # logger 配置、serve 时启动钩子（关防火墙、env）
│   ├── __main__.py                # 入口：python -m socket_server → dispatch
│   ├── cli.py                     # 子命令解析与派发
│   ├── version.py                 # 唯一版本源：VERSION + REPO（烘焙常量）
│   ├── server.py                  # ThreadingTCPServer + 客户端连接计数
│   ├── protocol.py                # MyTCPHandler 收包/分包/文件传输协议
│   ├── handlers.py                # do() 指令派发（datatype 0..200）
│   ├── replayer.py                # PcapReplayer / FlowInfo / PcapStats / PacketBuffer
│   ├── capture.py                 # tcpdump_start/stop/isrun / Tcpdump_scapy / SingleQueueRxThread
│   ├── boce.py                    # RequestChecker / PyppeteerChecker / BoceChecker / BrowserManager
│   ├── netutils.py                # routeinfo/mtu/isfile/isdir/mkdir/exec_cmd_subprocess/wait_until...
│   ├── socket_listen.py           # SocketServerListen
│   ├── pcap_flow.py               # extract_pcap_flow_five_tuples
│   ├── upgrader.py                # GitHub API / 下载 / 校验 / 切换 / list / switch / current
│   ├── supervisor.py              # systemd unit 安装/卸载、start/stop/enable
│   └── autoupgrade.py             # 后台线程：定时检查 + 客户端空闲判定 + 触发升级
├── packaging/
│   └── socket_server.spec         # pyinstaller --onefile 规范
├── requirements.txt
├── 打包命令
└── docs/superpowers/specs/
```

- monitor 完全删除。"选最新版本启动"由 `versions/current` 符号链接 + systemd 替代；崩溃重启由 systemd `Restart=always` 替代。
- 入口从 `socket_server.py` 改为包；`pyinstaller` 打包 `socket_server/__main__.py`，产物名 `socket_server`（不带版本号文件名）。

## 二、版本与部署模型

**唯一版本源** `socket_server/version.py`：

```python
VERSION = "1.3.0"
REPO = "owner/repo"        # GitHub owner/repo，烘焙进二进制
```

打包脚本读 `VERSION` 命名 GitHub Release tag（`v1.3.0`）；产物文件名统一为 `socket_server`（版本号在二进制内，不靠文件名识别）。

**部署目录** `/opt/socket/`：

```
/opt/socket/
├── versions/
│   ├── 1.3.0/socket_server
│   ├── 1.2.1/socket_server
│   └── current → 1.3.0        # 符号链接指向当前版本目录
├── config                     # 开关：autoupgrade=on/off
└── socket_server.service      # systemd unit（enable 子命令生成）
```

- 二进制内嵌版本号，彻底解决三处版本号不一致。
- `versions/current` 符号链接指向当前版本目录；systemd `ExecStart=/opt/socket/versions/current/socket_server serve`。
- 切换版本 = 改符号链接 + `systemctl restart`，原子且简单。
- 每个版本独立目录，保留历史版本供 `switch` 选择（不回滚，但保留可切换）。

**systemd unit**（`enable` 子命令生成）：

```ini
[Service]
ExecStart=/opt/socket/versions/current/socket_server serve
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

看门狗（崩溃重启）完全交给 `Restart=always`。

## 三、CLI 子命令

入口：`socket_server <subcommand> [options]`

| 子命令 | 作用 | 说明 |
|---|---|---|
| `serve [-p PORT]` | 启动 TCP 服务 + 自动升级后台线程 | systemd 跑这个；默认端口 9000 |
| `upgrade` | 立即检查 GitHub 并升级到最新版 | 当前有活跃客户端时拒绝并提示 |
| `list` | 列出 GitHub 所有 Release 的版本号 + notes | 本地已下载版本标注 `*` |
| `switch [VERSION]` | 切换到指定版本；不带参数则交互选择 | 本地无则自动下载 + 改符号链接 + 重启 |
| `current` | 显示当前版本号 + 版本 notes | 从本地二进制 VERSION + 缓存 notes |
| `start` | `systemctl start socket_server` | |
| `stop` | `systemctl stop socket_server` | |
| `enable` | 安装 systemd unit + `systemctl enable` + 建目录结构 | |
| `disable` | `systemctl disable socket_server` | |
| `autoupgrade <on\|off>` | 开关自动升级 | 写 `config`，运行中服务热加载 |

- 保留 `-p` 短选项，归到 `serve` 下（`socket_server serve -p 9000`）。
- 无子命令时打印 help。
- `list` 直接打印表格；`switch` 不带版本号时打印带序号列表，提示输入序号选择。

## 四、自动升级逻辑

后台线程（`serve` 时启动，daemon）：

```
每 1 小时：
  1. GET https://api.github.com/repos/{REPO}/releases/latest
  2. 解析 tag_name (v1.3.0 → 1.3.0)，用 packaging.version.parse 与当前 VERSION 比较
  3. 若有新版 且 versions/{new_ver}/ 不存在：
       下载 asset → versions/{new_ver}/.staging → 校验 sha256 → rename
       （已下载则跳过，不重复下载）
  4. 判定是否可切换：
       - 当前活跃客户端数 == 0
       - 且 距上次客户端断开 > 30 分钟
     不可切换 → 等下一轮，已下载版本保留
  5. 可切换 → 改 versions/current 符号链接 → systemctl restart
```

**客户端连接追踪**（`server.py` 维护，`threading.Lock` 保护）：

- `MyTCPHandler.setup`：`active_clients += 1`
- `handle` 结束 `finally`：`active_clients -= 1`，归零时 `last_disconnect = now`
- 暴露 `get_idle_state() -> (active_count, seconds_since_last_disconnect)`

**开关热加载**：`autoupgrade off` 写 `config`；后台线程每轮读 `config`，`off` 时跳过检查。`serve` 启动默认 `on`。

**安全**：

- 下载走 HTTPS；Release 同时发布 `socket_server.sha256`（或同目录 checksum 文件），下载后校验，失败删除 `.staging` 且不切换。
- 升级切换后由 `supervisor` 执行 `systemctl restart`；restart 失败记日志（不回滚）。

**手动 `upgrade`**：复用同一套下载逻辑，但跳过"30 分钟空闲"判定——仅检查"当前无活跃客户端"，有则拒绝并提示等待客户端断开。

## 五、多模块拆分映射

逻辑尽量原样搬迁、不做行为改动（除版本号统一、删死代码）。

| 现有代码 | 目标模块 |
|---|---|
| logger 配置（37-53） | `__init__.py` / `server.py`，改 `getLogger(__name__)` |
| `MyTCPHandler`（91-197） | `protocol.py`（收包/文件协议）+ `server.py`（连接计数钩子） |
| `do()`（759-1060） | `handlers.py` |
| `RequestChecker`/`PyppeteerChecker`/`BoceChecker`/`BrowserManager`（200-576） | `boce.py` |
| `extract_pcap_flow_five_tuples`（589-756） | `pcap_flow.py` |
| `PcapReplayer`/`FlowInfo`/`PcapStats`/`PacketBuffer`（1513-2400） | `replayer.py` |
| `SingleQueueRxThread`（2401-2703） | `capture.py` |
| `tcpdump_start/stop/isrun`/`Tcpdump_scapy`（1101-1308） | `capture.py` |
| `SocketServerListen`（1358-1508） | `socket_listen.py` |
| `exec_cmd_subprocess`/`routeinfo`/`mtu`/`isfile`/`isdir`/`mkdir`/`wait_until`/`ensure_command`/`detect_ip_version`/`compress_gzip`/`decompress_gzip`/`unzip`/`python_cmd` | `netutils.py` |
| 防火墙关闭（74-88）、java/chromium env（58-72） | `__init__.py` 启动钩子，仅 `serve` 时执行 |
| `__main__`（2741-2788） | `cli.py` + `__main__.py` |

**monitor 合并去向**：

- "选最新版本启动" → 删（`versions/current` + systemd 替代）
- "崩溃重启" → 删（systemd `Restart=always`）
- "init.d/rc3.d 自启" → 删（`enable` 装 systemd unit）
- NUMA/CPU 亲和性死代码（106-136） → 删
- `config` 文件解析 → 保留并扩展（加 `autoupgrade` 开关）

**打包改动**：`pyinstaller` 入口从 `socket_server.py` 改为 `socket_server/__main__.py`，产物名 `socket_server`；`打包命令` 脚本更新，不再单独打 `socket_server_monitor`。

## 验证

端到端验证（在测试靶机上）：

1. **打包**：`pyinstaller packaging/socket_server.spec --onefile`，产物为单文件 `socket_server`，`./socket_server current` 输出 `1.3.0`。
2. **enable/自启**：`./socket_server enable` → `systemctl status socket_server` 显示 active，`systemctl restart socket_server` 后崩溃能被 `Restart=always` 拉起。
3. **serve**：`./socket_server serve -p 9000`，客户端按现有协议连接发送 datatype 0/4/200 等指令，行为与旧版一致。
4. **list/switch/current**：`./socket_server list` 列出 GitHub Releases；`./socket_server switch 1.2.1` 改符号链接并重启；`./socket_server current` 显示切换后版本。
5. **手动 upgrade**：GitHub 发新 Release 后 `./socket_server upgrade` 下载、校验、切换、重启；有客户端连接时拒绝并提示。
6. **自动升级**：`config` 设 `autoupgrade=on`，无客户端连接 30 分钟后自动切换到新版并重启；有客户端连接时不触发；`autoupgrade off` 后不触发。
7. **回归**：发包（datatype 0）、抓包（5/6/121/122/123）、拨测（131）、socket 监听（171-174）、五元组提取（200）等指令行为与旧版一致。

## 范围外

- 不实现自动回滚（按需求）。
- 不支持私有仓库 token（当前为公开仓库；后续可扩展）。
- 不改写各业务指令的内部行为，仅做模块搬迁与版本号统一。
