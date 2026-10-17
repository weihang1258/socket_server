# 监听停止 + 全工具 timeout + 测试报告缺陷修复 设计

- 日期：2026-10-16（timeout 补全 10-17；测试报告 B01-B23/S01-S11 并入 10-17）
- 状态：待评审
- 上游：`docs/superpowers/specs/2026-10-16-http-file-transfer-mcp-convergence-design.md` §2.3
- 缺陷来源：外部测试报告（2026-10-16~17，B01-B23 + S01-S11，34 项）
- 涉及仓库：socket_server（1.7.1）、mcp_socket_server（0.2.1）
- **用户决定：所有修改一批次完成，不分批**

## 0. 测试报告核实结论（先立事实）

34 项逐条核对源码 + 生产实测复现，**全部真实，0 误报**。两点定性修正：
- **S02 "type:null"**：实为 pydantic 对 `Optional[int]` 的标准输出（`anyOf:[integer,null]`），类型信息完整，非"无类型"。修复=按语义收紧（可选整型参数保留 nullable；force_sport/dport 这类应为纯 integer 不允许 null）。
- **B20/S10 capabilities 空壳**：FastMCP 框架默认声明 prompts/resources，非本项目代码声明未实现。

**用户裁定（原始逻辑保持）**：
- **B11/B12/B21 全部不修**。git 全史核对（1.2.1 基线 `91f6dd9` vs HEAD）：封堵（requests 200 判定）/重定向（跳转判定）/弹窗（"未实现判定失败"如实声明）/未知模式判定失败/默认 mode=封堵，五个逻辑点**逐字符一致**。"放通"参数值从最初落地起就不存在（走入"未知模式判定失败"是**原始设计行为**，非缺陷）；B12 chromium 检查顺序、B21 浏览器环境问题同属既有行为。工具描述补一句真话（弹窗未实现/放通非有效值），代码零改动。
- B14（replay downlink 统计）需 DPI 环境验证成因，本批次只把 stats 原样透传的口径在描述中注明"downlink 计数依赖双接口回放，单臂回放恒 0"，修复单独排期。

## 1. socket_server 侧（1.7.1）

### 1.1 监听停止：新增 datatype 175（B10/B08）

```
datatype 175  socket监听-停止监听
请求:  无 payload
响应:  成功 -> b"ok"（与 172/173 同型）
       失败 -> {"error": "..."} JSON
```

handlers.py（紧跟 174 之后，`else: pass` 之前）：

```python
elif datatype == 175:
    logger.info("socket监听-停止监听，类型：%s" % datatype)
    if ss is None:
        return json.dumps({"error": "未启动监听"}).encode("utf-8")
    ss.stop_server()
    stopped = not ss.pid()          # 成功判定以"端口确实没了"为准
    ss = None                       # 释放单例：下次 171 可换端口重建
    if stopped:
        logger.info("socket监听已停止")
        return b"ok"
    return json.dumps({"error": "停止监听失败，端口仍被占用"}).encode("utf-8")
```

关键决定（已核实）：
- 成功判定**不用** `stop_server()` 返回值（其返回 `True`/`None`，"无可杀"时是 `None`），以停后 `ss.pid()` 为空为准。
- `ss = None`（do() 顶部已有 `global ss`，赋值生效）解决换端口：下次 171 走 `not ss` 分支用新端口重建。**同时修 B08**：171 分支加端口一致性检查——`ss and ss.pid()` 且 `data.get("port") != ss.port` 时不再静默 pass，返回 `{"error": "已有监听运行在端口 X，请先停止(datatype 175)"}`。
- stop 后 172/173/174 自然落入既有 error 分支（已核实），无需改。
- `stop_server()` 内 `os.system(kill -9)` 是既有代码，不动（kill 的是自己查到的数字串，无注入面）。

### 1.2 抓包停止链路（B01+B03+B06 同根同修）

**根因（已核实 capture.py:236-253）**：`_recv_loop` 的 drain 段逐帧读到 socket.timeout 为止，**无总时限**——高流量下在途帧持续到达，"队列已空"永不成立，线程不退出 → `stop()` join(10s) 超时 → `return False`（不 flush 不 close）→ 子进程 `sys.exit(1)` → 文件截断未排序。B01（10.8s 失败）/B03（重试 true 掩盖 exitcode=1）/B06（590B 尾部截断）是同一死结的三个症状。

**修 1：drain 加绝对 deadline（capture.py _recv_loop drain 段）**

```python
if self._stopped.is_set() and not self._exc:
    try:
        self._sock.settimeout(1.0)
        drain_deadline = time.monotonic() + 2.0   # 置位后最多再 drain 2s
        drained_after = 0
        while True:
            if time.monotonic() > drain_deadline:
                logger.warning(f"drain 超时(2s)，放弃残余在途帧 {self.eth} -> {self.path}")
                break
            try:
                data, ancdata, _flags, _addr = self._sock.recvmsg(65535, 1024)
            except socket.timeout:
                break  # 队列已空
            except OSError:
                break
            if not data:
                break
            drained_after += 1
            ...  # 原有写帧逻辑
```

高流量下在途帧排空实测 <100ms，2s 上限只在极端下放弃残余（打 WARNING 带帧数）——线程 2s 内必退，join(10s) 不再超时，B01 的 10.8s 失败链断根。

**修 2：stop 超时仍收尾文件（capture.py stop()）**

```python
alive = self._thread is not None and self._thread.is_alive()
if alive:
    logger.error(f"停止抓包线程超时(>{timeout}s)：{self.eth} -> {self.path}")
    # B06 修复：线程未退出也必须收尾文件，否则 pcap 永久截断。
    # 风险（与仍在写盘的线程竞态）可接受：进程即将 sys.exit(1)，截断文件
    # + 明确失败信号 优于 永久无尾文件。
    if self._sock is not None:
        try:
            self._sock.close()
        except OSError:
            pass
    if self._f is not None:
        try:
            self._f.flush()
            self._f.close()
        except Exception:
            pass
    return False
```

（修 1 生效后此路径仅在极端场景触达；保留防御。）

**修 3：stop 结果留存可查（B03，capture.py tcpdump_stop）**

```python
_last_stop_status: dict[str, dict] = {}   # 模块级，{path: {...}}

def tcpdump_stop(path=...):
    ...
    if entry is None:
        last = _last_stop_status.get(path)
        if last:
            # B03：对已停实例的重复 stop，回放上一轮真实结果而非裸 true
            return json.dumps(last, ensure_ascii=False).encode("utf-8")
        return True   # 从未 start 过的路径：幂等（原始行为）
    ...
    code = entry[0].exitcode
    status = {"path": path, "stopped": True,
              "exitcode": code,
              "pcap_complete": code in (0, None)}
    _last_stop_status[path] = status
    ...（原有 warning 日志保留）
    if code not in (0, None):
        return json.dumps({**status, "warning": "pcap 可能不完整/未排序"}, ensure_ascii=False).encode("utf-8")
    return json.dumps(status, ensure_ascii=False).encode("utf-8")
```

**返回值形态变更（三态语义，修 B02）**：
- `b"ok"` → 不再使用；改为 JSON 文本响应（`recv_text_response` 可收，协议层 5/6 本就是 JSON 文本，帧格式零改动）
- 从未 start 的路径：仍返回 `True`（幂等，原始行为——但 JSON 形态下为 `{"stopped": false, "note": "no active capture for path"}`，语义自明，B02 解决）
- 服务端返回从裸 bool 变为 dict 后，datatype 5/6 的响应仍是无前缀 JSON（与 `{"res": bool}` 同型），**协议兼容**；桥侧解析同步改（§2.1）。

**修 4：时间戳兜底（B05，capture.py _decode_ts_ns 尾部）**

```python
return time.time_ns()   # 原 time.monotonic_ns()：monotonic 是开机秒数，按 Unix 纪元解释=1970
```

保序说明：单队列收包序=线序，墙钟 ns 在同一收包线程内单调非严格递减（NTP 跳变才回退），_sort_pcap_by_timestamp 本就为重排兜底，time_ns 不破坏该机制。降级路径补 WARNING 日志（"cmsg 时间戳缺失，回退墙钟"）。

### 1.3 服务端错误统一（B09 服务端侧）

172/173 的 `return b"error"` 改为 `return json.dumps({"error": "未启动监听"}).encode("utf-8")`（与 174 同型）。协议仍是文本响应，无兼容问题（桥侧 recv_ok 收到非 ok 字节即报错，文案见 §2.4）。

### 1.4 HTTP 文件服务超时 + CRLF 修复（终审遗留）

- `ThreadingHTTPServer` 实例设 `timeout = 60`（Handler socket 超时；大文件下载是持续写非 idle，60s 静默才断，不误杀）
- `parse_multipart` 尾部 CRLF 截断修复：值段 `value.endswith(b"\r\n")` 剥离改为"按段定界剥离"——分隔行前的 CRLF 属定界符，但仅当该段是最后一个字段（后面紧跟 `--boundary--`）时才有尾 CRLF 可剥；实现为：切段后若 `value.endswith(b"\r\n")` 且段尾即结束分隔则剥，正文场景由 split 消费定界符不再二次剥。**以"内容含尾部 CRLF 的 roundtrip 测试"为验收**。

### 1.5 不修清单（原始逻辑保持，用户裁定）

- B11/B12/B21（封堵/重定向/弹窗/chromium 检查顺序/浏览器环境）——git 全史核对与 1.2.1 逐字符一致
- B14（replay downlink 统计）——描述注明口径，修复单独排期
- socket_server TCP 服务端超时（客户端超时驱动断开，既有设计）

## 2. mcp_socket_server 侧（0.2.1）

### 2.1 capture_stop/capture_start 适配新响应（B01/B02/B03 桥侧）

- `socket_client.capture_start`：解析 `{"res": bool}` 不变（服务端 start 响应未改）
- `socket_client.capture_stop`：改为 `json.loads(recv_text_response())` 返回 dict（`{"stopped":..,"exitcode":..,"pcap_complete":..}` 或裸 `True`（从未 start））；`server.py capture_stop` 工具把 dict 透传进 results，`pcap_complete: false` 时该 target 判 failed 且 failed[].reason="pcap 可能不完整/未排序(exitcode=N)"——**reason:null 根治**
- 兼容旧靶机（<1.7.1 返回 `{"res": bool}`）：解析时先探 `isinstance(dict) and "res" in d` 走旧形态，否则新形态

### 2.2 裸 bool 返回值判失败（B23）

`_run_batch` 的 `data = fn(client)` 后加：

```python
if data is False:
    return TargetResult(target=host_port, ok=False,
                        error="agent 返回 False（业务失败）")
```

只精确匹配 `False`（`0`/`""`/`[]` 等合法返回值不受影响）。isfile/isdir 等已映射字段的工具走各自 server 代码不受影响；capture_start 失败从 `ok:1+results:[false]` 变为 `ok:0+failed[reason]`。

### 2.3 CAPTURE 锁失败释放（B04）

`_run_batch` finally 改为：

```python
finally:
    pool.release(client, healthy)
    if lock_mgr is not None and lock_class != LockClass.NONE:
        if lock_class != LockClass.CAPTURE or not <本次 ok>:
            lock_mgr.release(host_port, lock_class, key)
```

语义：CAPTURE 锁仍由 stop 释放（跨调用长锁不变）；但**本次调用失败**（ok=False，含 start 返回 False/异常）时立即释放，不再留僵尸锁。stop 本身失败（ok=False）也释放——与 stop finally 无条件释放的既有行为一致。实现上 run_one 内已知 ok，用 nonlocal/返回结构传递。

### 2.4 锁冲突文案补 target（B07）

`LockManager.acquire/release` 捕获内部 `TargetLock` 抛出的 `LockConflict`，重抛前填 `e.target = target`（LockConflict 已有 target 字段，TargetLock 内传 "?" 改为透传占位，外层覆盖）——4 处 `"?"` 硬编码消除，文案变"靶机 10.12.131.35 锁类 shell …"。

### 2.5 socket_listen stop + 描述更新（B10/B08/B15 桥侧）

- `socket_client.socket_listen` 加 `stop`→175（recv_ok 判 ok；error JSON 报 ConnectionError 含文案）
- `server.py socket_listen`：action 合法集合加 `stop`；docstring 改写（stop 用法、换端口先 stop、fetch/save 须在 stop 前、**删除"配合回放做对比测试"误导表述**（B15）改为"接收发往该端口的 TCP/UDP 流量（注意：TCP 监听不接收无握手的裸回放流，回放对比场景需回放侧重建会话或用 UDP）"）
- capture_start/stop 描述同步：返回结构变化、pcap_complete 语义、S06②"tcpdump 抓包"改为"AF_PACKET 嗅探（ps 中无 tcpdump 子进程属正常）"

### 2.6 其余桥层修复

| 项 | 修复 |
|---|---|
| B09 桥侧 | socket_listen save/clean/stop 的 `resp=b'error'` 报错文案翻译为"靶机无监听实例，请先 action=start"（对 b"error" 特判） |
| B13 | `_run_batch` 返回的 results 统一带 target：`_run_batch` 出口把 `[r.data for r in results if r.ok]` 包成 `[{"target": r.target, **(r.data if isinstance(r.data, dict) else {"result": r.data})}`——所有走 _run_batch 的工具（cmd_exec/capture/boce/replay/socket_listen）一次性生效；boce stats dict 与 target 合并 |
| B18 wait=false | run_one 返回 None 时 results 元素为 `{"target":.., "note": "wait=false 无返回值"}`（配合 B13 的 dict 包装自然实现）；描述同步 |
| B18 白名单 | config.example.yaml 加注释样例（`cmd_exec_whitelist: ["iptables ", "tcpdump ", "ss "]`），描述加"白名单为空=不限制（默认）" |
| B19 | `add_target` 注册前 TCP connect 探活（timeout 3s）：不通返回 `{"ok": false, "error": "host:port 不可达(TCP connect 失败)，未注册"}`；`force` 可选参数（默认 false）允许跳过探活注册离线靶机 |
| B17 | `pcap_flow_extract` 结果超 1000 条流时截断 + `{"target":.., "flows":[...], "total": N, "truncated": true}`；服务端静默空输出的行为描述注明"路径不存在/非目录返回空数组" |
| B20/S10 | 接受 FastMCP 默认 capabilities（框架行为），**不做**——在描述性 YAGNI 记录 |
| S01/S04/S05 | 全工具 schema 字段级补 description（FastMCP 签名 `Annotated[type, Field(description=...)]`）：targets 补"@tag 或 host:port"、boce mode 枚举说明（封堵/重定向有效；弹窗未实现恒失败）、protoType 枚举（1=TCP 2=UDP 4=ICMP）写入 pcap_flow_extract 描述 |
| S02 | force_sport/force_dport 改纯 `int`（非 Optional，默认 0=不改写）；tags/env/vlan 保持 nullable（语义就是"可选"） |
| S03 | `@mcp.tool()` 加 `title`（中文名）；annotations 用 `readOnlyHint=True`（isfile/isdir/pcap_flow_extract/list_targets）/`destructiveHint=True`（cmd_exec/capture_stop/remove_target），FastMCP 支持 |
| S06 | 四处描述与实际对齐：cmd_exec 白名单、capture AF_PACKET、remove_target（§2.7）、socket_listen（§2.5） |
| S07 | capture_start/stop 默认 path 保留（原始行为）但描述加"多任务请显式传不同 path，默认路径全局共用" |
| S09 | B17 的 1000 条上限 + cmd_exec stdout 超 64KB 截断加 `"stdout_truncated": true`（在 client 层 cmd_exec 收 gzip 结果后处理） |
| S11 | B01（reason 必有文案）/B07（target 真名）/B09（统一文案）修复后自然消解 |

### 2.7 remove_target 防误删活跃监听靶机（B16）

`server.py remove_target`：在现有"活跃锁拒绝"检查后追加一项——查该靶机是否有活跃监听：无独立查询接口，采用**标记法**：`socket_listen action=start` 成功后在该靶机的锁表登记一个长效标记锁（`LockClass.SYSTEM, key=("listen-liveness",)`，不随调用结束释放）；`action=stop` 成功后释放。remove_target 的 `has_active(host)` 天然覆盖（锁表里有就拒绝）。stop 语义与 1.7.1 之前的旧靶机：start 在旧靶机成功后标记同样登记，但无法 stop（旧服务端无 175）——标记永不释放会导致该靶机永久不可删，**因此标记锁的登记仅在新靶机（协议含 175）生效**：start 前 `version_query` 探测版本 ≥1.7.1 才登记。复杂度可控（一个版本判断），避免引入不可清理状态。

### 2.8 timeout 参数（既有 §6 全文保留）

§6.1-§6.6（9 工具加可选 timeout、默认值=现硬编码值、boce_run 业务参数改名 http_timeout、default_timeout config、HTTP 60s、TCP 不动）原样并入本批次。

## 3. 测试计划

### socket_server（1.7.1）
- 复现测试先行（报告的验证清单直接做用例）：
  - B01：高流量（回放灌包）start→立即 stop：stop ≤3s 返回；pcap capinfos 无 "cut short"
  - B02：stop 从未 start 的路径 → `{"stopped": false, "note": ...}`
  - B03：制造停止失败后重试 → 返回上轮 exitcode 信息（_last_stop_status 回放）
  - B05：抓 10 包 → capinfos First packet time 为当前墙钟 ±5s
  - B06：B01 场景后逐记录解析 missing_tail=0
  - 175：start→stop→ok→再 stop→error；stop 后 172/174 报未启动；换端口 start→新 pid
  - CRLF：上传尾部带 \r\n 的文件 → roundtrip 字节一致
  - HTTP timeout：慢客户端（nc 挂连接不发请求）60s 断开
  - test_e2e.py：175 段 + 新 stop 响应形态段
- 真实靶机（10.12.131.82）：start(39002)→回放打流→stop（≤3s）→capinfos 墙钟+完整性→ss 无监听→换端口 start(39003)→stop

### mcp_socket_server（0.2.1）
- mock 扩展：capture stop 新 JSON 形态（三种：stopped/never-started/previous-status）、175 handler、171 端口不一致 error
- 单测：B23（False→failed）、B04（start 失败锁释放）、B07（冲突文案含真名）、B13（results 带 target）、B18（wait=false note）、B19（探活失败拒注册/force 绕过）、S03（annotations 在 schema）、S02（force_sport 非 nullable）
- 工具层：capture_stop 新响应三态、socket_listen stop、pcap_flow 截断
- 全量 pytest + ListTools（12 工具、9 个 timeout、字段 description、annotations）
- 真实靶机：stop 链路 + B01 高流量复现场景走 MCP 全链

## 4. 发版与中间态

1. socket_server 1.7.1 先发（§1 全部）
2. mcp_socket_server 0.2.1 后发（§2 全部；capture_stop 新解析对旧靶机兼容 §2.1；socket_listen stop 在旧靶机 → error → failed 可解释）
3. 靶机自动升级铺开 1.7.1 后全功能生效

## 5. 明确不做（YAGNI + 用户裁定）

- B11/B12/B21：封堵/重定向/弹窗/chromium 检查顺序/浏览器环境——**保持原始逻辑**（git 全史核对一致）
- B14 修复：描述注明口径，DPI 环境验证后单独排期
- B20/S10 capabilities：FastMCP 框架默认行为，不做
- 多端口并发监听、stop 前自动 fetch、pause/resume、TCP 服务端超时
- S08 additionalProperties：FastMCP 框架层能力，本批不做（schema 收紧依赖框架支持）
