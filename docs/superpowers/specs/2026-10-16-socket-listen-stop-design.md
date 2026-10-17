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
- **B14 不修（核实为设计行为，此前定性有误）**：`replayer.py:137` 单臂模式（uplink==downlink 且 VLAN 相同）+ 不改五元组时，`:743` 所有包从 uplink 发出、不建流——downlink/flow_count 恒 0 是**单臂回放的定义行为**。测试报告的单臂回放场景得出"恒 0"是误读。归入"描述完善"而非修复。
- B17 的"静默空输出"同理核为 pcap_flow.py:42 的既有行为（非目录不入扫描列表），归入描述完善 + 可选的输入校验增强（见 §2.6）。

## 1. socket_server 侧（1.7.1）

### 1.1 监听停止：新增 datatype 175（B10/B08）

```
datatype 175  socket监听-停止监听
请求:  无 payload
响应:  成功 -> b"ok"（与 172/173 同型）
       失败 -> {"error": "..."} JSON
```

handlers.py（datatype 200 之后、`else: pass` 之前——174 与 else 之间还有 18/200，位置任意均可）：

```python
elif datatype == 175:
    logger.info("socket监听-停止监听，类型：%s" % datatype)
    if ss is None:
        return json.dumps({"error": "未启动监听"}).encode("utf-8")
    ss.stop_server()
    # 成功判定以"端口确实没了"为准；kill -9 异步生效，ss 表更新有调度延迟，
    # 重试 3 次 x0.5s 防误判失败（评审第 1 项）
    stopped = False
    for _ in range(3):
        if not ss.pid():
            stopped = True
            break
        time.sleep(0.5)
    ss = None                       # 释放单例：下次 171 可换端口重建
    if stopped:
        logger.info("socket监听已停止")
        return b"ok"
    return json.dumps({"error": "停止监听失败，端口仍被占用"}).encode("utf-8")
```

关键决定（已核实）：
- 成功判定**不用** `stop_server()` 返回值（其返回 `True`/`None`，"无可杀"时是 `None`），以停后 `ss.pid()` 为空为准（带重试）。
- **停止机制的事实描述（评审第 3 项修正）**：`stop_server()` 置 `_stop_flag=False` 对子进程**无效**——监听进程是 `multiprocessing.Process` fork，子进程持有 fork 时刻的 flag 副本，父进程的置位传导不过去；**实际停止完全依赖 terminate/SIGKILL**。本设计不依赖优雅退出，175 的行为与该机制吻合。
- stop 后 cachedata 线程（start_server 起的 daemon 线程，`while True` 无退出条件）仍存活：ss=None 后 q 永远空，走 sleep(1) 空转分支——不刷日志不异常，代价是每次 stop→start 泄漏一个 idle 线程（内网场景频次极低，接受）。
- `ss = None`（do() 顶部已有 `global ss`，赋值生效）解决换端口：下次 171 走 `not ss` 分支用新端口重建。**同时修 B08**：171 分支加端口一致性检查——`ss and ss.pid()` 且 `data.get("port") != ss.port` 时不再静默 pass，返回 `{"error": "已有监听运行在端口 X，请先停止(datatype 175)"}`。
- stop 后 172/173/174 自然落入既有 error 分支（已核实），无需改。
- `stop_server()` 内 `os.system(f"kill -9 {pid}")` 是既有代码，不动（pid 串来自 ss+awk 提取，远程调用无注入面；本地 comm 含逗号的进程理论上可干扰提取——既有 nit，不顺手改）。

### 1.2 抓包停止链路（B01+B03+B06 同根同修）

**根因（已核实 capture.py:236-253）**：`_recv_loop` 的 drain 段逐帧读到 socket.timeout 为止，**无总时限**——高流量下在途帧持续到达，"队列已空"永不成立，线程不退出 → `stop()` join(10s) 超时 → `return False`（不 flush 不 close）→ 子进程 `sys.exit(1)` → 文件截断未排序。B01（10.8s 失败）/B03（重试 true 掩盖 exitcode=1）/B06（590B 尾部截断）是同一死结的三个症状。

**修 1：drain 加绝对 deadline（capture.py _recv_loop drain 段，完整可抄代码）**

```python
if self._stopped.is_set() and not self._exc:
    try:
        self._sock.settimeout(1.0)
        drain_deadline = time.monotonic() + 2.0   # 置位后最多再 drain 2s
        drained_after = 0
        while True:
            if time.monotonic() > drain_deadline:
                logger.warning(
                    f"drain 超时(2s)，放弃残余在途帧 {drained_after} 包已收 {self.eth} -> {self.path}")
                break
            try:
                data, ancdata, _flags, _addr = self._sock.recvmsg(65535, 1024)
            except socket.timeout:
                logger.info(f"drain 完成：停止后补收 {drained_after} 包 {self.eth} -> {self.path}")
                break  # 队列已空
            except OSError:
                break  # 网卡不可用/已关闭
            if not data:
                break
            ts_ns = self._decode_ts_ns(ancdata)
            frame = self._restore_vlan(data, ancdata)
            self._write_packet(frame, ts_ns)
            drained_after += 1
    except Exception:
        pass
```

放弃时机只在两条记录之间（`_write_packet` header+data 完整执行完才回 recvmsg），**deadline 放弃不产生半条记录**；半条风险只在修 2 的强制 close 竞态（已声明）。正常排空与超时放弃均记录帧数（评审第 4 项：与 B06 验证对账）。

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

（修 1 生效后此路径仅在极端场景触达；保留防御。竞态最坏损失：close 与写线程 write 的瞬时交叉丢 1 条记录——写线程下一轮 recvmsg 抛 OSError 退出，不会无限写坏。）

**修 3：stop 结果留存可查（B03，capture.py tcpdump_stop + handlers.py datatype 6 配套）**

`tcpdump_stop` 返回从裸 bool 改为 dict（三态+第四态"停止失败"）：

```python
_last_stop_status: dict[str, dict] = {}   # 模块级，{path: {...}}

def tcpdump_stop(path="/home/tmp/tmp.pcap"):
    with _captures_lock:
        entry = _captures.get(path)
    if entry is None:
        last = _last_stop_status.get(path)
        if last:
            # B03：对已停实例的重复 stop，回放上一轮真实结果而非裸 true
            return {**last, "note": "already stopped（重复 stop，回放上轮结果）"}
        return {"stopped": False, "note": "no active capture for path"}
    ok = _stop_capture_entry(entry)
    if not ok:
        # 停止失败（join 超时）：实例保留可重试（既有行为），返回第四态
        status = {"path": path, "stopped": False,
                  "error": "停止未完成（停止线程超时），实例保留可重试"}
        _last_stop_status[path] = status
        return status
    with _captures_lock:
        _captures.pop(path, None)
    code = entry[0].exitcode
    status = {"path": path, "stopped": True,
              "exitcode": code,
              "pcap_complete": code in (0, None)}
    _last_stop_status[path] = status
    if code not in (0, None):
        status["warning"] = "pcap 可能不完整/未排序"
        logger.warning(f"抓包子进程退出码 {code}，本轮 pcap 可能不完整/未排序：{path}")
    return status
```

**handlers.py datatype 6 配套修改（评审第 3 项：原设计漏写，直接照旧包裹会把 bytes 丢进 json.dumps 炸 TypeError）**：

```python
    # 旧： res = json.dumps({"res": response}).encode("utf-8")
    # 新： response 已是 dict（tcpdump_stop 改造后），直出
    res = json.dumps(response, ensure_ascii=False).encode("utf-8")
    return res
```

**四态语义汇总（修 B02+B03）**：

| 场景 | 响应 | 调用方判定 |
|---|---|---|
| 正常停止（exitcode 0/None） | `{"stopped": true, "exitcode": 0, "pcap_complete": true}` | 成功 |
| 停止但有损（exitcode≠0） | `{"stopped": true, "exitcode": N, "pcap_complete": false, "warning": ...}` | **failed**（pcap 可能不完整） |
| 从未 start 的路径 | `{"stopped": false, "note": "no active capture for path"}` | 非失败（幂等明示，B02） |
| 停止失败（join 超时） | `{"stopped": false, "error": "停止未完成..."}` | **failed**（可重试） |

响应仍是无前缀 JSON 文本（与旧 `{"res": bool}` 同型），帧格式零改动；**兼容影响显式声明**：新服务端响应只保证配 0.2.1+ 桥，旧桥（`get("res", False)`）对 1.7.1 靶机的 capture_stop 会误判失败——发版顺序 1.7.1→0.2.1 期间存在此中间态，CHANGELOG 标注。桥侧解析见 §2.1。

**修 4：时间戳兜底（B05，capture.py _decode_ts_ns 尾部）**

```python
return time.time_ns()   # 原 time.monotonic_ns()：monotonic 是开机秒数，按 Unix 纪元解释=1970
```

保序说明：单队列收包序=线序，墙钟 ns 在同一收包线程内单调非严格递减（NTP 跳变才回退），_sort_pcap_by_timestamp 本就为重排兜底，time_ns 不破坏该机制。降级路径补 WARNING 日志（"cmsg 时间戳缺失，回退墙钟"）。

### 1.3 服务端错误统一（B09 服务端侧）

172/173 的 `return b"error"` 改为 `return json.dumps({"error": "未启动监听"}).encode("utf-8")`（与 174 同型）。协议仍是文本响应，无兼容问题（桥侧 recv_ok 收到非 ok 字节即报错，文案见 §2.4）。

### 1.4 HTTP 文件服务超时 + CRLF 修复（终审遗留，修法已实测定位）

- **Handler 超时（评审第 10 项修正：超时设 Handler 类属性而非 server 实例——`BaseServer.timeout` 只作用于 accept 的 select 循环，不传导到已 accept 连接）**：`FileHandler.timeout = 60`（`BaseHTTPRequestHandler` 继承 `StreamRequestHandler`，setup 时对连接 socket `settimeout`，idle 语义：每次 recv/send 60s 静默才断；大文件下载持续写不触发）。连接数不加上限（内网信任模型不变）。
- **CRLF 截断实测定案**（本轮 review 用现解析器实测三组：无尾 CRLF ✅／尾 1 条 ❌ 丢 1 条／尾 2 条 ❌ 丢 1 条）：根因是 `parse_multipart` 里 `if value.endswith(b"\r\n"): value = value[:-2]` 这两行——`split(delim)` 已把定界串（含其前导 CRLF）消费掉，段值尾**不存在**定界 CRLF，内容自己的尾 CRLF 就是不该剥的内容。**修法 = 直接删除这两行剥除逻辑**（原稿"仅最后字段才剥"的叙述是错的，multipart 语义下 split 后无需任何尾部剥除）。验收=三组 roundtrip 全字节一致。

### 1.5 不修清单（原始逻辑保持，用户裁定）

- B11/B12/B21（封堵/重定向/弹窗/chromium 检查顺序/浏览器环境）——git 全史核对与 1.2.1 逐字符一致
- B14（replay downlink 统计）——描述注明口径，修复单独排期
- socket_server TCP 服务端超时（客户端超时驱动断开，既有设计）

## 2. mcp_socket_server 侧（0.2.1）

### 2.1 capture_stop/capture_start 适配新响应（B01/B02/B03 桥侧）

- `socket_client.capture_start`：解析 `{"res": bool}` 不变（服务端 start 响应未改）
- `socket_client.capture_stop`：`json.loads(recv_text_response())` 后按四态解析：
  - dict 含 `stopped` 键 → **新形态透传**（1.7.1 靶机）
  - dict 含 `res` 键 → **旧形态（<1.7.1 靶机 `{"res": bool}`）**：`res: true` → `{"stopped": true, "legacy": true}`；**`res: false` → `{"stopped": false, "error": "停止未完成（旧版语义），可重试"}`（评审修正：旧版 false=真失败，绝不能映射为非失败的 stopped:false——那会回归 B03）**
  - 其他 → `{"stopped": false, "error": "无法解析的服务端响应: ..."}`（防御）
- `server.py capture_stop` 工具判定：`stopped: true and pcap_complete: false` → **failed**，reason="pcap 可能不完整/未排序(exitcode=N)"；`stopped: false and error 存在` → **failed**（reason=error 文案）；`stopped: false and note 存在` → 非失败（幂等明示，B02）——**reason:null 根治**

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
        # ok 为 run_one 局部变量(默认 True;data is False 分支与 except 分支置 False)
        if lock_class != LockClass.CAPTURE or not ok:
            lock_mgr.release(host_port, lock_class, key)
```

语义：CAPTURE 锁仍由 stop 释放（跨调用长锁不变）；但**本次调用失败**时立即释放，不再留僵尸锁。stop 本身失败（ok=False）也释放——与 stop finally 无条件释放的既有行为一致。实现：run_one 内局部变量 `ok = True`，`data is False` 分支与 except 分支置 `False`，finally 直接可见（同函数作用域）。

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
| B13 | `_run_batch` 返回的 results 统一带 target：出口把 `[r.data for r in results if r.ok]` 包成 `[{"target": r.target, **(r.data if isinstance(r.data, dict) else {"result": r.data})} for r in results if r.ok]`——所有走 _run_batch 的工具一次性生效。**波及面（评审补充）**：capture_start 返回 bool 会包成 `{"target":..,"result": bool}`（形态变化）——mock 测试断言与 capture_start 描述同步更新；其余工具（cmd_exec/boce/replay/socket_listen 返回 dict）仅多一个 target 键，既有字段保留 |
| B18 wait=false | run_one 返回 None 时 results 元素为 `{"target":.., "note": "wait=false 无返回值"}`——**显式 None→note 分支**（评审修正：B13 的 dict 包装对 None 产出的是 `"result": null`，note 不会"自然实现"，须在包装处显式判 None）；描述同步 |
| B18 白名单 | config.example.yaml 加注释样例（`cmd_exec_whitelist: ["iptables ", "tcpdump ", "ss "]`），描述加"白名单为空=不限制（默认）" |
| B19 | `add_target` 注册前 TCP connect 探活（timeout 3s）：不通返回 `{"ok": false, "error": "host:port 不可达(TCP connect 失败)，未注册"}`；`force` 可选参数（默认 false）允许跳过探活注册离线靶机。**host 解析**：hostname/裸 IPv6 走 `socket.create_connection` 均可；**带方括号 IPv6（"[fd00::1]"）create_connection 失败——探活前剥方括号**（评审第 9 项）；已核实 registry.resolve 对 IPv6 裸地址原样透传不损坏 |
| B17 | `pcap_flow_extract` 结果超 1000 条流时截断 + `{"target":.., "flows":[...], "total": N, "truncated": true}`；服务端静默空输出的行为描述注明"路径不存在/非目录返回空数组" |
| B20/S10 | 接受 FastMCP 默认 capabilities（框架行为），**不做**——在描述性 YAGNI 记录 |
| S01/S04/S05 | 全工具 schema 字段级补 description（FastMCP 签名 `Annotated[type, Field(description=...)]`）：targets 补"@tag 或 host:port"、boce mode 枚举说明（封堵/重定向有效；弹窗未实现恒失败）、protoType 枚举（1=TCP 2=UDP 4=ICMP）写入 pcap_flow_extract 描述 |
| **描述完善（把"设计行为"写成明文，防再被误报）** | ① `replay_run`：**单臂回放（uplink==downlink 且无 VLAN 差异）时所有包计为 uplink，downlink/flow_count 恒 0 属正常，需方向统计用双接口**（B14 口径）；② `boce_run`：mode 仅"封堵/重定向"有判定实现，"弹窗"未实现恒失败，其他值按未知模式恒失败；③ `capture_stop`：幂等语义明示（未在抓的路径返回 stopped:false 非报错）；④ `pcap_flow_extract`：路径不存在/非目录返回空数组（既有行为） |
| S02 | **force_sport/force_dport 改纯 `int` 默认 0，且 wire 语义定死：0 → payload 省略该字段**（服务端保持 None 进 `no_tuple_modification` 判定）——若把 0 发上 wire，`replayer.py:139` 的 `all(x is None ...)` 变 False → 单臂回放开始建流 → downlink/flow_count 不再恒 0，与 B14"设计行为"口径直接冲突（评审第 6 项）；tags/env/vlan 保持 nullable（语义就是"可选"） |
| S03 | `@mcp.tool()` 加 `title`（中文名）；annotations：`readOnlyHint=True` 标 isfile/isdir/pcap_flow_extract/list_targets，`destructiveHint=True` 标 cmd_exec/capture_stop/remove_target；**其余 5 个（add_target/capture_start/boce_run/replay_run/socket_listen）不标 annotations**（读写语义混合，标了反误导——评审补充归类） |
| S06 | 四处描述与实际对齐：cmd_exec 白名单、capture AF_PACKET、remove_target（§2.7）、socket_listen（§2.5） |
| S07 | capture_start/stop 默认 path 保留（原始行为）但描述加"多任务请显式传不同 path，默认路径全局共用" |
| S09 | B17 的 1000 条上限 + cmd_exec stdout 超 64KB 截断加 `"stdout_truncated": true`（在 client 层 cmd_exec 收 gzip 结果后处理） |
| S11 | B01（reason 必有文案）/B07（target 真名）/B09（统一文案）修复后自然消解 |

### 2.7 remove_target 防误删活跃监听靶机（B16，载体重设计——评审裁定锁表标记法不可行）

**原锁表标记法被否**（locks.py:74-82 排他锁忽略 key：标记锁与 start/stop 的 SYSTEM 锁同类必然互自冲突；若嵌套成功则 stop 永远 acquire 不到 → 死锁，且阻塞该靶机全部排他操作）。**新载体：registry 表加 liveness 标志列**：

```python
# registry.py targets 表加列（SQLite ALTER TABLE ADD COLUMN，带迁移：已有库启动时补列）
#   listen_liveness INTEGER DEFAULT 0   -- 0=无监听 1=有活跃监听

# server.py socket_listen 工具内（run_one 成功后，非锁逻辑）：
#   start 成功 → _registry.set_listen_liveness(host, True)
#   stop 成功  → _registry.set_listen_liveness(host, False)
# remove_target（server.py）：现有 has_active 检查后追加——
#   if _registry.get_listen_liveness(host):
#       return {"ok": False, "error": "靶机 X 有活跃监听，请先 socket_listen action=stop"}

# 版本探测（决定是否登记）：恢复 client 层 version() 方法（datatype 14，3 行，仅内部用，
#   不恢复 MCP 工具）。注意 handlers 14 返回 json.dumps(VERSION)（带引号 JSON 字符串），
#   client 需 json.loads；版本比较用 tuple 化（ major,minor,patch ），禁字符串直比
#   （"1.10.0" < "1.9.0" 字典序陷阱）。≥1.7.1 才登记 liveness（旧靶机无 175 无法清理，
#   登记会造成永久不可删）。
```

优点：registry 持久化（中央节点重启不丢）、与锁表完全解耦、remove_target 检查成本一次 SQL。代价：socket_listen 工具内多一次 DB 写 + start 时一次 version RTT。

### 2.8 timeout 参数（全文并入，不再引用外部章节——评审发现原引用的 §6.1-§6.6 不存在于任何文档）

**范围**：9 个业务工具加统一可选 `timeout`（秒）；注册表 3 工具（list/add/remove_target）不加。

| 工具 | timeout 默认值（=现 `_run_batch` 硬编码值） |
|---|---|
| isfile / isdir | 15 |
| pcap_flow_extract | 30 |
| cmd_exec | 60（wait=false 时 15） |
| capture_start / capture_stop | 30 |
| boce_run | 60 |
| replay_run | 120 |
| socket_listen | 30 |

**schema 形态**（可选不入 required）：
```json
"timeout": {"type": "integer", "default": <上表值>, "description": "单靶机操作超时秒数(connect+执行+返回),多靶机并行各自计时"}
```

**传递链（管道已存在，只接形参）**：工具形参 `timeout` → `_run_batch(..., timeout=timeout)` → `pool.acquire(socket_timeout=int(timeout))` → `SocketServerClient(timeout=...)`（约束 connect/send/recv）。默认行为零变化。

**boce_run 双 timeout**：工具级 `timeout=60`（新增）；业务参数 `timeout: int = 3`（拨测 HTTP 请求级）改名 `http_timeout`（默认 3 不变）。**wire payload 键名保持 `"timeout"` 不变**（socket_client.boce_run 现发 `payload["timeout"]=http_timeout` 值——字段名不动，TCP 协议零改动）。破坏性：旧调用显式传过 `boce_run timeout=10` 语义变化——审计查证 0.2.0 期间 12 次调用无显式 timeout，实际影响 ~0，CHANGELOG 标注。

**底层默认可配**：`config.yaml` 新增 `default_timeout: 30`；`socket_client.py DEFAULT_TIMEOUT` 改为模块加载时 env `MCP_DEFAULT_TIMEOUT` 优先、其次 config，读不到回落 30（不动态读——池化连接建连时固化）。

**socket_server HTTP 超时**：见 §1.4（`FileHandler.timeout = 60`）。

**cmd_exec wait=false**：fn 立即返回 None，timeout 形参无意义但无害（保留，统一签名）。

## 3. 测试计划

### socket_server（1.7.1）
- 复现测试先行（报告的验证清单直接做用例）：
  - B01：高流量（回放灌包）start→立即 stop：stop 返回时间**断言 ≤5s**（评审修正：原 ≤3s 未计 sort 耗时——`_sort_pcap_by_timestamp` 全量载入内存排序，大 pcap 秒级；具体阈值以实测校准后在本用例固化），pcap capinfos 无 "cut short"
  - B02：stop 从未 start 的路径 → `{"stopped": false, "note": ...}`
  - B03：制造停止失败后重试 → 返回上轮 exitcode 信息（_last_stop_status 回放）
  - B05：抓 10 包 → capinfos First packet time 为当前墙钟 ±5s
  - B06：B01 场景后逐记录解析 missing_tail=0（对照 drain 日志"补收 N 包/放弃 N 包"对账）
  - 175：start→stop→ok→再 stop→error；stop 后 172/174 报未启动；换端口 start→新 pid
  - CRLF：上传尾部带 \r\n 的文件 → roundtrip 字节一致（三组：无尾/尾1条/尾2条）
  - HTTP timeout：慢客户端（nc 挂连接不发请求）60s 断开（断言 FileHandler.timeout 生效）
  - test_e2e.py：175 段 + 新 stop 响应四态段
- 真实靶机（10.12.131.82）：start(39002)→回放打流→stop（计时）→capinfos 墙钟+完整性→ss 无监听→换端口 start(39003)→stop
- 既有风险备注（非本批引入）：`_sort_pcap_by_timestamp` 全量驻内存，GB 级 pcap 有 OOM 风险——记录不在本批修

### mcp_socket_server（0.2.1）
- mock 扩展：capture stop 新 JSON 三形态（stopped/never-started/previous-status）、175 handler、171 端口不一致 error
- 单测：B23（False→failed）、B04（start 失败锁释放）、B07（冲突文案含真名）、B13（results 带 target；capture_start bool 包装形态）、B18（wait=false note）、B19（探活失败拒注册/force 绕过）、S03（annotations 在 schema）、S02（force_sport 非 nullable）、B16（listener 活跃 remove 拒绝/stop 后可删）
- 工具层：capture_stop 新响应三态、socket_listen stop、pcap_flow 截断
- **描述断言防回退（评审补充）**：ListTools 校验加——capture_start 描述含"AF_PACKET"、socket_listen 描述不含"配合回放"、boce_run 描述含"弹窗未实现"、replay_run 描述含"单臂"
- 全量 pytest + ListTools（12 工具、9 个 timeout、字段 description、annotations）
- 真实靶机：stop 链路 + B01 高流量复现场景走 MCP 全链

## 4. 发版与中间态

1. socket_server 1.7.1 先发（§1 全部）
2. mcp_socket_server 0.2.1 后发（§2 全部；capture_stop 新解析对旧靶机兼容 §2.1；socket_listen stop 在旧靶机 → error → failed 可解释）
3. 靶机自动升级铺开 1.7.1 后全功能生效

## 5. 明确不做（YAGNI + 用户裁定）

- B11/B12/B21：封堵/重定向/弹窗/chromium 检查顺序/浏览器环境——**保持原始逻辑**（git 全史核对一致）
- **B14：replay downlink/flow_count 单臂恒 0——设计如此**（replayer.py:137/743 directionless 定义行为），归入描述完善
- B17 静默空输出主体：非目录不入扫描是既有行为，仅描述完善（可选增强见 §2.6 的截断上限）
- B20/S10 capabilities：FastMCP 框架默认行为，不做
- 多端口并发监听、stop 前自动 fetch、pause/resume、TCP 服务端超时
- S08 additionalProperties：FastMCP 框架层能力，本批不做（schema 收紧依赖框架支持）
