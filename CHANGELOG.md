# Changelog

本文件记录 socket_server 各版本的变更。版本号见 [`socket_server/version.py`](socket_server/version.py)。

## [1.8.3] - 2026-10-28

兼容 MCP 版本：mcp_socket_server 0.3.2（无损）

### 变更：日志路径支持环境变量覆盖（非 root 开发场景）

- `setup_logging`/`setup_child_logging` 的日志路径优先级改为：
  **显式参数 > 环境变量 `SOCKET_SERVER_LOG` > `/var/log/socket_server.log`（默认）**。
- 背景：开发环境迁移到非 root 用户（35/weihang，rootless podman 构建）后，
  跑 `serve`/`current` 在日志初始化处 Permission denied。开发实例用法：
  `SOCKET_SERVER_LOG=~/socket_server_dev.log ./dist/socket_server serve -p 9500`
- 生产零影响：systemd 部署不设该变量，日志路径与行为和 1.8.2 完全一致。

## [1.8.2] - 2026-10-27

兼容 MCP 版本：mcp_socket_server 0.3.2（file_transfer 指引工具）；旧 MCP 无损可用

### 变更：:9001 文件服务全程流式（支持 GB 级大文件）

- **上传流式化**：POST /files multipart 解析重写为边收边写——path 字段先到
  （curl -F 文档用法）则直写目标 `<path>.part` 临时文件，收完 `os.replace`
  原子顶替；file 先到则落系统临时文件再搬运。内存占用恒定（实测 ~18MB RSS），
  与文件大小无关。
- **下载流式化**：GET /files?path= 改 1MB 分块边读盘边发，Content-Length 锁定
  发送上限；客户端中断按 info 记一行，不再抛 traceback。
- **大文件实测**：2GB 上传 4.6s / 下载 3.5s，sha256 双向一致，服务进程 RSS
  峰值 18-20MB（旧实现峰值 ≈ 2×文件大小，GB 级必然 OOM 且拖死 :9000 主服务）。
- **修复存量缺陷（上传内容截短）**：文件内容中出现 `\r\n--<boundary>` 前缀
  但后随非 `--`/`\r\n`（如 `\r\n--ABC123x`）时，旧整载解析器会误判为分隔行，
  文件被截短丢字节（复现：307 字节只收 274）。新解析器按 RFC 2046 校验
  分隔行后随字符，内容不丢字节。**建议尽快升级**——旧版本上传 pcap 中恰含
  这种序列时文件已可能损坏。
- 兼容性：HTTP 接口（URL、参数、multipart 字段名、JSON 响应结构）完全不变，
  curl 命令与 MCP push_binary 零改动。
- **修复 install.sh 在线模式误判（发版日紧急修）**：版本守卫写了占位符字面量，
  发版 sed 全局替换后变成 `[ "$VERSION" != "1.8.2" ]`，latest 恰为烧入版本时
  误判"版本解析失败"直接退出。守卫改为按占位符特征（`*__*`）判断，字面量
  不再出现在比较式中；Release 资产已原地替换（2026-10-28）。

## [1.8.1] - 2026-10-22

兼容 MCP 版本：mcp_socket_server 0.3.1（无损，老桥可照常用 1.8.1 靶机）

### 修复
- **重启等端口释放**：主服务 :9000 两个监听 socket（IPv4/IPv6）开启
  `allow_reuse_address`（SO_REUSEADDR），TIME_WAIT 残留不再阻塞新进程 bind。
  此前 restart 后须等 :9000 自然过期 60 秒以上（35 靶机实测超 90 秒，
  systemd 每 5 秒拉起一次、失败一次），现重启秒起。
  server.py 起服段另补 `daemon_threads=True`（与 :9001/http_file.py、
  socket_listen.py 既有写法对齐）。
- 兼容性：监听行为变更仅影响"旧进程退出→新进程 bind"窗口；协议、datatype、日志均不变。

## [1.8.0] - 2026-10-21

兼容 MCP 版本：mcp_socket_server 0.3.0+（upgrade_run）；1.8.0 之前接口与 1.7.x 完全一致

### 变更：升级机制重构（靶机去联网化）
- **删除靶机侧全部 GitHub 联网**：每小时版本轮询线程（autoupgrade）、raw/API 版本查询、
  16M 二进制下载全部移除。查询与下载由中央节点（mcp_socket_server）统一执行一次，
  靶机升级零外呼——不再有联网超时阻塞 TCP 处理，不再有连不上刷屏（3 warning + 1 error/小时）。
- **新增 datatype 20 升级落地**：请求 `{"version":"1.8.0","sha256":"<64hex>","force":false}`。
  前置：中央经 :9001 把二进制推到 `versions/<版本>/.staging/socket_server`。
  行为：同步校验 sha256 + 落盘（本地毫秒级），回 `{"ok":true,"version":"..."}` 或
  `{"error":"原因"}`（版本非法/sha 不符/autoupgrade=off/升级进行中/有其它客户端且非 force）；
  受理后后台重启生效。重启前打一行 `升级完成：v旧 → v新`，与新进程启动行形成闭环。
- `autoupgrade=` 配置键保留（缺省 on），语义改为"是否接受升级下发"（off 时 dt20 拒绝）；
  `socket_server autoupgrade on|off` 命令不变。
- CLI：`upgrade` / `list` 子命令删除；`switch` 改为仅切本地 versions/ 已有版本（缺失版本
  不再联网下载）；`current` 改纯本地输出（不再拉 GitHub release notes，消除最后一块联网阻塞）。
- 新增 `packaging/install.sh` 一键安装/覆盖安装：在线 `curl -fsSL .../releases/latest/download/install.sh | sudo bash`，
  或离线包目录内 `sudo ./install.sh`；重复执行即升级/降级/覆盖修复；`/opt/socket/config` 已存在绝不覆盖；
  curl 全程带超时（connect 10s / max 300s）+ sha256 校验。
- 已知限制：新版本起不来时无自动回滚（与 1.7.x 一致），兜底 = 中央再推旧版 / 本机
  `socket_server switch <旧版本>` / 重跑 install.sh。

### 兼容性
- dt5/6/131/171-175/200 等既有接口零改动；TCP 帧格式零改动；文件传输 21-24 零改动。
- 存量 config（autoupgrade=on）升级后行为不变（接受下发）。

## [1.7.4] - 2026-10-20

兼容 MCP 版本：mcp_socket_server 0.2.x（capture_stop/capture_start 兼容旧响应）

### 回退
- **接口行为整体回退到 v1.7.0**：dt5/dt6/131/171/172/173/174 的请求与响应格式恢复 v1.7.0 原样
  （dt6 恢复 {"res": bool}；dt5 失败恢复 {"res": false}；171 换端口恢复静默忽略；172/173 失败恢复裸 b"error"）。
  原因：现场程序按 v1.7.0 响应格式解析，新格式导致解析不兼容。
- 保留项：datatype 175（监听停止）保留；drain/时间戳墙钟/HTTP 文件服务等**内部行为**一并回退
  （dt5/dt6 恢复 v1.7.0 语义后，1.7.1 的停止链路修复随之移除）。

### 保留
- datatype 175 停止监听接口保留（v1.7.1 引入的唯一位号新增）。

## [1.7.3] - 2026-10-19

兼容 MCP 版本：mcp_socket_server 0.2.5+

### 修复
- N7 chrome 僵尸累积：新增子进程收割线程（waitpid -1 WNOHANG 轮询），浏览器类拨测失败后的 chrome <defunct> 自动回收，不再耗尽 pid 表。对所有子进程僵尸通用。

## [1.7.2] - 2026-10-19

兼容 MCP 版本：mcp_socket_server 0.2.3+

### 修复
- 拨测(datatype 131)异常不再断连：浏览器崩溃/依赖异常转为 {"error": ...} JSON 响应，调用方可读
- 浏览器损坏（Browser closed unexpectedly）纳入重建重试；重建仍失败强制关闭 browser 供下次重建
- 浏览器类拨测异常时自动清理残留 chrome 进程（缓解僵尸累积）
- 抓包(datatype 5)失败时响应携带失败原因（{"res":false,"error":"..."}，如路径不可写/无默认路由/子进程启动超时）

### 兼容
- dt5 失败响应多出 error 字段：旧桥 get("res", False) 判定不受影响

## [1.7.1] - 2026-10-17

兼容 MCP 版本：mcp_socket_server 0.2.1+

### 修复
- 抓包停止链路：高流量下 stop 卡 10s 失败（drain 无时限）→ 2s 上限；停止超时仍收尾文件（不再永久截断）；stop 返回四态 JSON（stopped/exitcode/pcap_complete/note/error），重复 stop 回放上一轮真实结果。**注意：1.7.1 的 capture_stop 响应需配 mcp_socket_server 0.2.1+，旧桥会误判失败**
- 抓包时间戳回退路径由开机秒数（落盘显示 1970 年）改为墙钟
- socket 监听新增 datatype 175（停止监听）；171 对"换端口"明确报错（不再静默忽略）
- 172/173 无监听时的错误从裸 b"error" 改为 JSON 文案
- HTTP 文件服务：Handler 连接空闲超时 60s；multipart 解析丢弃内容尾部 CRLF 的字节保真修复

### 兼容
- capture_stop 响应形态变更（见上）；datatype 175 为新增分支，旧客户端零影响

## [1.7.0] - 2026-10-16

兼容 DPI/MCP 版本：mcp_socket_server 0.2.0

### 新增
- HTTP 文件服务（`http_port=9001`，config 可配，0=关闭）：GET `/files?path=` 下载、POST `/files` multipart 上传，双栈监听，TCP 协议零改动。供 LLM 直连靶机上传/下载文件（MCP file_upload/download 工具由 0.2.0 起移除）。

### 依赖
- 零第三方新依赖（http.server 标准库；二进制增量 <100KB）

---

## [1.6.3] — 2026-08-12

**兼容 DPI 版本：v1.0.7.0**

### 抓包精确性修复：stop 时 drain 队列在途帧 + 缓冲 32MB 抗并发突发（capture.py / __init__.py）

#### 现象
35 上 pcapdump 用例偶发差 1-2 帧：预期包数 61613，实际 pcap 61612（少 2 帧，缺 seq=2302950491 段的 15.54/15.53 两个子流帧）。日志 `抓包完成 ...：61612 包，内核丢弃 0 包`——**内核统计 0 丢弃，但 pcap 少帧**。

#### 根因（两处）
1. **stop 时 socket 队列在途帧被丢弃**（主因）：`_recv_loop` 循环条件在 `recvmsg` 前检查 `_stopped`，`stop()` 置位后 recv 线程**不再 recvmsg**，队列里 stop 前已进缓冲、尚未读走的在途帧随 socket close 被内核丢弃。镜像口突发（回放结束瞬间的收尾帧）恰好在这批里 → pcap 少最后 1-2 帧。
2. **SO_RCVBUF 4MB 偏小**：镜像口环境有并发流量（llcj 批量回放）时，回放突发 + 并发流叠加可瞬时填满 4MB 缓冲 → 内核丢包（实测并发场景抓到 65850 帧，其中混入 llcj 流）。

#### 修复
1. `_recv_loop`：`_stopped` 置位后新增 drain 段——循环退出后继续 `recvmsg` 直到超时/队列空，把在途帧全部落盘再退出（超时兜底防 stop join 卡死）。
2. `SO_RCVBUF` 4MB → **32MB**，`_ensure_sock_buf_limits` rmem_max 8MB → **32MB**（否则 32MB 请求被 rmem_max 截断）。

#### 验证
35 上严格按真实用例参数（单口 enp94s0f0np0 + 回放 enp135s0f0np0 单接口 50Mbps）连续 3 次：2 次 61613 精确一致，1 次 65850（并发 llcj 回放混入，非抓包缺陷）。修复后应稳定 61613，且并发场景不再丢帧。

---

## [1.6.2] — 2026-08-12

**兼容 DPI 版本：v1.0.7.0**

### 修复带 BPF 抓包启动失败：spec 补 `fcntl`（packaging/socket_server.spec）

#### 现象
带 `extended`（BPF 过滤）的抓包启动失败，日志：
```
开启tcpdump抓包 参数：{'eth': 'enp6s0f1', 'extended': 'tcp[13] == 0x1c', ...}
网卡 enp6s0f1 时间戳精度模式: hw_ts
启动抓包失败：BPF 过滤编译失败(tcp[13] == 0x1c): No module named 'fcntl'
```
`.32` enp6s0f1 封堵用例抓包现场复现。

#### 根因
- `fcntl`（Python 标准库）**未被打进 PyInstaller onefile 二进制**。
- `scapy/arch/linux/__init__.py:11` 顶层 `import fcntl`；`_open` 在 `extended` 非空时才延迟 import `scapy.arch.linux`（attach_filter），frozen importer 找不到 `fcntl` → ModuleNotFoundError。
- 触发条件：**只有带 BPF 的抓包**（extended 非空）。不带 extended 的抓包不 import scapy，不炸。A/B 站 017 封堵用例的 `host x and port 8000` 也带 extended，同样受影响。
- 与 1.6.1 时间戳改动**无关**（时间戳 `hw_ts` 模式已正常初始化）；是 PyInstaller 静态分析漏收标准库模块，1.5.0 补 `scapy.arch.linux` 为同款先例。

#### 修复
`socket_server.spec` hiddenimports 补 `'fcntl'`，强制打进二进制。三行纯 spec 改动，零代码。

#### 验证
待 CentOS 7 builder 构建后：`.32` 带 `tcp[13]==0x1c` 抓包启动成功、日志无 ModuleNotFoundError、抓包正常落盘。

---

## [1.6.1] — 2026-07-24

**兼容 DPI 版本：v1.0.7.0**

### 时间戳精度自动降级：优先硬件亚纳秒，逐层兜底（capture.py）

#### 背景
v1.5.6/1.6.0 用 `SO_TIMESTAMPNS` 取内核软中断纳秒时间戳保序。软中断时刻是包进入 net_rx_action 的时刻，非线缆到达时刻，burst 下同纳秒的包仍可能时间戳相同。35 的 mlx5 物理网卡支持硬件时间戳（`ethtool -T` 显示 `hardware-receive` + PHC），可达亚纳秒（网卡 PHC 线缆到达时刻），是同纳秒包区分先后的天花板。virtio（A/B 站）不支持硬件时间戳。

#### 修复
`_open` 一次性探测 setsockopt，存 `self._ts_mode`，`_decode_ts_ns` 据模式解码 cmsg，自动降级：

- **L0 `hw_ts`**：`setsockopt(SO_TIMESTAMPING, RX_HARDWARE|RX_SOFTWARE|RAW_HARDWARE)`。网卡支持 HW 时 cmsg `ts[0]` 非零，取硬件时间戳（亚纳秒 PHC，线缆到达时刻）；驱动/内核不给 HW 则 `ts[0]==0`，自动退 `ts[1]`（软中断纳秒）。**HW 是否可用由 cmsg 内容判定，不依赖预探测**（setsockopt 成功≠HW 真生效，需看 ts[0]）。
- **L1 `sw_ts`**：L0 setsockopt 失败（ENOPROTOOPT）退 `setsockopt(SO_TIMESTAMPING, RX_SOFTWARE)`，取 cmsg `ts[1]`（等同 SO_TIMESTAMPNS）。
- **L2 `ns_ts`**：L1 也失败退 `SO_TIMESTAMPNS`（老内核/驱动不支持 SO_TIMESTAMPING）。
- **L3 `userspace`**：L2 也失败退 `time.monotonic_ns()` 在 recvmsg 返回时取（微秒级，全失败兜底）。

各网卡实测预期：35 mlx5 -> `hw_ts`（PHC 亚纳秒）；A/B virtio -> `hw_ts` 模式但 ts[0] 始终零，自动取 ts[1] 纳秒（等价软中断）；老 CentOS 6 / 不支持驱动 -> `ns_ts` 或 `userspace`。日志每次抓包打 `时间戳精度模式: <mode>`。

#### 设计要点
- `SO_TIMESTAMPING` 是 `SO_TIMESTAMPNS` 的超集（同时支持 HW+SW），优先用之，cmsg 给 `scm_timestamping`（3 个 timespec 48B：HW/SW/unused）。原 `SO_TIMESTAMPNS` 路径作为 L2 兜底保留。
- HW 优先但 SW 共存：一次 setsockopt 同时请求 HW+SW，内核能给 HW 就填 ts[0]、给不了就填 ts[1]，`_decode_ts_ns` 据 ts[0] 是否非零自适应——**不需要"知道硬件是否支持"的预探测**，全靠 cmsg 内容。
- 保序不回归：时间戳精度提升不改变 `_sort_pcap_by_timestamp` 纳秒重排逻辑（HW/SW 都是纳秒级整数，落盘仍是 PCAP_NSEC_MAGIC 纳秒 pcap，排序 key `(ts_sec, ts_nsec, idx)` 不变）。
- 纳秒 pcap 落盘不变：HW 时间戳虽亚纳秒，落盘截断到纳秒（pcap 格式精度上限），但相比软中断时间戳已消除"net_rx_action 排队抖动"，同纳秒包大幅减少。

#### 验证
本地 lo 冒烟：ts_mode 探测正常、pcap 纳秒 magic `0xA1B23C4D`、时间戳单调。待 CentOS 7 builder 构建后：35 mlx5 跑抓包确认日志 `时间戳精度模式: hw_ts` 且 cmsg ts[0] 非零；A/B virtio 确认 ts[1] 纳秒。各模式包序与 v1.6.0 一致。

### review 修法（capture.py）

- **bug：`SOF_TIMESTAMPING_RX_HARDWARE` 位定义错**：原 `1<<4`（实为 `SOF_TIMESTAMPING_SOFTWARE` 全局软件位），应是 `1<<2`（核对 `/usr/include/linux/net_tstamp.h:20`）。位错致内核没收到 RX 硬件时间戳请求 → cmsg `ts[0]` 永远零 → `_decode_ts_ns` 永远退化软中断 → **35 mlx5 的 PHC 也用不上，1.6.1 等于白做**（静默失效，冒烟能过因 lo 本就无 HW）。修为 `1<<2`，组合 flags 从错位 88 修正为 76（0x4C）。
- **诊断：HW 未生效不静默退化**：`_decode_ts_ns` 在 `hw_ts` 模式下 cmsg `ts[0]==0`（驱动/内核未填 PHC，如 virtio/lo）退化软中断时，首次 warn 一次"硬件时间戳未生效，退化软中断纳秒"。让 35 mlx5 这类声称支持 HW 的网卡能一眼看出 HW 是否真用上（不静默退化），诊断闭环。正常路径不刷屏（仅首次）、不破坏保序。

---

## [1.6.0] — 2026-07-24

**兼容 DPI 版本：v1.0.7.0**

### 根治同机拨测+抓包丢包：抓包移入独立子进程（capture.py / __init__.py）

#### 现象
017-过滤-音频URI（入向）用例，A 站（拨测发起方 172.31.138.158，v1.5.6）抓到 7001 包、内核丢 887 包；B 站（被拨测方 10.12.131.183，v1.5.6）抓到 8795 包、内核丢 0 包。A 是发起方、少抓的不是没发——是真切丢了 887 个，`tp_drops` 白纸黑字。

#### 根因（v1.5.2 第 3 层，当时留到 v1.6.0）
- A 站是拨测**发起方**：`boce.boce()` 在 A 站 socket_server 进程内跑，10 个 `ThreadPoolExecutor` worker 线程与 `AFPacketCapture._recv_loop` 的 recv 线程**同进程抢 GIL**。
- recv 线程拿不到足够 GIL 去 `recvmsg` drain，AF_PACKET socket 缓冲（8MB）在突发峰值溢出 → 内核 `tp_drops=887`。
- B 站是被拨测方，进程内无 boce worker，GIL 不被抢 → recv 全速 → 0 丢包。
- v1.5.2 已判明：rmem/SO_RCVBUF 调大只能拖延溢出，GIL 竞争才是主因（1.5.3 调到 8MB 后 A 仍丢 887 即铁证）。
- v1.5.6 修的是**乱序**（纳秒 pcap + 单队列保序），未碰丢包这层。

#### 选型（systematic-debugging）
考虑过 TPACKET_V3 + PACKET_RX_RING 进程内 mmap：V3 块超时退休让唤醒/GIL 获取次数骤减。但 ABI 核实（`/usr/include/linux/if_packet.h`）暴露硬约束——snap=65535（不截断 jumbo，零数据丢失硬需求）下，frame_size=65535 使环仅容 ~1900 包，比现状 8MB 缓冲更易丢；snap=2048 截 jumbo 又违背"零信息丢失"。VLAN 还原在 V3 ring 路径不可靠（内核不设 VLAN_VALID 位，必须额外开 PACKET_AUXDATA）。**单进程内无法同时满足"全帧不截断 + VLAN 还原 + 丢包根治"**。最终选独立子进程：抓包进程独占 GIL，boce worker 抢不到它，recvmsg 在独立进程里就够用，且 snap=65535 全帧 + AUXDATA 还原 VLAN——三个硬需求全满足。

#### 修复
- **抓包移入独立子进程**：`tcpdump_start` 不再在主 serve 进程起 `AFPacketCapture` 线程，改为 `multiprocessing.Process` 起子进程加载 `_capture_process_main`。独立 GIL，boce 的 worker 线程抢不到抓包进程 → recv 全速 drain → 丢包根治。
- **启停协议（Pipe + Event）**：子进程 `_open` 成功经 `multiprocessing.Pipe` 回 `("ready",None)`，失败回 `("error",str(exc))`；主进程 `tcpdump_stop` 经 `multiprocessing.Event` 通知子进程收尾。替代旧主进程 `time.sleep(0.5)+查_exc` 盲等（主进程不再 sleep 轮询 _exc；子进程内仍保留一处 sleep(0.5) 作 recv 线程起跑+网卡 down 立即抛出的确认窗口，非盲等）。pcap 仍子进程直写 `path`、主进程 join 后下载——现契约不变，零额外 IPC。
- **`AFPacketCapture` 类内 recv/VLAN/纳秒落盘/stop 语义全部不变**：仅宿主从"主进程线程"换成"子进程内线程"。`_open`/`_recv_loop`/`_restore_vlan`/`_write_packet`/`_read_stats`/`stop` 均沿用 1.5.2~1.5.6 实现。
- **子进程内做单队列 + 排序**：`SingleQueueRxThread` 与 `_sort_pcap_by_timestamp` 移入 `_capture_process_main`（内聚：网卡状态改动与保序重排只影响抓包进程）。
- **`_captures` 注册表**：`path -> (proc, stop_evt)`，`tcpdump_stop` set Event → join 30s → 超时 terminate 兜底（极端异常路径，正常 settimeout(1.0)+stop_evt 必退）。`tcpdump_isrun` 查 `proc.is_alive()`。
- **PyInstaller onefile 子进程可用**：`socket_listen.py` 已用同款 `multiprocessing.Process(daemon=True)`（v1.5.5 验证），spec 无需改。
- **退出与丢包边界（daemon + SIGTERM）**：子进程 `daemon=True`。**仅 MCP 层显式调 datatype 6（tcpdump_stop）才走优雅收尾**（stop_evt.set -> cap.stop flush+close -> sort）。systemctl stop/restart 走 SIGTERM，serve 未注册 handler 不调 tcpdump_stop（v1.5.6 线程模型同样如此，非 v1.6.0 回归），daemon 子进程被 systemd cgroup 清理，可能丢最后一批未 flush 包 + 未排序。kill -9/OOM 同理。正常抓包流程（start->拨测->stop->下载）不受影响。

#### VLAN 全帧保留（硬需求）
- **snaplen 65535**：不截断任何帧（含 jumbo），满足"抓包必抓原始数据、零信息丢失"。
- **VLAN 还原保留**：开 `PACKET_AUXDATA`，内核把被剥离的 802.1Q 标签放 `tpacket_auxdata` cmsg，`_restore_vlan` 据 cmsg 在 offset 12 插回（对齐 libpcap pcap-linux.c:4302）。V3 ring header 的 `hv1` 字段内核不设 VLAN_VALID 位（ABI 核实），VLAN 必须走 AUXDATA cmsg——进程内 recvmsg 路径天然支持，1.5.3 成果完整保留。

#### rmem/SO_RCVBUF 保留（独立进程下的定位变化）
- v1.6.0 前曾计划删 `__init__._ensure_sock_buf_limits` + `SO_RCVBUF` 设置（基于 RX_RING 方案：ring 容量替代缓冲）。改独立进程后，缓冲层从"无用治标"变回"独立进程的抗抖动防御层"：GIL 主因已解，但 recv 线程 drain 间隙的瞬时积压仍靠 SO_RCVBUF 缓冲。**保留** rmem_max 8MB（`__init__` 启动期调）+ SO_RCVBUF 4MB（`_open` 设并校验）作为双保险，保"零数据丢失"。rmem 全局副作用 v1.5.3 已接受（serve root 随 systemd 重设）。

#### 验证
本地 lo 冒烟：子进程启停正常，pcap 纳秒 magic `0xA1B23C4D`、时间戳单调、1693 包 0 逆序。待 CentOS 7 builder 构建后复现 017 用例：A 站"内核丢弃"须为 0（现 887）、包数向 B 站 8795 靠拢；`tcpdump -r`/`pcap_flow.py` 流数、包序、VLAN 层、时间戳单调与 v1.5.6 一致。

---

## [1.5.6] — 2026-07-23

**兼容 DPI 版本：v1.0.7.0**

### 根治抓包乱序：纳秒 pcap + 单队列默认开（capture.py / replayer.py）

#### 现象
突发场景（瞬间连续发包）抓到的 pcap 仍有乱序，且乱序包之间的时间戳在纳秒级完全相同（间隔低于纳秒级，几乎同时发送）。

#### 根因
1.5.0 的保序机制是"内核纳秒时间戳 + 抓完按时间戳稳定重排"。但有两个洞：
- **落盘截断丢精度**：`_write_packet` 把内核纳秒时间戳截断为微秒落盘（`ts_usec = ts_ns // 1000`）。突发下多个包落在同一微秒 → 重排 key `(ts_sec, ts_usec, idx)` 时间戳部分相同 → 退化为文件写入序（idx）。
- **收包序 ≠ 线序**：多队列网卡（实测 .82 virtio 有 8 个 RX 队列）下，AF_PACKET 收包顺序由各 CPU softirq 排空队列的次序决定，不等于线序。单队列默认关（`single_queue=False`）时，文件写入序本就不是线序。

两者叠加：突发同微秒包 → 重排无法区分 → 退化为错误的收包序 → 乱序。提高时间戳分辨率（到纳秒）只在收包序=线序时有效，单队列才是根治。

#### 修复
- **`_write_packet` 改写纳秒时间戳**：pcap magic 从微秒 `0xA1B2C3D4` 改为纳秒 `0xA1B23C4D`（标准 libpcap PCAP_NSEC_MAGIC），记录头第二字段直接写 `ts_nsec`（不截断），让重排有纳秒精度区分同微秒内的包。
- **`_sort_pcap_by_timestamp` 支持纳秒 magic**：不再跳过纳秒格式，按 `(ts_sec, ts_nsec, idx)` 稳定重排；微秒/纳秒 magic 都支持，单位不影响排序单调性。
- **`single_queue` 默认 `True`**：`tcpdump_start` 抓包前缩到单队列 + 绑 CPU + 关 RPS/RFS，确保收包序=线序。配合纳秒排序双重保序。
- **replayer.py 兼容纳秒读取**：`replay_file` 读全局头 magic 判定分辨率，纳秒（`0xA1B23C4D/0x4D3CB2A1`）除以 1e9，微秒除以 1e6。否则纳秒文件 timestamp 会算错最多偏 ~1000s。
- pcap_flow.py 无需改（只读 incl_len，不依赖时间戳单位）。

#### 验证
.82 单流 20000 包突发：ns/μs 排序后逆序对均为 0；多流 4×5000：per-flow 0 逆序。纳秒 pcap 由 replayer 正确读取时间戳，回放节奏正确。

---

## [1.5.5] — 2026-07-22

**兼容 DPI 版本：v1.0.7.0**

### 修 socket 监听 30001 被单个常驻连接卡死：并发 accept（socket_listen.py）

#### 现象
10.12.131.82 上 datatype 171 启动 30001 监听后，IPv4 路径新连接全部超时、抓不到数据；IPv6 路径偶发能通。`ss` 显示 listen backlog（Recv-Q=6）已塞满完成三次握手但从未被 `accept()` 取走的连接（无 owner），其中一个 Recv-Q 高达 127KB——客户端数据早到了，服务端没读。

#### 根因
`_start_server` 是**迭代式服务器**：`accept()` 一个连接后，整个监听进程就陷在该连接的 `recv` 循环里，**直到该连接关闭**才能 accept 下一个。只要有一个客户端常驻连接（现场是 DPI 上报流 10.12.131.33）持续推数据，后续连接就只能排进 `listen(5)` 的 backlog，满后 SYN 被内核丢弃。IPv4/IPv6 是两个独立监听进程，哪个被占哪个瘫痪，所以表现为半瘫。

复现：本机向 `127.0.0.1:30001` 发测试包 → `timeout('timed out')`；向 `[::1]:30001` 发 → 秒通落盘。

#### 修复
- `_start_server` 改为**并发 accept**：`accept()` 出连接后立即交给 daemon 线程 `_handle_conn` 去 `recv`，主循环立刻回到 `accept()` 继续收下一个。
- 数据仍 put 进同一个 `multiprocessing.Queue`，下游 `cachedata` 维持**合并流**语义不变（datatype 174/173 返回内容结构不变）。
- `listen(5)` → `listen(128)`，给突发连接留缓冲。
- `_handle_conn`：`recv` 异常（ConnectionResetError/OSError）和对端正常关闭（`not data`）分支明确区分，去掉原"无效数据循环2/3"的含糊日志。

#### 验证
本地端到端：常驻连接占住期间，并发发起后续 IPv4/IPv6 连接均秒通落盘；单连接内多次 `sendall` 的分片正确按序拼回。

---

## [1.5.4] — 2026-07-22

**兼容 DPI 版本：v1.0.7.0**

### 修 BPF 过滤时机窗口：抓到非过滤条件包（capture.py）

#### 现象
10.12.131.32 抓 `tcp[13] == 0x1c`，pcap 里混入 25 个非 0x1c 包（17 UDP + 4 ACK + 2 FIN + 2 ARP）。BPF 附上后 2000 包全是 0x1c、零非 0x1c；25 个漏网包时间戳**全部**在 BPF 附上之前。

注：v1.5.3 已发版但漏了此修复（1.5.3 的 capture.py 仍是旧 `socket.htons(ETH_P_ALL)`），本版补上。

#### 根因（读内核 + pcap 实测确认）
- `_open()` 原顺序：`socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL))` + `bind()` -> setsockopts -> `attach_filter`。
- 内核 `packet_create`（`af_packet.c:3393`）`if (proto)` 才 `__register_prot_hook` 收包--**socket 一创建就开始收**（bind 前从所有网卡，bind 后从指定网卡）。BPF 在 bind 之后才附，必有"bind 后 BPF 前"窗口。
- 实测：`from scapy.arch.linux import attach_filter` 首次导入 scapy 耗 ~412ms，期间 socket 无过滤收包进缓冲，recv 线程原样落盘。
- 现场对照：`proto=ETH_P_ALL` 创建未 bind 即收到包（len=74）；`proto=0` 创建未 bind 收不到包（timeout）。

#### 修复
- `_open()` 改为 **proto=0 创建（`packet_create` 不注册 prot_hook，socket 不收包）-> setsockopt + BPF attach -> 最后 `bind(ETH_P_ALL)` 才注册收包**。BPF 在收包前就位，零未过滤窗口；scapy 那 412ms 导入发生在 socket 不收包期间，不再泄漏。
- proto=0 创建后 `po->num=0`，bind 必须显式给 `ETH_P_ALL`（`bind(0)` 会 fallback 到 `po->num=0` 不注册收包）。Python AF_PACKET bind 自动 htons 主机序 proto（已用 getsockname 验证）。

### review 修法（capture.py / __init__.py）
- `_recv_loop`：down 网卡 errno 魔法数字 `100/19` -> `errno.ENETDOWN/ENODEV`（自解释）。
- `tcpdump_start`：down 网卡时 `_exc` 检查提到 `is_alive` 前，避免含糊的"线程未存活"盖掉 `_recv_loop` 已打的"网卡不可用"。
- `_ensure_sock_buf_limits`：sysctl 写入加 `\n`（/proc/sys 数值写惯例）。

### 其他
- `.gitignore` 加 `.mcp.json`（本地 MCP 配置，不入库）。

---

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
