# Changelog

本文件记录 socket_server 各版本的变更。版本号见 [`socket_server/version.py`](socket_server/version.py)。

## [1.5.3] — 2026-07-22

**兼容 DPI 版本：v1.0.7.0**

### 还原内核剥离的 VLAN 标签 + 启动期自动调大缓冲上限 + down 网卡诊断（capture.py / __init__.py）

#### 现象
v1.5.2 修好抓包丢包后，发现抓出的 pcap **没有 VLAN 层**——线上明明带 802.1Q 的帧，落盘后 VLAN 头消失。抓包要求"原始报文、不丢数据信息"，VLAN 层丢失即数据丢失。

#### 根因（读内核 + libpcap 源码确认，非猜测）
- **内核在把帧交给 AF_PACKET tap 之前就剥掉了 802.1Q 标签**，剥进 skb 元数据 `vlan_tci`/`vlan_proto`：
  - 入向 `__netif_receive_skb_core`（`net/core/dev.c:6032`）`skb_vlan_untag` 先剥，**之后**才 `ptype_all` tap；
  - 出向 `dev_queue_xmit_nit`（`:3884`）先 clone 给 tap，**之后** `validate_xmit_vlan`（`:3923`）才把 VLAN 推进帧。
- **内核把剥掉的 VLAN 放进了 `PACKET_AUXDATA` 辅助消息**（`net/packet/af_packet.c:3527-3566` 填 `tpacket_auxdata` + `put_cmsg`），但**前提是 socket 设了 `PACKET_AUXDATA` 选项**。旧代码没设 → cmsg 无 VLAN 信息 → 原样写"被剥过的帧" → pcap 丢 VLAN。
- tcpdump/wireshark 的做法：`setsockopt(PACKET_AUXDATA,1)`（`pcap-linux.c:2732`），收包时从 cmsg 取 `tp_vlan_tci`/`tp_vlan_tpid`，在 offset 12 插回 4 字节 802.1Q 头（`pcap-linux.c:4302-4327`）。本版做完全一样的事。

#### 修复
- `_open` 设 `PACKET_AUXDATA=8` 选项，让内核在 ancdata 附带 `tpacket_auxdata`。失败（老内核 ENOPROTOOPT）只 warn，退化为不还原（现状）。
- 新增 `_restore_vlan(data, ancdata)`：从 cmsg 取 `tpacket_auxdata`（`=IIIHHHH` 本机序解包），按 libpcap 的 `VLAN_VALID`/`VLAN_TPID` 宏判断帧是否带 VLAN；带则在 offset 12 插入 4 字节 `TPID(网络序)+TCI(网络序)`，还原在线原始帧；非 VLAN 帧原样返回一字节不加。`_recv_loop` 收包后先 `_restore_vlan` 再落盘。
- 兼容性：cmsg 缺失/长度不足（ABI 不一致老内核）/帧不足 12B → 一律原样返回，不崩。
- 下游消费者无需改：`pcap_flow.py:62-70` 与 `replayer.py:184-188` 本就显式检测 `0x8100` 并跳 4 字节取内层 ethertype，修复前它们从没收到 VLAN 帧，现在正常跳过 → 流分类结果不变，只是 VLAN 层重新可见。

### 启动期自动调大 rmem_max/wmem_max（__init__.py）
- v1.5.2 加了 SO_RCVBUF 被截断的告警，但只告警不修。本版在 `setup_environment` 加 `_ensure_sock_buf_limits()`：serve 启动时读 `net.core.rmem_max`/`wmem_max`，<8MB 则写 `/proc/sys` 调到 8MB，幂等、>=8MB 不动、失败只 warn 不阻断 serve。
- serve 以 root 跑、随 systemd 每次开机重设 → 等价持久，自包含、不碰 `/etc`。

### down 网卡诊断（capture.py）
- 10.12.131.35 "启动抓包失败"根因已确认是网卡 down（`enp94s0f0np0` 无 link）：AF_PACKET `bind` 到 down 网卡不报错，但 `recvmsg` 立即抛 `OSError(ENETDOWN)`，被 `_recv_loop` 的 `except OSError: break` 静默吞 → 线程 0.5s 内退出 → 报含糊的"线程未存活"，真实原因没传达。
- 修复：`except OSError` 分支对 errno 100(ENETDOWN)/19(ENODEV) 打明确 error "网卡不可用，请检查是否 up/插线" 并设 `_exc`，不再含糊。仅加诊断，不改启动判定（down 网卡仍返回 False，正确）。

---

## [1.5.2] — 2026-07-22

**兼容 DPI 版本：v1.0.7.0**

### 紧急修复：抓包丢失 + "内核丢弃 0 包"假信号（capture.py）

#### 现象

- 同一次 boce（1000 并发连接），拨测机抓包 1043 / 靶站抓包 6991，拨测机丢 ~85% 包（出向 SYN 172/1000、入向 SYN-ACK 207/1000）。
- 但日志始终报"内核丢弃 0 包"，让人误以为不是丢包问题。

#### 根因（三层叠加）

1. **`stop()` 顺序 bug → 统计永远为 0**（`AFPacketCapture.stop` / `_read_stats`）
   原顺序：`shutdown → close socket → join thread → 线程 finally 调 _read_stats`。线程 finally 在已关闭 fd 上 `getsockopt(PACKET_STATISTICS)` → `OSError(EBADF)` → 被 `except (OSError, struct.error): pass` 静默吞掉 → `_dropped` 永远保持初始值 0。**所有"内核丢弃 0 包"日志都是假信号**。

2. **`SO_RCVBUF` 被 `net.core.rmem_max` 静默截断**（`_open`）
   内核对 `SO_RCVBUF` 的规则：请求值超过 `rmem_max` 时静默截断，不报错。原代码 `try: setsockopt; except: pass` 完全没察觉。拨测机实测 `rmem_max=212992`，请求 4MB 实际生效仅 416KB（≈ 600 个 ~700B 包）。boce 前 0.2s 突发 ~3000 pps，瞬间填满缓冲 → 内核丢包。

3. **Python 单线程 recv + GIL 竞争**（`_recv_loop`）
   单线程 `recvmsg → struct.unpack → struct.pack → f.write` 全串行。boce 同时跑 10 个 `ThreadPoolExecutor` worker 线程（`boce.py:53`），全部跟 recv 线程抢 GIL，recv 线程跟不上峰值 pps。靶站没事是因为靶站 socket_server 进程没有 10 个并发 outbound worker 线程，GIL 不被抢。

#### 修复

- **第 1 层（本次发版）**：
  - `stop()` 改成 `join → _read_stats → close`，统计能在 socket 还活着时读到真实 `tpacket_stats.tp_drops`。
  - `_recv_loop` 移除 `finally: _read_stats()`，避免在已关闭 fd 上调用。
  - `_open` 中 setsockopt `SO_RCVBUF=4MB` 后立即 `getsockopt` 验证实际值；若小于请求值 ×2（Linux 把 val 翻倍用于协议开销），记 warning 提示 `sysctl -w net.core.rmem_max=8388608`。
- **第 3 层（v1.6.0 再做）**：改用 `PACKET_RX_RING` mmap 接收，绕过 recvmsg + GIL 竞争，从根本上解决 Python 单线程跟不上突发 pps 的问题。

---

## [1.5.1] — 2026-07-21

**兼容 DPI 版本：v1.0.7.0**

### 紧急修复：datatype 131 首次拨测 UnboundLocalError（handlers.py）

- **现象**：拨测（datatype 131）首次调用即崩，日志报 `local variable 'boce' referenced before assignment`。
- **根因**：`do()` 的 `global` 声明（`global ss, cache_sendpkts`）漏了 `boce`。而 131 路径对 `boce` 既有读（`boce.boce(**data) if boce else None`）又有赋值（`boce = BoceChecker()`）。Python 规则：函数体内有赋值的变量被视为 local，于是 244 行的读 `boce` 被判定为 local——首次调用时还没赋值 → `UnboundLocalError`，模块级 `boce = None` 被局部声明遮蔽读不到。
- **历史**：`ef72626`（2026-06-24）写 131 逻辑时 `global` 就没含 `boce`，潜伏至今；`4cc423c`（2026-07-08）清理 Tcpdump_scapy 改 `global` 时也未补回。与 1.5.0 抓包重写无关。
- **修复**：`global ss, cache_sendpkts, boce` 补回 `boce`。已验证首次调用正常初始化且不再崩。

---

## [1.5.0] — 2026-07-21

**兼容 DPI 版本：v1.0.7.0**

### 抓包重写：进程内 AF_PACKET + 时间戳稳定重排，根治乱序（capture.py / __init__.py / packaging）

- **背景**：原 `tcpdump_start`/`tcpdump_stop` 起 tcpdump/dumpcap 子进程，靠 `SingleQueueRxThread` 改网卡单队列 + 绑 IRQ + 关 RPS/RFS 保序。实测仍乱序，且根因在内核多队列/RSS 跨 CPU 分发 AF_PACKET 写入顺序非确定——压单队列方案脆（`ethtool -L` 驱动不支持静默失败、mlx5 中断命名 grep 不到）、改网卡全局状态且 `tcpdump_stop` 不恢复（拖累同机业务、状态残留）。
- **新实现 `AFPacketCapture`**：进程内 `AF_PACKET` socket + 单线程 `recvmsg` + 内核纳秒时间戳（`SO_TIMESTAMPNS`），直写微秒 pcap。停止 = 关 socket + join 线程 + 关文件，无信号/无 PID/无轮询。每路抓包独立实例按 path 注册到 `_captures`，天然支持同机多 path 并发。
- **保序核心**：抓完调 `_sort_pcap_by_timestamp` 按记录头时间戳稳定重排（不赌内核写入顺序，只信内核打的时间戳）。纳秒精度内部排序，落盘截断为微秒（兼容 `replayer.py`/`pcap_flow.py` 的 `=IIII` 解析）。
- **零网卡侵入**：`single_queue` 默认 `False`（原 `True`），默认不调 `ethtool`/不动 IRQ/不关 RPS-RFS；`SingleQueueRxThread` 保留为 opt-in。
- **不依赖外部二进制**：不再需要靶机安装 tcpdump/dumpcap；`__init__.init_capture` 不再检测工具，`_sniff_command` 仅用于日志。
- **BPF 过滤保留**：`extended` 非空时用 scapy `attach_filter`（libpcap ctypes 编译，无 shell），失败抛错不静默抓全部流量。
- **spec**：hiddenimports 加 `scapy.arch.linux`（`attach_filter` 所在，PyInstaller 静态分析可能漏抓）。
- **接口兼容**：`tcpdump_start(eth,path,extended,single_queue)`/`tcpdump_stop(path)`/`tcpdump_isrun(path)` 签名不变，datatype 5/6 透明。
- **真实工具可读**：生成的 pcap magic 为标准 `0xA1B2C3D4`，`tcpdump -r`/`tshark`/`wireshark` 正常读取（旧内部消费者 replayer/pcap_flow 跳过 magic 校验，故未暴露）。

### code review 修复（8 项，capture.py / __init__.py）

- **pcap magic typo**：`PCAP_MAGIC_US_LE` 由 `0xA1B2C3D8` 改为标准 `0xA1B2C3D4`（末位 D4 非 D8，LE/BE 不自洽证明手误）。
- **`_read_stats` 结构体错**：`tpacket_stats` 是 2 个 u32（8B），原用 `III`（12B）致 `struct.error` 未被 `except OSError` 捕获、recv 线程每次 stop 在 finally 崩溃、`_dropped` 恒 0。改 `II` + `except (OSError, struct.error)`。
- **`tcpdump_stop` 孤立泄漏**：原先 pop 再 stop，停止超时则实例移出注册表但线程仍存活，重试幂等返回 True 永不停止。改为失败保留实例可重试、成功才 pop。
- **`stop()` 写入竞态**：原 join 超时后仍 close 文件，与仍存活 recv 线程的 `write` 竞态。改为超时不关文件返回 False、线程退出后才 flush/close。
- **无默认路由 TypeError**：`routeinfo()['0.0.0.0']['Iface']` 在无默认路由时 `None['Iface']` 抛 TypeError。改 `.get() or {}` 链式取值，空则记 ERROR 返回 False。
- **BPF 失败静默抓全部**：`attach_filter` 失败原 `except Exception` 吞掉继续抓全部流量，调用方要过滤却抓到非预期流量。改抛 RuntimeError 让 `tcpdump_start` 返回 False。
- **`stop()` 半初始化崩溃**：`_open` 中途失败（bind/网卡不存在）时 `_sock` 仍 None，失败路径调 `stop()` 抛 `AttributeError` 盖掉真实启动错误。加 `if self._sock is not None` 防御。
- **文档/注释**：`init_capture` docstring 与 `setup_environment` 读取 `_sniff_command` 一致化；恢复 `Tcpdump_scapy` 弃用头块（`handlers.py:151` 交叉引用重新有指向）。

### 历史 review 结论

- 无历史回归：v1.3.1 的 capture 安全修复（停止幂等、flush 保证、`self.e=False` 竞态、shell 注入、`_sniff_command` RuntimeError）被新设计结构性消除——无子进程/无 PID/无信号/无 shell。

---

## [1.4.0] — 2026-07-09

**兼容 DPI 版本：v1.0.7.0**

### release notes 打包内嵌，版本详情查询不再联网（handlers.py / packaging）

- **背景**：`version_detail`（datatype 19）每次查询都调 GitHub API（`get_latest()`），占未认证 60 次/小时限额，且 `handlers.py` 未 import `REPO` 导致线上崩溃。
- **修复 import**：`handlers.py` 顶部补 `from .version import VERSION, REPO`。
- **内嵌 notes**：新增 `packaging/generate_release_notes.py`，打包前从 `CHANGELOG.md` 提取当前版本 notes，生成 `socket_server/release_notes.py`（`.gitignore` 不入库）。`version_detail(19)` 直接读本地模块，**完全不联网**，零限额消耗。
- **notes 与版本绑定**：release notes 随二进制一起升级，天然同步，不会出现"版本 1.4.0 但 notes 是 1.3.9 的"。
- 更新 `socket_server.spec`，打包前自动调用生成脚本 + 加 `release_notes` 到 hiddenimports。
- datatype 19 不再 import `upgrader.get_latest`，断开与 GitHub API 的依赖。

---

## [1.3.9] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 紧急修复：ETag 缓存变量未 global 导致 list/upgrade 崩溃

- **根因**：`get_releases()` / `get_latest()` 引用模块级 ETag 缓存变量 `_releases_etag` / `_releases_cache` / `_latest_etag` 时未声明 `global`，Python 将其视为局部变量。首次调用无异常（变量被赋值而非读取），第二次及并发调用时报 `local variable referenced before assignment`。
- **影响**：v1.3.7 所有依赖 `get_releases()`/`get_latest()` 的功能（`socket_server list`、`socket_server upgrade`、自动升级下载阶段）均崩溃。
- **修复**：`get_releases()` 和 `get_latest()` 内添加对应 `global` 声明。

---

## [1.3.8] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 请求日志增加代理标识 + raw 失败重试（upgrader.py）

- **新增 `_proxy_tag()`**：所有请求日志末尾统一显示 `(via proxy http://...)` 或 `(直连)`，一眼判断是否走代理。支持 config 和 env 两种代理来源。
- **raw 重试**：`get_latest_version_raw()` 失败后最多重试 2 次（间隔 2s/4s）。raw 不占 API 限额，重试无成本，扛偶发抖动。
- **修复 `_latest_cache` 未赋值**：`get_latest()` 304 分支返回的 `_latest_cache` 在首次成功时未保存，导致缓存始终为 None。
- 所有请求日志（get_releases / get_latest / _download_file / _verify_sha256 / show_current）统一使用 `_proxy_tag()` 标识代理状态。

---

## [1.3.7] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 解决 GitHub 未认证 API 60 次/小时共享限额（upgrader.py / autoupgrade.py）

内网多靶机共享同一出口 IP，GitHub 未认证 API 限额 60 次/小时易耗尽（403）。本次用两个不依赖 token 的方案彻底规避：

- **方案 1 — raw 文件查版本**：新增 `get_latest_version_raw()`，自动升级检查时从 `raw.githubusercontent.com/.../version.py` 读版本号（走 CDN，**不占 API 限额**）。仅在确认有新版需下载时才调 API 拿 asset 信息。
- **方案 2 — ETag 条件请求**：`get_latest()` / `get_releases()` 缓存响应的 `ETag`，后续请求带 `If-None-Match`，未变化返回 **304（不计入限额）**。
- 自动升级流程：raw 查版本 → 比对 → 有新版才调 `get_latest()` 拿 asset → 下载。日常检查零限额消耗。

---

## [1.3.6] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 升级代理与重试策略调整（upgrader.py）

- **实时读取代理**：所有 GitHub 请求（list / upgrade / 自动升级 / 下载）每次都实时读 `/opt/socket/config` 的 `proxy=`，不缓存。proxy 有值即走代理，无值则直连。
- **失败不重试**：`_download_file` 去掉原来的"直连失败后用代理重试一次"逻辑。请求失败直接抛出，由调用方等下个周期再试，避免短时间内重复消耗 GitHub API 限额。
- **首次启动即检查**：autoupgrade 线程启动后立即触发一次版本检查（无前置等待），后续周期保持 1 小时。
- **检查周期**：`CHECK_INTERVAL` 保持 3600 秒（1 小时）。

### 背景

v1.3.4/1.3.5 在内网多靶机环境下暴露两个问题：(1) 服务启动时若 config 尚未写入 `proxy=`，首次检查直连失败；(2) 所有靶机共享同一出口 IP，GitHub 未认证 API 60 次/小时限额被耗尽（403）。本次调整重试策略避免雪上加霜；根因的限流问题建议后续给 GitHub API 请求加 token 认证（5000 次/小时）。

---

## [1.3.5] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### chromium 按需自动下载（boce.py / handlers.py）

- **背景**：拨测（datatype 131）依赖 chromium，此前要求人工预先部署到 `/opt/socket/chrome-linux/chrome`，缺失时直接报错。
- **新增**：`ensure_chromium()` —— 拨测触发时若 chromium 不存在则自动下载，用到才下，不阻塞服务启动。
- **下载源**：npmmirror 镜像（`registry.npmmirror.com/-/binary/chromium-browser-snapshots/Linux_x64`），动态查询最新 revision，下载 `chrome-linux.zip` 解压到 `/opt/socket/chrome-linux/`。
- **代理支持**：复用 `/opt/socket/config` 的 `proxy=` 配置，内网环境下载走代理。
- **失败兜底**：下载失败时在日志和标准输出打印手动下载方法（含完整 wget/解压/chmod 步骤）。
- **线程安全**：用 `_browser_lock` 防止并发拨测重复下载。
- 缺依赖库的自动安装逻辑（yum/apt-get）保持不变，在 chromium 存在但缺 so 时触发。

---

## [1.3.4] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 升级下载代理支持（upgrader.py）

- **背景**：systemd 启动的服务不继承 shell 的 `http_proxy`/`https_proxy` 环境变量，内网代理环境下自动升级下载 GitHub Release 会直连失败。
- **新增**：`/opt/socket/config` 支持 `proxy=http://host:port` 字段。所有 GitHub API 请求与二进制下载显式带上该代理。
- **失败重试**：`_download_file` 直连失败后，若 config 配置了 `proxy=`，自动用代理重试一次。
- **优先级**：config `proxy=` 优先；未配置时回退环境变量（适用于手动 `socket_server upgrade` 等非 systemd 场景）。
- 更新 `docs/build-and-deploy.md` 说明 `proxy` 配置项。

---

## [1.3.3] — 2026-07-07

**兼容 DPI 版本：v1.0.7.0**

### 修复自动升级切换失效（supervisor.py / cli.py）

- **根因**：`_find_current_binary()` 用 `os.path.realpath()` 把 `versions/current` 符号链接解析成具体版本目录（如 `/opt/socket/versions/1.3.0`），写入 systemd unit 的 `ExecStart` 成为硬编码版本路径。`switch_to()` 切换符号链接后 `systemctl restart` 仍按 unit 里写死的旧路径启动，导致自动升级"切换成功、重启后版本不变"的死循环。
- **修复 1**：`_find_current_binary()` 不再 `realpath` 解析，直接返回 `versions/current/socket_server`，使 unit 的 `ExecStart` 指向稳定的 `current` 链接，切换链接后重启即生效。
- **修复 2（自愈）**：新增 `ensure_unit_correct()`，在 `cmd_serve` 启动时检查 unit 文件的 `ExecStart` 是否指向 `current` 链接，若不是（历史版本写死的路径）则自动重写 + `daemon-reload`。已部署靶机升级到本版本后，下次重启即自愈，无需手动 `enable`。
- 重构：抽取 `_write_unit()` 供 `service_enable` 与 `ensure_unit_correct` 复用。

---

## [1.3.2] — 2026-07-06

**兼容 DPI 版本：v1.0.7.0**

### 子进程环境清理增强（netutils.py）

- `_clean_subprocess_env`：在原有 `LD_LIBRARY_PATH` + `_PYI_*` 清理基础上，新增"任何值以 `sys._MEIPASS` 开头的变量一律清除"的规则，覆盖 `PLAYWRIGHT_BROWSERS_PATH` 及未来同源变量，无需逐个枚举。
- 修复 `PLAYWRIGHT_BROWSERS_PATH=/tmp/_MEIxxxx/ms-playwright` 泄漏到长驻子进程（如 `dpi_monitor`）的问题：`_MEI` 目录在 socket_server 退出即删，子进程持有的该路径会变成悬空引用。
- `PATH` 显式保留，即便其值引用了 `_MEIPASS` 也不动，避免子进程找不到命令。
- 进程内代码（如 playwright）仍可直接读 `os.environ`，不受影响：本函数只改传给子进程的环境副本。

---

## [1.3.1] — 2026-07-06

**兼容 DPI 版本：v1.0.7.0**

### Bug 修复（抓包停止/启动，capture.py）
- **tcpdump_stop**：幂等 + 信号升级 SIGINT→SIGTERM→SIGKILL + 精确 PID kill（`os.kill`）。原 `pkill -f` 把客户端传入的 `path` 当扩展正则，存在注入风险且 SIGKILL 会误杀无关同名进程。
- **tcpdump_isrun**：改用 `pgrep -x` 精确匹配进程名 + `/proc/{pid}/cmdline` 字面子串校验 path，返回 `(running, pids)` 元组，杜绝误杀同名 tcpdump/dumpcap。
- **Tcpdump_scapy._sniff**：删除 `_sniff` 开头 `self.e=False` 的竞态覆盖（冷导入期间 `stop()` 设的 True 被覆盖）；分段 sniff 循环（每轮≤1s），解决无包到达时 `stop_filter` 不触发导致 stop 卡死 30s。
- **flush 校验**：改用 `_wait_file_stable` 轮询文件大小稳定（连续两次相同）。原 `getsize/os.access` 对 root 恒真，是空操作。

### sudo 兼容（netutils.py）
- `_strip_sudo_if_root`：root 下剥离裸 `sudo ` 前缀，规避目标机 sudo 损坏（libldap/OpenSSL ABI 不匹配导致 `sudoers.so` 加载失败）。

### 子进程环境清理（netutils.py）
- `_clean_subprocess_env`：清除 PyInstaller 注入的 `LD_LIBRARY_PATH` 和 `_PYI_*`，避免 DPI 的 xsa 从临时目录加载错版本 libcrypto 启动即崩、被 dpi_monitor 反复重启。

### Shell 注入修复（客户端可控参数全部改 list args + shell=False）
- `tcpdump_start`：path/extended/eth
- `mtu`：eth（同时去掉 grep/awk 管道，改 Python 解析）
- `unzip`：filename/passwd/outdir
- `handlers datatype 131`：chromium_path

### 其他
- `wait_until`/`wait_not_until` 异常分支补 `sleep`，避免 CPU 空转
- `handlers datatype 122` 处理 `stop()` 返回值，停止失败时返回错误 JSON
- 新增 `test_e2e.py` 用于部署后回归测试

---

## [1.3.0] — 前序版本

- 重构：拆分单体 `socket_server.py` 为多模块包 + 版本管理 CLI
- 新增 datatype 19（结构化版本查询）
- 修复 20 个 code review 发现的 bug + e2e 流程模拟
- 修复 PyInstaller spec 路径解析 + 新增 entry.py 用于 exe 构建
- 修复 `MyTCPHandler` 必须继承 `BaseRequestHandler`、`TrackedTCPHandler` 作为 mixin 修复 MRO、`start_tcp_server` 必须使用传入的 handler_class
- 文档：README 安装/升级指南、TCP 接口指南、Linux 构建部署指南
