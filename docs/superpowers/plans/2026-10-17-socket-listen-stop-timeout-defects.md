# 监听停止 + timeout + 测试报告缺陷修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 单批次落地 socket_server 1.7.1（datatype 175 停止监听 + 抓包停止链路三修 + 时间戳修复 + HTTP Handler 超时 + CRLF 修复）与 mcp_socket_server 0.2.1（stop 动作 + 9 工具 timeout 参数 + capture_stop 四态适配 + B04/B07/B13/B16/B18/B19 桥层修复 + S01-S07/S09/S11 schema 完善）。

**Architecture:** socket_server 侧全部为既有模块的增量修改（handlers/socket_listen/capture/http_file），协议帧格式零改动、新增 1 个 datatype（175）；mcp 侧同样增量（socket_client/server/locks/registry/config），对旧靶机保持兼容解析。

**Tech Stack:** Python 3.8.10（socket_server 侧，禁 3.9+ 语法）；Python 3.10 + FastMCP/pydantic（mcp 侧）。

**Spec:** `docs/superpowers/specs/2026-10-16-socket-listen-stop-design.md`（深度 review 后定稿，ce5a5c4）

## Global Constraints

- Python 3.8.10 兼容（socket_server 侧禁用 3.9+ 语法如 `dict |`、`str.removeprefix`）
- 禁止 shell=True；TCP 协议帧格式零改动（新增 datatype 175 是分发分支，不改帧）
- 封堵/重定向/弹窗/boce chromium 顺序/replay 单臂统计（B11/B12/B21/B14）——**原始逻辑保持，一行不改**
- force_sport/force_dport 值 0 → wire payload **省略该字段**（服务端保持 None，保单臂 directionless 语义）
- 所有工具 schema 的可选 `description` 参数（既有 CallDescription）保持不变
- 两仓独立提交：socket_server 在 `/opt/socket_server`（测试 `python3 -m pytest tests/ -q`），mcp 在 `/opt/mcp_socket_server`（测试 `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q`）
- `/opt/socket/config` 的 `port=9000` 历史未接线配置不得参照、不得改动

---

## Part A：socket_server 1.7.1

### Task 1: datatype 175 停止监听 + 171 换端口报错

**Files:**
- Modify: `/opt/socket_server/socket_server/handlers.py:338-365`（174 之后、`else:` 之前插入 175；171 分支加端口检查）
- Test: `/opt/socket_server/test_e2e.py`（文件末尾追加 175 段）

**Interfaces:**
- Produces: datatype 175（无 payload → `b"ok"` / `{"error": "..."}` JSON）；171 端口不一致 → `{"error": "已有监听运行在端口 X，请先停止(datatype 175)"}`。Task 4（mcp 侧）消费此接口。

- [ ] **Step 1: handlers.py 171 分支加端口一致性检查（B08）**

定位 `elif datatype == 171:` 块内（约 301 行）：

```python
        if ss and ss.pid():
            pass
```

改为：

```python
        if ss and ss.pid():
            req_port = data.get("port", 30001)
            if req_port != ss.port:
                return json.dumps({"error": "已有监听运行在端口 %s，请先停止(datatype 175)" % ss.port},
                                  ensure_ascii=False).encode("utf-8")
            pass
```

- [ ] **Step 2: handlers.py 插入 datatype 175（200 之后、else 之前，约 365 行前）**

```python
    # socket监听-停止监听（v1.7.1，spec: docs/superpowers/specs/2026-10-16-socket-listen-stop-design.md §1.1）
    elif datatype == 175:
        logger.info("socket监听-停止监听，类型：%s" % datatype)
        if ss is None:
            return json.dumps({"error": "未启动监听"}).encode("utf-8")
        ss.stop_server()
        # 成功判定以"端口确实没了"为准；kill -9 异步生效，ss 表更新有调度延迟，重试 3 次 x0.5s
        stopped = False
        for _ in range(3):
            if not ss.pid():
                stopped = True
                break
            time.sleep(0.5)
        ss = None   # 释放单例：下次 171 可换端口重建
        if stopped:
            logger.info("socket监听已停止")
            return b"ok"
        return json.dumps({"error": "停止监听失败，端口仍被占用"}).encode("utf-8")
```

前置确认：`import time` 已在 handlers.py 顶部（若无则加）；`time.sleep` 用的是此 import。

- [ ] **Step 3: py_compile + 本机 e2e 验证 175 全链**

```bash
cd /opt/socket_server
python3 -m py_compile socket_server/handlers.py && echo COMPILE-OK
# 起测试实例（避开生产 9000；HTTP 9001 若被占换 19001——本任务未涉 HTTP）
python3 -m socket_server serve -p 19000 > /tmp/srv175.log 2>&1 &
SRV=$!; sleep 2
python3 - <<'EOF'
import socket, struct, json

def req(dt, payload=b""):
    s = socket.create_connection(("127.0.0.1", 19000), timeout=15)
    body = struct.pack("i", dt) + payload
    s.sendall(struct.pack("i", len(body)) + body)
    import time; time.sleep(0.8)
    s.settimeout(2)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    return data

# 1) 未启动时 stop → error JSON
r = req(175)
print("stop-before-start:", r)
assert b"未启动监听" in r

# 2) start 39001
r = req(171, json.dumps({"host": None, "port": 39001}).encode())
print("start:", r)
assert b"39001" not in r or json.loads(r)["pid"], r

# 3) 同端口 start 幂等
r = req(171, json.dumps({"host": None, "port": 39001}).encode())
assert json.loads(r)["pid"], r
print("same-port idempotent OK")

# 4) 换端口 start → error（B08）
r = req(171, json.dumps({"host": None, "port": 39002}).encode())
print("port-switch:", r)
assert b" datatype 175" in r

# 5) stop → ok
r = req(175)
print("stop:", r)
assert r == b"ok"

# 6) stop 后 174 → error
r = req(174)
assert b"未启动监听" in r
print("after-stop fetch errors OK")
EOF
kill $SRV
```

Expected: 全部断言过，输出各步标签。

- [ ] **Step 4: test_e2e.py 追加 175 段（沿用逐步 check 风格）**

在 `/opt/socket_server/test_e2e.py` 文件末尾追加（复用该文件既有的 `new_sock`/`send_request`/`recv_text_response`/`check` 辅助——先读文件确认函数名一致）：

```python
# ===== Test: datatype 175 停止监听 =====
print("\n[Test] datatype 175 - socket监听停止")
s = new_sock()
send_request(s, 171, json.dumps({"host": None, "port": 39011}).encode())
r = recv_text_response(s)
check("171 start", "pid" in json.loads(r), r)
s.close()

s = new_sock()
send_request(s, 175)
r = recv_text_response(s)
check("175 stop", r == b"ok", r)
s.close()

s = new_sock()
send_request(s, 175)
r = recv_text_response(s)
check("175 re-stop error", b"未启动监听" in r, r)
s.close()
```

- [ ] **Step 5: Commit**

```bash
cd /opt/socket_server
git add socket_server/handlers.py test_e2e.py
git commit -m "feat: datatype 175 停止监听 + 171 换端口明确报错(B08/B10)"
```

---

### Task 2: 抓包停止链路三修（B01 drain deadline + B06 失败收尾 + B03/B02 结果留存三态）

**Files:**
- Modify: `/opt/socket_server/socket_server/capture.py:236-253`（drain 段）、`:367-381`（stop 超时收尾）、`:589-612`（tcpdump_stop 四态）
- Modify: `/opt/socket_server/socket_server/handlers.py:91-98`（datatype 6 返回包裹）

**Interfaces:**
- Produces: `tcpdump_stop(path) -> dict`（四态：正常停 `{"stopped":True,"exitcode":N,"pcap_complete":bool}`；从未 start `{"stopped":False,"note":...}`；重复 stop 回放 `_last_stop_status`；停止失败 `{"stopped":False,"error":...}`）。datatype 6 wire 响应从 `{"res": bool}` 变为直出 dict。Task 5（mcp 桥侧）消费。

- [ ] **Step 1: capture.py drain 段加 deadline（完整替换 `_recv_loop` 的 drain 块）**

定位 `capture.py` 约 236-253 行（`if self._stopped.is_set() and not self._exc:` 起的 try 块），整块替换为：

```python
        if self._stopped.is_set() and not self._exc:
            try:
                self._sock.settimeout(1.0)
                drain_deadline = time.monotonic() + 2.0   # 置位后最多再 drain 2s（B01：无时限下
                # 高流量在途帧持续到达，队列永不空，线程不退 -> join(10s) 超时 -> 文件不收尾）
                drained_after = 0
                while True:
                    if time.monotonic() > drain_deadline:
                        logger.warning(
                            f"drain 超时(2s)，放弃残余在途帧：停止后已补收 {drained_after} 包 "
                            f"{self.eth} -> {self.path}")
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

前置确认：`time` 已 import（capture.py 顶部既有）。

- [ ] **Step 2: stop() 超时分支补文件收尾（B06）**

定位 `stop()` 内 `if alive:` 块（约 373-378 行），替换为：

```python
        alive = self._thread is not None and self._thread.is_alive()
        if alive:
            logger.error(f"停止抓包线程超时(>{timeout}s)：{self.eth} -> {self.path}")
            # B06：线程未退出也必须收尾文件，否则 pcap 永久截断。
            # 竞态最坏损失=1 条记录（close 与写线程 write 瞬时交叉）；截断文件+明确失败信号
            # 优于永久无尾文件。
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

- [ ] **Step 3: tcpdump_stop 四态化（B02/B03）**

在 capture.py 模块级（`_captures = {}` 定义附近）加：

```python
# 最近一次 stop 的结果留存（B03：重复 stop 回放上轮真实结果，而非裸 true 掩盖 exitcode）
_last_stop_status = {}
```

`tcpdump_stop` 整个函数替换为：

```python
def tcpdump_stop(path="/home/tmp/tmp.pcap"):
    """停止抓包（独立子进程）。返回 dict 四态（v1.7.1，B02/B03）：
    正常停止 {"stopped":True,"exitcode":N,"pcap_complete":bool}
    从未 start {"stopped":False,"note":"no active capture for path"}
    重复 stop   上轮 _last_stop_status + note（回放真实结果）
    停止失败   {"stopped":False,"error":...}（实例保留可重试，既有行为）
    """
    with _captures_lock:
        entry = _captures.get(path)
    if entry is None:
        last = _last_stop_status.get(path)
        if last:
            return {**last, "note": "already stopped（重复 stop，回放上轮结果）"}
        return {"stopped": False, "note": "no active capture for path"}
    ok = _stop_capture_entry(entry)
    if not ok:
        status = {"path": path, "stopped": False,
                  "error": "停止未完成（停止线程超时），实例保留可重试"}
        _last_stop_status[path] = status
        logger.error(f"停止抓包未完成（子进程可能仍在运行），保留实例可重试：{path}")
        return status
    with _captures_lock:
        _captures.pop(path, None)
    # 子进程退出码反映 cap.stop()/sort 成败；非 0 则本轮 pcap 可能不完整/未排序
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

- [ ] **Step 4: handlers.py datatype 6 返回包裹直出**

定位 `elif datatype == 6:` 块（约 91-98 行）：

```python
    elif datatype == 6:
        data = json.loads(s=data)
        logger.info("停止tcpdump抓包，类型：%s，参数：%s" % (datatype, data))
        response = tcpdump_stop(**data)
        logger.info(response)
        res = json.dumps({"res": response}).encode("utf-8")
        return res
```

改为：

```python
    elif datatype == 6:
        data = json.loads(s=data)
        logger.info("停止tcpdump抓包，类型：%s，参数：%s" % (datatype, data))
        response = tcpdump_stop(**data)
        logger.info(response)
        # v1.7.1: tcpdump_stop 返回四态 dict，直出（旧 {"res": bool} 废弃，
        # 配套桥侧 mcp_socket_server 0.2.1 解析；中间态兼容见 spec §1.2）
        res = json.dumps(response, ensure_ascii=False).encode("utf-8")
        return res
```

同时检查本文件其他 `tcpdump_stop` 引用点（如 datatype 173 相关或其他编排）是否也有 `{"res": ...}` 包裹需同步——`grep -n "tcpdump_stop" socket_server/handlers.py` 逐个处理，同样改直出。

- [ ] **Step 5: py_compile + 本机 e2e（抓包→立即停→四态验证）**

```bash
cd /opt/socket_server
python3 -m py_compile socket_server/capture.py socket_server/handlers.py && echo COMPILE-OK
python3 -m socket_server serve -p 19000 > /tmp/srv_cap.log 2>&1 &
SRV=$!; sleep 2
python3 - <<'EOF'
import socket, struct, json, time, os

def req(dt, payload=b""):
    s = socket.create_connection(("127.0.0.1", 19000), timeout=20)
    body = struct.pack("i", dt) + payload
    s.sendall(struct.pack("i", len(body)) + body)
    time.sleep(1.0)
    s.settimeout(3)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    return data

PCAP = "/tmp/e2e_cap_b01.pcap"
if os.path.exists(PCAP): os.remove(PCAP)

# 1) stop 从未 start 的路径 → stopped:false + note（B02）
r = json.loads(req(6, json.dumps({"path": "/tmp/never_started_check.pcap"}).encode()))
print("never-started:", r)
assert r["stopped"] is False and "note" in r

# 2) start → 立即 stop（本机 lo 无流量，drain 快速完成）
r = json.loads(req(5, json.dumps({"eth": "lo", "path": PCAP}).encode()))
print("start:", r)
assert r["res"] is True, r
time.sleep(1)
t0 = time.time()
r = json.loads(req(6, json.dumps({"path": PCAP}).encode()))
dt = time.time() - t0
print(f"stop: {r} in {dt:.1f}s")
assert r["stopped"] is True, r
assert r["pcap_complete"] is True, r
assert dt < 8, f"stop took {dt}s"

# 3) 重复 stop → 回放上轮结果（B03）
r = json.loads(req(6, json.dumps({"path": PCAP}).encode()))
print("re-stop:", r)
assert r.get("stopped") is True and "note" in r, r
assert r["pcap_complete"] is True
print("ALL OK")
EOF
kill $SRV; rm -f /tmp/e2e_cap_b01.pcap
```

Expected: ALL OK（本机 lo 流量低，正常停止路径 <8s；B01 极端场景由 Task 6 真实靶机高流量验证）。

- [ ] **Step 6: test_e2e.py 追加四态段**

```python
# ===== Test: capture_stop 四态 =====
print("\n[Test] capture_stop 四态响应")
s = new_sock()
send_request(s, 6, json.dumps({"path": "/tmp/e2e_never.pcap"}).encode())
r = recv_text_response(s)
d = json.loads(r)
check("never-started stopped:false", d["stopped"] is False and "note" in d, r)
s.close()
```

- [ ] **Step 7: Commit**

```bash
cd /opt/socket_server
git add socket_server/capture.py socket_server/handlers.py test_e2e.py
git commit -m "fix: 抓包停止链路——drain 2s 上限+超时收尾文件+stop 四态留存(B01/B02/B03/B06)"
```

---

### Task 3: 时间戳修复 + HTTP Handler 超时 + CRLF 修复

**Files:**
- Modify: `/opt/socket_server/socket_server/capture.py`（`_decode_ts_ns` 尾部 fallback；降级 WARNING）
- Modify: `/opt/socket_server/socket_server/http_file.py`（`FileHandler.timeout` 类属性；`parse_multipart` 删两行剥除）

**Interfaces:**
- Produces: 时间戳 fallback=墙钟 `time.time_ns()`；`FileHandler.timeout = 60`；`parse_multipart` 无尾部剥除（内容字节保真）。Task 5 的 mcp 上传、Task 6 真实靶机验证消费。

- [ ] **Step 1: B05 时间戳兜底改墙钟**

定位 `capture.py` `_decode_ts_ns` 尾部（约 294-296 行）：

```python
      # userspace 模式 / hw_ts|sw_ts 但 cmsg 缺失或 ts 全零 -> 调用点取单调纳秒
      return time.monotonic_ns()
```

改为：

```python
      # userspace 模式 / hw_ts|sw_ts 但 cmsg 缺失或 ts 全零 -> 回退墙钟（B05：
      # monotonic 是开机秒数，按 Unix 纪元解释落盘=1970）。NTP step 窗口内保序
      # 不保证（kernel 时间戳同受影响，既有共性边界）；排序由 _sort_pcap_by_timestamp 兜底。
      logger.warning(f"cmsg 时间戳缺失，回退墙钟 time_ns：{self.eth} -> {self.path}")
      return time.time_ns()
```

（先 grep 该函数上下文确认 logger 可用——`capture.py` 模块级有 `logger = logging.getLogger(...)`，若有 self.eth 属性名差异以实际为准调整。）

- [ ] **Step 2: FileHandler.timeout = 60（I5：设 Handler 类属性，非 server 实例）**

定位 `http_file.py:100-103`：

```python
class FileHandler(BaseHTTPRequestHandler):
    """GET /files?path= 下载;POST /files multipart 上传。"""

    server_version = "socket_server-file/1.7"
```

改为：

```python
class FileHandler(BaseHTTPRequestHandler):
    """GET /files?path= 下载;POST /files multipart 上传。"""

    server_version = "socket_server-file/1.7"
    # 连接 socket 超时（BaseHTTPRequestHandler -> StreamRequestHandler.setup 里 settimeout）。
    # idle 语义：每次 recv/send 60s 静默才断——防慢客户端永久挂线程；大文件持续写不触发。
    # 注意必须设这里：ThreadingHTTPServer 实例的 timeout 只作用于 accept select 循环。
    timeout = 60
```

- [ ] **Step 3: CRLF 修复——删两行剥除（实测定案）**

定位 `http_file.py` `parse_multipart` 内（约 61-63 行）：

```python
        value = seg[header_end + 4:]
        # 去掉段尾的 \r\n(分隔行前的回车换行)
        if value.endswith(b"\r\n"):
            value = value[:-2]
```

改为（直接删除剥除）：

```python
        value = seg[header_end + 4:]
        # v1.7.1: 不剥尾部 CRLF——split(delim) 已消费定界串(含其前导 CRLF)，
        # 段值尾不存在定界 CRLF；内容自己的尾 CRLF 是内容(B05 实测:无条件剥会丢字节)
```

- [ ] **Step 4: 现有测试回归 + 新增 CRLF 三组用例**

先跑既有：`cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -q`——**预期 `test_parse_multipart_basic` 会 FAIL**（构造的 body 每段尾带 `\r\n`，删除剥除后 value 多出该 `\r\n`）——这说明**测试构造器 `_multipart_body` 与真实 curl 输出不一致**：真实 curl 的 body 段间分隔 `\r\n--boundary`，split 消费后段值确实不含尾部 CRLF。修测试构造器使其对齐 curl 真实形态：`_multipart_body` 里各段之间已有的 `\r\n--` 分隔保留（split 消费），**但上一版构造器的字段值后手动补的 `\r\n`（在 content 之后、`--boundary` 之前的那个）本来就是分隔符的一部分——检查构造器代码**，正确形态应为：

```python
def _multipart_body(path_val: str, filename: str, content: bytes, boundary: bytes) -> bytes:
    """构造标准 multipart/form-data body（与 curl -F 输出同构）。"""
    b = boundary
    return (b"--" + b + b"\r\n"
            b'Content-Disposition: form-data; name="path"\r\n\r\n' +
            path_val.encode() + b"\r\n"
            b"--" + b + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n" +
            content + b"\r\n"
            b"--" + b + b"--\r\n")
```

（即 content 后的 `\r\n` 保留——split 消费 `\r\n--boundary` 时会把这个 `\r\n` 当分隔符前导消费。实测验证：改完解析器后跑三组用例。）

追加三组 roundtrip 用例到 `tests/test_http_file.py`：

```python
def test_parse_multipart_crlf_tail_variants():
    # B05 验收:内容尾 CRLF 各形态全字节保真(修复前:尾1条/尾2条各丢1条)
    for label, content in [("no-tail", b"abc"), ("tail-1", b"abc\r\n"), ("tail-2", b"abc\r\n\r\n")]:
        body = _multipart_body("/tmp/t", "t.bin", content, b"B")
        path, got = parse_multipart("multipart/form-data; boundary=B", body)
        assert got == content, f"{label}: expect {content!r} got {got!r}"
```

- [ ] **Step 5: 跑全量 http_file 测试**

Run: `cd /opt/socket_server && python3 -m pytest tests/test_http_file.py -v`
Expected: 全 passed（含既有对抗用例 `binary_contains_boundary_substring`——其 content 不含尾 CRLF 形态变化，不受影响；若 `tail-2` 失败则说明 split 语义仍有边角，停下分析不要硬调）

- [ ] **Step 6: Commit**

```bash
cd /opt/socket_server
git add socket_server/capture.py socket_server/http_file.py tests/test_http_file.py
git commit -m "fix: 时间戳回退墙钟(B05) + FileHandler.timeout=60 + CRLF 剥除删除(字节保真)"
```

---

### Task 4: 版本号 1.7.1 + CHANGELOG + 打包

**Files:**
- Modify: `/opt/socket_server/socket_server/version.py`（`VERSION = "1.7.1"`）
- Modify: `/opt/socket_server/CHANGELOG.md`（顶部 1.7.1 段）
- Modify: `/opt/socket_server/packaging/socket_server.spec`（无新增模块，**无需改**——确认 http_file 已在）

- [ ] **Step 1: 版本号 + CHANGELOG**

`version.py`：`VERSION = "1.7.1"`

`CHANGELOG.md` 顶部加（保持 `## [x.y.z] - date` 格式）：

```markdown
## [1.7.1] - 2026-10-17

兼容 MCP 版本：mcp_socket_server 0.2.1+

### 修复
- 抓包停止链路：高流量下 stop 卡 10s 失败（drain 无时限）→ 2s 上限；停止超时仍收尾文件（不再永久截断）；stop 返回四态 JSON（stopped/exitcode/pcap_complete/note/error），重复 stop 回放上轮真实结果。**注意：1.7.1 的 capture_stop 响应需配 mcp_socket_server 0.2.1+，旧桥会误判失败**
- 抓包时间戳回退路径由开机秒数（落盘显示 1970 年）改为墙钟
- socket 监听新增 datatype 175（停止监听）；171 对"换端口"明确报错（不再静默忽略）
- 172/173 无监听时的错误从裸 b"error" 改为 JSON 文案
- HTTP 文件服务：Handler 连接空闲超时 60s；multipart 解析丢弃内容尾部 CRLF 的字节保真修复

### 兼容
- capture_stop 响应形态变更（见上）；datatype 175 为新增分支，旧客户端零影响
```

- [ ] **Step 2: release notes 重生成 + 编译**

```bash
cd /opt/socket_server
python3 packaging/generate_release_notes.py
python3 -m py_compile socket_server/handlers.py socket_server/capture.py socket_server/http_file.py socket_server/version.py
python3 -m pytest tests/test_http_file.py -q
```

Expected: 全通过。

- [ ] **Step 3: Commit**

```bash
cd /opt/socket_server
git add socket_server/version.py CHANGELOG.md
git commit -m "chore: 1.7.1 版本 + CHANGELOG"
```

---

### Task 5: 构建发版 1.7.1（构建→真实验证→release）

**Files:** 无代码改动；构建产物 + 验证 + GitHub release。

- [ ] **Step 1: 构建（CentOS 7 builder，按 CLAUDE.md 命令）**

构建后确认：`./dist/socket_server current` → 1.7.1；`ls -lh dist/socket_server` ≈16M ELF。

- [ ] **Step 2: 本机 serve 验证（175/抓包/HTTP/CRLF 全链）**

```bash
pkill -f "dist/socket_server serve" 2>/dev/null; sleep 1
./dist/socket_server serve -p 19000 > /tmp/srv171.log 2>&1 &
SRV=$!; sleep 2
# HTTP CRLF roundtrip（字节保真）
printf 'crlf-tail\r\n' > /tmp/crlf_tail.bin
curl -sf -F "path=/tmp/crlf_up.bin" -F "file=@/tmp/crlf_tail.bin" "http://127.0.0.1:9001/files"
cmp /tmp/crlf_tail.bin /tmp/crlf_up.bin && echo "CRLF roundtrip OK"
# 慢客户端 60s 超时（可选验证，跳过不阻塞——耗时 60s）
# 175 + 抓包（复用 Task 1/2 的 e2e 脚本改端口 19000）
kill $SRV; rm -f /tmp/crlf_*.bin
```

Expected: CRLF roundtrip OK；175/抓包段同 Task 1/2 步骤全过。

- [ ] **Step 3: 靶机 10.12.131.82 部署 1.7.1 + 高流量 B01 验证**

经 MCP `cmd_exec`（或 TCP 协议直传，同 1.7.0 部署路径）：

```bash
# 靶机侧切换后 systemctl restart，然后：
# 1) version_query 确认 1.7.1
# 2) B01 复现场景：回放灌包期间 start→立即 stop
#    capture_start(eth=靶机实网卡, path=/home/tmp/b01.pcap)
#    replay_run 或 tcpreplay 打流量 3 秒
#    capture_stop(同 path) —— 计时，期望 ≤5s 且返回 {"stopped":true,"pcap_complete":true}
# 3) capinfos /home/tmp/b01.pcap —— 无 "cut short"；首包时间为当前墙钟
# 4) socket_listen start(39012) → stop → ss 确认无监听 → 换端口 start(39013) → stop
```

判定标准：stop 耗时实测记录（≤5s 断言或以实测校准）；missing_tail=0（逐记录解析）；时间戳墙钟。**若 stop 超时阈值不够（>5s），回 Task 2 调 drain deadline 后重验。**

- [ ] **Step 4: 提交 + tag + push + release**

```bash
cd /opt/socket_server
git push origin main
git tag -a v1.7.1 -m "1.7.1: 监听停止(175)/抓包停止链路修复/时间戳墙钟/HTTP超时/CRLF"
git push origin v1.7.1
sha256sum dist/socket_server | tee dist/socket_server.sha256
python3 -c "from socket_server.release_notes import RELEASE_NOTES; print(RELEASE_NOTES.strip())" > /tmp/rn_171.md
HTTPS_PROXY=http://10.12.186.204:7897 gh release create v1.7.1 -R weihang1258/socket_server --title "v1.7.1" --notes-file /tmp/rn_171.md dist/socket_server dist/socket_server.sha256
HTTPS_PROXY=http://10.12.186.204:7897 gh release view v1.7.1 -R weihang1258/socket_server --json tagName,assets
```

（gh 被分类器连续拒绝时：按 CLAUDE.md 预案请用户真实终端手动跑，去掉开头 `!`。）

---

## Part B：mcp_socket_server 0.2.1

### Task 6: registry liveness 列 + client version() 恢复 + socket_client 适配（capture_stop 四态/stop 动作/force_* 省略）

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/registry.py`（liveness 列迁移 + get/set 方法）
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/socket_client.py`（恢复 `version()`；`capture_stop` 四态解析；`socket_listen` 加 stop；`boce_run` payload 字段确认）
- Modify: `/opt/mcp_socket_server/tests/mock_socket_server.py`（175 handler、capture stop 新 JSON 形态开关、171 端口不一致）
- Modify: `/opt/mcp_socket_server/tests/test_socket_client.py`

**Interfaces:**
- Produces:
  - `registry.get_listen_liveness(host: str, port: int = 9000) -> bool`；`registry.set_listen_liveness(host: str, alive: bool, port: int = 9000) -> None`（Task 7 消费）
  - `socket_client.version() -> str`（恢复；内部 json.loads；仅 socket_listen start 用，不加 MCP 工具）
  - `socket_client.capture_stop(path) -> dict`（四态，见 Step 2 分支）
  - `socket_client.socket_listen("stop")`（→175）
  - `replay_run` 的 force_sport/force_dport=0 → wire 省略（改动：现实现 `if force_sport is not None: payload[...]`——0 会发上 wire；改为 `if force_sport:`）

- [ ] **Step 1: 写失败测试（registry liveness + client version/capture_stop/socket_listen stop/force_* 省略）**

`tests/test_registry.py` 追加：

```python
def test_listen_liveness(tmp_path):
    from mcp_socket_server.registry import TargetRegistry
    reg = TargetRegistry(str(tmp_path / "r.db"))
    assert reg.get_listen_liveness("10.0.0.1") is False
    reg.set_listen_liveness("10.0.0.1", True)
    assert reg.get_listen_liveness("10.0.0.1") is True
    reg.set_listen_liveness("10.0.0.1", False)
    assert reg.get_listen_liveness("10.0.0.1") is False
```

`tests/test_socket_client.py` 追加：

```python
def test_version_method_restored(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        v = c.version()
        assert isinstance(v, str) and v, v   # mock 返回 "1.3.9-mock"（json 字符串）
    finally:
        c.close()


def test_capture_stop_four_states(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    # mock 需要 capture_stop_mode 开关: "new_ok"/"new_never"/"new_fail"/"legacy_ok"/"legacy_fail"
    # (实现见 Step 3 mock 扩展)默认 "new_ok"
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        d = c.capture_stop("/tmp/a.pcap")
        assert d["stopped"] is True and d["pcap_complete"] is True, d
        mock_server.capture_stop_mode = "new_never"
        d = c.capture_stop("/tmp/b.pcap")
        assert d["stopped"] is False and "note" in d, d
        mock_server.capture_stop_mode = "new_fail"
        d = c.capture_stop("/tmp/c.pcap")
        assert d["stopped"] is False and "error" in d, d
        mock_server.capture_stop_mode = "legacy_fail"
        d = c.capture_stop("/tmp/d.pcap")
        assert d["stopped"] is False and "legacy" in d, d
    finally:
        c.close()
        mock_server.capture_stop_mode = "new_ok"


def test_socket_listen_stop_action(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        assert c.socket_listen("stop") == b"ok"
        mock_server.listen_mode = "error"   # 175 error 形态复用
        try:
            c.socket_listen("stop")
            assert False, "should raise"
        except ConnectionError:
            pass
    finally:
        c.close()
        mock_server.listen_mode = "ok"


def test_replay_force_port_zero_omitted(mock_server):
    from mcp_socket_server.socket_client import SocketServerClient
    c = SocketServerClient(mock_server.host, mock_server.port)
    c.connect()
    try:
        # mock 的 datatype 0 handler 需记录最近 payload（Step 3 加 captured_payload）
        c.replay_run(["/tmp/a.pcap"], "eth0", "eth1", force_sport=0, force_dport=0)
        payload = json.loads(mock_server.captured_payload)
        assert "force_sport" not in payload and "force_dport" not in payload, payload
        c.replay_run(["/tmp/a.pcap"], "eth0", "eth1", force_sport=12345)
        payload = json.loads(mock_server.captured_payload)
        assert payload.get("force_sport") == 12345, payload
    finally:
        c.close()
```

（`import json` 若文件头未有则加。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_registry.py::test_listen_liveness tests/test_socket_client.py -v -k "version_method or four_states or stop_action or force_port_zero"`
Expected: FAIL（AttributeError: get_listen_liveness / version / 旧 capture_stop 返回形态 / stop 未实现 / force 0 仍上 wire）

- [ ] **Step 3: 实现——registry liveness**

`registry.py`：`__init__` 建表语句后加迁移：

```python
        # v0.2.1: listen liveness 标记（B16：有活跃监听的靶机 remove_target 拒绝）
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(targets)").fetchall()]
        if "listen_liveness" not in cols:
            self._conn.execute("ALTER TABLE targets ADD COLUMN listen_liveness INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
```

类内加方法：

```python
    def set_listen_liveness(self, host: str, alive: bool, port: int = 9000) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE targets SET listen_liveness=? WHERE host=? AND port=?",
                (1 if alive else 0, host, port))
            self._conn.commit()

    def get_listen_liveness(self, host: str, port: int = 9000) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT listen_liveness FROM targets WHERE host=? AND port=?",
                (host, port)).fetchone()
            return bool(row and row[0])
```

- [ ] **Step 4: 实现——socket_client 三处**

(a) 恢复 `version()`（放在 `isfile` 方法前）：

```python
    def version(self) -> str:
        """datatype 14。v0.2.1 恢复（仅 socket_listen start 内部探测靶机版本用，
        不暴露 MCP 工具）。服务端返回 json.dumps(VERSION) 带引号 JSON 字符串。"""
        self._send(14)
        return json.loads(self.recv_text_response().decode("utf-8", "replace"))
```

(b) `capture_stop` 整方法替换为四态解析：

```python
    def capture_stop(self, path: str) -> dict:
        """datatype 6。1.7.1 四态 JSON：stopped/exitcode/pcap_complete/note/error；
        旧靶机 {"res": bool} 转 legacy 形态（res:false=真失败，映射 error）。"""
        self._send(6, json.dumps({"path": path}).encode())
        data = json.loads(self.recv_text_response().decode("utf-8", "replace"))
        if isinstance(data, dict) and "stopped" in data:
            return data                      # 新形态（1.7.1）
        if isinstance(data, dict) and "res" in data:
            if data["res"]:
                return {"stopped": True, "legacy": True}
            return {"stopped": False, "legacy": True,
                    "error": "停止未完成（旧版语义），可重试"}
        return {"stopped": False, "error": f"无法解析的服务端响应: {data!r}"}
```

（先看现 capture_stop 实现的方法签名与 `_send(6, ...)` payload 形态——`{"path": path}` 保持。）

(c) `socket_listen` 加 stop 分支（在 clean 分支后）：

```python
        if action == "stop":
            self._send(175)
            resp = self.recv_ok()
            if resp != b"ok":
                raise ConnectionError(f"listen stop failed: {resp!r}")
            return resp
```

(d) `replay_run` 的 force_sport/force_dport 改 truthy 判断（force_ip_src/force_ip_dst 保持 `is not None`——空串场景不同）：

```python
        if force_sport:
            payload["force_sport"] = force_sport
        if force_dport:
            payload["force_dport"] = force_dport
```

- [ ] **Step 5: 实现——mock 扩展**

`tests/mock_socket_server.py`：
- `MockSocketServer` 类属性加 `capture_stop_mode = "new_ok"`、`captured_payload = None`；`__setattr__` 胶水照 listen_mode 模式
- `_on_data`：
  - datatype 6 分支按 `self.capture_stop_mode` 返回：

```python
        if datatype == 6:
            mode = self.capture_stop_mode
            if mode == "new_ok":
                return json.dumps({"path": json.loads(data)["path"], "stopped": True,
                                   "exitcode": 0, "pcap_complete": True}).encode()
            if mode == "new_never":
                return json.dumps({"stopped": False,
                                   "note": "no active capture for path"}).encode()
            if mode == "new_fail":
                return json.dumps({"stopped": False,
                                   "error": "停止未完成（停止线程超时），实例保留可重试"}).encode()
            if mode == "legacy_ok":
                return json.dumps({"res": True}).encode()
            return json.dumps({"res": False}).encode()   # legacy_fail
```

  - datatype 175 分支：`listen_mode == "error"` 时返回 `json.dumps({"error": "未启动监听"}).encode()`，否则 `b"ok"`
  - datatype 171：`self.listen_port_requested` 记录——端口不一致 error 用例：加类属性 `listen_active_port = None`（None=未监听）；`_on_data` 171 分支：`if self.listen_active_port and json.loads(data).get("port") != self.listen_active_port: return json.dumps({"error": "已有监听运行在端口 %d，请先停止(datatype 175)" % self.listen_active_port}).encode()`；正常返回 `{"pid": ["12345"]}` 并记 `self.listen_active_port = port`
  - datatype 0 分支开头加 `self.captured_payload = data.decode("utf-8", "replace")`

- [ ] **Step 6: 跑测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_registry.py tests/test_socket_client.py -q`
Expected: 全 passed

- [ ] **Step 7: Commit**

```bash
cd /opt/mcp_socket_server
git add src/mcp_socket_server/registry.py src/mcp_socket_server/socket_client.py tests/mock_socket_server.py tests/test_socket_client.py tests/test_registry.py
git commit -m "feat: registry liveness列+client version恢复+capture_stop四态解析+listen stop+force_*省略"
```

---

### Task 7: server.py——capture_stop 工具四态判定 + socket_listen stop/liveness + B23 + B04 + B13/B18 + timeout 参数

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/server.py`（多处）
- Modify: `/opt/mcp_socket_server/tests/test_write_tools.py` / `tests/test_server_read_tools.py`

**Interfaces:**
- Consumes: Task 6 的 registry liveness 方法、client 四态 capture_stop、client socket_listen stop、client version()
- Produces: 工具层最终行为（12 工具、9 个带 timeout、capture_stop 四态判定、remove_target liveness 拒绝、results 带 target）

- [ ] **Step 1: 写失败测试（B23/B04/B13/B18/B16/remove liveness/capture_stop 四态工具层）**

`tests/test_write_tools.py` 追加：

```python
def test_capture_start_false_becomes_failed(app, mock_server):
    # B23: mock 5 返回 {"res": true}；临时改 mock 让 datatype 5 返回 false——
    # 用 capture_stop_mode 不行，需 mock 5 分支可控：加 capture_start_mode 开关（Task 内 Step 3 mock 扩展）
    mock_server.capture_start_mode = "fail"
    res = capture_start([mock_server.host], path="/tmp/x.pcap", port=mock_server.port)
    mock_server.capture_start_mode = "ok"
    assert res["ok"] == 0, res
    assert any("False" in f["reason"] or "业务失败" in f["reason"] for f in res["failed"]), res


def test_capture_start_failure_releases_capture_lock(app, mock_server):
    # B04: start 失败后同 path 的 CAPTURE 锁应已释放——再次 start 不应锁冲突而应是正常流程
    mock_server.capture_start_mode = "fail"
    r1 = capture_start([mock_server.host], path="/tmp/lockx.pcap", port=mock_server.port)
    mock_server.capture_start_mode = "ok"
    assert r1["ok"] == 0
    lm = get_lock_manager()
    r2 = cmd_exec([mock_server.host], "echo hi", port=mock_server.port)   # 若锁泄漏会 shell 冲突
    assert r2["ok"] == 1, r2


def test_capture_stop_four_state_tool(app, mock_server):
    mock_server.capture_stop_mode = "new_fail"
    r = capture_stop([mock_server.host], path="/tmp/f1.pcap", port=mock_server.port)
    mock_server.capture_stop_mode = "new_ok"
    assert r["ok"] == 0 and "停止未完成" in r["failed"][0]["reason"], r

    mock_server.capture_stop_mode = "new_never"
    r = capture_stop([mock_server.host], path="/tmp/f2.pcap", port=mock_server.port)
    assert r["ok"] == 1, r    # 未在抓=非失败
    assert r["results"][0]["stopped"] is False, r


def test_results_carry_target(app, mock_server):
    # B13: 走 _run_batch 的工具 results 带 target
    res = cmd_exec([mock_server.host], "echo hi", port=mock_server.port)
    assert res["results"][0].get("target") == mock_server.host, res
    assert res["results"][0].get("stdout") == "mock_out", res


def test_cmd_exec_wait_false_note(app, mock_server):
    # B18: wait=False -> note 而非 null
    res = cmd_exec([mock_server.host], "echo hi", port=mock_server.port, wait=False)
    assert res["ok"] == 1, res
    assert res["results"][0].get("note") == "wait=false 无返回值", res


def test_remove_target_with_listener_rejected(app, mock_server):
    # B16: start 登记 liveness -> remove 拒绝 -> stop 后可删
    add_target(mock_server.host, mock_server.port, tags=["t16"])
    socket_listen([mock_server.host], "start", listen_port=39021, port=mock_server.port)
    r = remove_target(mock_server.host, mock_server.port)
    assert r["ok"] is False and "监听" in r["error"], r
    socket_listen([mock_server.host], "stop", port=mock_server.port)
    r = remove_target(mock_server.host, mock_server.port)
    assert r["ok"] is True, r
```

（import 区补 `remove_target`；`import time` 不需要。）

`tests/test_server_read_tools.py` 追加 timeout 参数存在性：

```python
def test_isfile_accepts_timeout(app, mock_server):
    res = isfile([mock_server.host], "/etc/hostname", port=mock_server.port, timeout=10)
    assert res["ok"] == 1, res
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/test_write_tools.py tests/test_server_read_tools.py -q`
Expected: 新增用例 FAIL（capture_start_mode 属性缺失→AttributeError；results 无 target 键；note 缺失；remove 未拒绝）

- [ ] **Step 3: mock 扩展 capture_start_mode**

`tests/mock_socket_server.py`：类属性 `capture_start_mode = "ok"` + `__setattr__` 胶水（同 capture_stop_mode）；`_on_data` datatype 5 分支：

```python
        if datatype == 5:
            if getattr(self, "capture_start_mode", "ok") == "fail":
                return json.dumps({"res": False}).encode()
            return json.dumps({"res": True}).encode()
```

- [ ] **Step 4: server.py 实现——capture_stop 工具四态 + socket_listen stop/liveness + B23/B04/B13/B18**

(a) `_run_batch`（B23+B04+B13+B18 四合一，整块 run_one 与出口改造）：

```python
    def run_one(host_port: str) -> TargetResult:
        t0 = time.monotonic()
        lock_mgr = _lock_manager
        key = ()
        if lock_key_fn:
            key = lock_key_fn(host_port)
        ok = True
        # 1. 锁
        if lock_mgr is not None and lock_class != LockClass.NONE:
            try:
                lock_mgr.acquire(host_port, lock_class, key)
                logger.debug(f"[{host_port}] lock acquired: {lock_class.value} key={key} "
                             f"({(time.monotonic()-t0)*1000:.0f}ms)")
            except LockConflict as e:
                logger.debug(f"[{host_port}] lock CONFLICT: {e}")
                return TargetResult(target=host_port, ok=False, error=str(e))
        # 2. 池借出
        pool = sched.get_pool(host_port, port)
        t_pool = time.monotonic()
        try:
            client = pool.acquire(socket_timeout=int(timeout))
            logger.debug(f"[{host_port}] pool acquire ({(time.monotonic()-t_pool)*1000:.0f}ms) "
                         f"sock_timeout={timeout}s")
        except Exception as e:
            logger.debug(f"[{host_port}] pool acquire FAILED: {e!r}")
            ok = False
            if lock_mgr is not None and lock_class != LockClass.NONE:
                lock_mgr.release(host_port, lock_class, key)
            return TargetResult(target=host_port, ok=False, error=f"pool: {e}")
        # 3. 执行
        healthy = True
        try:
            t_fn = time.monotonic()
            data = fn(client)
            if data is None:
                data = {"note": "wait=false 无返回值"}   # B18
            elif data is False:
                ok = False                                # B23/B04
                return TargetResult(target=host_port, ok=False,
                                    error="agent 返回 False（业务失败）")
            logger.debug(f"[{host_port}] fn done ({(time.monotonic()-t_fn)*1000:.0f}ms) "
                         f"data_type={type(data).__name__}")
            return TargetResult(target=host_port, ok=True, data=data)
        except Exception as e:
            healthy = False
            ok = False
            logger.debug(f"[{host_port}] fn FAILED ({(time.monotonic()-t_fn)*1000:.0f}ms): {e!r}")
            return TargetResult(target=host_port, ok=False, error=str(e))
        finally:
            pool.release(client, healthy)
            # B04: CAPTURE 长锁仅在本次失败时立即释放；成功时保留给 stop 释放
            if lock_mgr is not None and lock_class != LockClass.NONE:
                if lock_class != LockClass.CAPTURE or not ok:
                    try:
                        lock_mgr.release(host_port, lock_class, key)
                    except Exception:
                        pass
```

出口（`_run_batch` return 处，B13）：

```python
    def _wrap(r: TargetResult):
        if isinstance(r.data, dict):
            return {"target": r.target, **r.data}
        return {"target": r.target, "result": r.data}

    return {"ok": sum(1 for r in results if r.ok),
            "failed": _failed(results),
            "results": [_wrap(r) for r in results if r.ok]}
```

注意：`TargetResult.ok` 字段被 B23 改路径使用——确认 dataclass 定义有 ok 字段（有，`pool.py` TargetResult(target, ok, data, error)）。

(b) `capture_stop` 工具（自有 executor 版本）改造为四态判定——现有实现是独立 `run_one`（不走 `_run_batch`），同步 B13/B18 语义：其 run_one 返回 `ok = client.capture_stop(path)` 处改为：

```python
        def run_one(host_port: str) -> TargetResult:
            pool = sched.get_pool(host_port, port)
            client = pool.acquire(socket_timeout=30)
            healthy = True
            try:
                d = client.capture_stop(path)
                # 四态判定（spec §2.1）：
                #   stopped:True + pcap_complete:False -> failed（pcap 可能不完整）
                #   stopped:False + error -> failed（可重试）
                #   stopped:False + note -> 非失败（幂等明示）
                if d.get("stopped") and d.get("pcap_complete") is False:
                    return TargetResult(target=host_port, ok=False,
                                        error=f"pcap 可能不完整/未排序(exitcode={d.get('exitcode')})")
                if d.get("stopped") is False and d.get("error"):
                    return TargetResult(target=host_port, ok=False, error=d["error"])
                return TargetResult(target=host_port, ok=True, data=d)
            except Exception as e:
                healthy = False
                return TargetResult(target=host_port, ok=False, error=str(e))
            finally:
                pool.release(client, healthy)
```

出口 results 同样包 target（`{"target": r.target, **r.data}`）。锁语义不变（finally 无条件释放 CAPTURE——stop 就是释放者）。

(c) `socket_listen` 工具：action 合法集合加 `"stop"`；stop/clean/save 分支成功后若 start/stop 动作涉及 liveness：

```python
        # run_one 内 start 成功分支后（仅 start）：
        if action == "start":
            result = client.socket_listen("start", listen_port=listen_port)
            # B16: 1.7.1+ 靶机登记 liveness（旧靶机无法 stop，登记会造成永久不可删）
            try:
                v = client.version()
                parts = [int(x) for x in v.split(".")]
                if tuple(parts[:3]) >= (1, 7, 1) and _registry is not None:
                    _registry.set_listen_liveness(host_port.split(":")[0], True, port)
            except Exception:
                logger.warning(f"liveness 登记失败（不影响 start）: {host_port}")
            return result
        if action == "stop":
            r = client.socket_listen("stop")
            if _registry is not None:
                _registry.set_listen_liveness(host_port.split(":")[0], False, port)
            return r
```

`remove_target` 工具加 liveness 检查（在现有 has_active 之后）：

```python
    if _registry is not None and _registry.get_listen_liveness(host, port):
        return {"ok": False, "error": f"靶机 {host} 有活跃监听，请先 socket_listen action=stop"}
```

(d) 9 工具加 `timeout` 形参（signature 追加 + `_run_batch`/`pool.acquire` 传参）。逐工具改动（默认值=现硬编码值）：

```python
# isfile / isdir（217/232 行）：签名加 timeout: int = 15，
#   batch(..., timeout=timeout)
# pcap_flow_extract（243）：timeout: int = 30
# cmd_exec（258）：timeout: int = 60，_run_batch(..., timeout=timeout if wait else min(timeout, 15))
# capture_start（285）/ capture_stop（305）：timeout: int = 30
# boce_run（359）：签名 timeout: int = 60；原业务参数 timeout: int = 3 改名 http_timeout: int = 3，
#   client.boce_run(..., timeout=http_timeout ...) —— payload 字段名仍 "timeout"（client 内已如此）
# replay_run（380）：timeout: int = 120
# socket_listen（409）：timeout: int = 30
```

每个工具 docstring 不变（timeout 语义由 schema description 表达——FastMCP 用 `Annotated[int, Field(description=...)]`：

```python
ToolTimeout = Annotated[int, Field(description="单靶机操作超时秒数(connect+执行+返回),多靶机并行各自计时")]
```

签名写 `timeout: ToolTimeout = <默认值>`。）

(e) `add_target` 探活（B19）：

```python
@mcp.tool()
def add_target(host: str, port: int = 9000, tags: list[str] | None = None,
               note: str = "", description: CallDescription = "",
               force: bool = False) -> dict:
    """注册新靶机。tags 用于 @tag 选机。注册前 TCP 探活（不通拒绝，force=True 跳过）。"""
    import socket as _socket
    host_clean = host.strip("[]")   # IPv6 方括号剥除
    if not force:
        try:
            _socket.create_connection((host_clean, port), timeout=3).close()
        except OSError as e:
            return {"ok": False,
                    "error": f"{host}:{port} 不可达(TCP connect 失败: {e})，未注册；force=True 可强制注册"}
    if _registry is None:
        return {"ok": False, "error": "注册表未初始化"}
    return _registry.add_target(host_clean, port, tags, note)
```

(f) 描述同步（S01/S04/S05/S06/S07/B14/B15 描述完善——逐工具 docstring 修改，关键内容）：
- 所有业务工具 targets 参数：`Annotated[list[str], Field(description="靶机列表(裸IP/host:port/@tag 标签)")]`
- `boce_run` docstring 加：`mode 仅"封堵/重定向"有判定实现；"弹窗"未实现（恒失败）；其他值按未知模式恒失败`
- `replay_run` docstring 加：`单臂回放（uplink_iface == downlink_iface 且无 VLAN 差异）时所有包计为 uplink，downlink/flow_count 恒 0 属正常，需方向统计用双接口`
- `capture_start` docstring 改：`AF_PACKET 嗅探抓包（独立子进程；ps 中无 tcpdump 子进程属正常）；默认 path 全局共用，多任务请显式传不同 path`
- `capture_stop` docstring 改：`返回四态：stopped/exitcode/pcap_complete；pcap_complete=false 表示 pcap 可能不完整；未在抓的路径返回 stopped=false（幂等，非错误）`
- `socket_listen` docstring：删除"配合回放做对比测试"，改为 `接收发往该端口的 TCP/UDP 流量（TCP 监听不接收无握手的裸回放流，回放对比需回放侧重建会话或用 UDP）；action 加 stop=停止当前监听；更换 listen_port 先 stop 再 start；fetch/save 须在 stop 之前`
- `pcap_flow_extract` docstring 加：`路径不存在/非目录返回空数组；protoType: 1=TCP 2=UDP 4=ICMP`
- `cmd_exec` docstring 加：`白名单为空=不限制（默认）；wait=false 无返回值`
- S03 annotations：`@mcp.tool(annotations={"readOnlyHint": True})` 标 isfile/isdir/pcap_flow_extract/list_targets；`@mcp.tool(annotations={"destructiveHint": True})` 标 cmd_exec/capture_stop/remove_target；其余 5 个不标
- S09：`pcap_flow_extract` 工具内 flows 超 1000 条截断：`{"target":.., "flows": flows[:1000], "total": len(flows), "truncated": len(flows) > 1000}`；`cmd_exec` client 层 stdout 截断在 Task 6 的 socket_client（若加在 server 层则此处：stdout >64KB 时 `stdout=stdout[:65536]` + `stdout_truncated: true`——放 `socket_client.cmd_exec` 收到 gzip 结果后）

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q`
Expected: 全 passed（既有用例因 results 带 target 键可能需微调断言——如 `results[0]["stdout"]` 仍在，仅多 target 键，不该破；capture_start 形态 `{"target":..,"result": true}` 断言更新）

- [ ] **Step 6: Commit**

```bash
cd /opt/mcp_socket_server
git add src/mcp_socket_server/server.py tests/
git commit -m "feat: capture_stop四态判定+socket_listen stop/liveness+B23/B04/B13/B18/B19+timeout参数+schema完善"
```

---

### Task 8: default_timeout config + 描述断言 + 版本 0.2.1

**Files:**
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/config.py`（DEFAULTS 加 `default_timeout`；Config dataclass 加字段）
- Modify: `/opt/mcp_socket_server/src/mcp_socket_server/socket_client.py`（DEFAULT_TIMEOUT 读 env/config）
- Modify: `/opt/mcp_socket_server/config.example.yaml`（default_timeout + cmd_exec_whitelist 样例注释）
- Modify: `/opt/mcp_socket_server/pyproject.toml`（version 0.2.1）
- Modify: `/opt/mcp_socket_server/README.md`（如有工具清单章节同步）

**Interfaces:**
- Produces: `DEFAULT_TIMEOUT` 可配（env `MCP_DEFAULT_TIMEOUT` > config `default_timeout` > 30）

- [ ] **Step 1: config.py + socket_client.py**

`config.py` DEFAULTS 加一行：`"default_timeout": 30,`；`Config` dataclass 加 `default_timeout: int`；`load_config` return 处加 `default_timeout=data.get("default_timeout", 30),`。

`socket_client.py` 顶部：

```python
DEFAULT_TIMEOUT = int(os.environ.get("MCP_DEFAULT_TIMEOUT", "30"))
```

（os 已 import？确认——socket_client 顶部有 `import os` 无则加。config.yaml 的 default_timeout 供部署参考与后续动态读取，env 优先足够本批落地。）

`config.example.yaml` 追加：

```yaml
# 单靶机 socket 操作默认超时秒数（工具级 timeout 参数未传时的底层兜底；
# 也可用环境变量 MCP_DEFAULT_TIMEOUT 覆盖）
# default_timeout: 30

# cmd_exec 白名单（前缀匹配；空列表=不限制——默认不限制）
# cmd_exec_whitelist: ["iptables ", "tcpdump ", "ss "]
```

- [ ] **Step 2: pyproject 0.2.1 + README**

version = "0.2.1"；README 工具说明区（若维护）同步 capture_stop 四态/socket_listen stop/timeout 参数。

- [ ] **Step 3: 全量回归 + ListTools 校验**

```bash
cd /opt/mcp_socket_server && venv/bin/python -m pytest tests/ -q
```

Expected: 全 passed。

```bash
cd /opt/mcp_socket_server && venv/bin/python - <<'EOF'
import asyncio
from mcp_socket_server.server import mcp

async def main():
    tools = await mcp.list_tools()
    by = {t.name: t for t in tools.tools}
    assert len(tools.tools) == 12, len(tools.tools)
    # 9 业务工具带 timeout 且默认值正确
    expect = {"isfile": 15, "isdir": 15, "pcap_flow_extract": 30, "cmd_exec": 60,
              "capture_start": 30, "capture_stop": 30, "boce_run": 60,
              "replay_run": 120, "socket_listen": 30}
    for name, dv in expect.items():
        props = by[name].inputSchema["properties"]
        assert "timeout" in props and props["timeout"].get("default") == dv, (name, props.get("timeout"))
    # boce_run 无业务 timeout 参数（改名 http_timeout）
    assert "timeout" not in by["boce_run"].inputSchema["properties"] or \
           by["boce_run"].inputSchema["properties"]["timeout"].get("default") == 60
    assert "http_timeout" in by["boce_run"].inputSchema["properties"]
    # socket_listen action 有 stop
    desc = by["socket_listen"].description or ""
    assert "stop" in desc, desc
    # 描述断言防回退
    assert "AF_PACKET" in (by["capture_start"].description or ""), "S06②"
    assert "配合回放" not in (by["socket_listen"].description or ""), "B15"
    assert "弹窗" in (by["boce_run"].description or ""), "S05"
    assert "单臂" in (by["replay_run"].description or ""), "B14"
    # annotations
    assert by["isfile"].annotations and by["isfile"].annotations.readOnlyHint is True
    assert by["cmd_exec"].annotations and by["cmd_exec"].annotations.destructiveHint is True
    print("0.2.1 ListTools 全量校验 OK")

asyncio.run(main())
EOF
```

Expected: `0.2.1 ListTools 全量校验 OK`。

- [ ] **Step 4: 真实靶机端到端（需 Part A Task 5 完成——靶机已 1.7.1）**

```bash
cd /opt/mcp_socket_server && venv/bin/python - <<'EOF'
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8080/mcp"   # 生产；或起临时实例 :18080
T = ["10.12.131.82"]

async def call(s, name, args):
    r = await s.call_tool(name, args)
    return json.loads(r.content[0].text)

async def main():
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # capture_stop 四态走真实靶机
            r = await call(s, "capture_stop", {"targets": T, "path": "/tmp/never_mind.pcap"})
            print("never:", r)
            # socket_listen start -> stop -> remove 保护
            r = await call(s, "socket_listen", {"targets": T, "action": "start", "listen_port": 39031})
            print("start:", r)
            r = await call(s, "socket_listen", {"targets": T, "action": "stop"})
            print("stop:", r)
            # timeout 参数
            r = await call(s, "isfile", {"targets": T, "path": "/etc/hostname", "timeout": 10})
            print("isfile timeout:", r)
            assert r["ok"] == 1

asyncio.run(main())
EOF
```

Expected: 全部 ok；stop 后 liveness 清除。

- [ ] **Step 5: 重启生产 + push**

```bash
# 确认无进行中调用后
systemctl restart mcp-socket-server && sleep 3 && systemctl is-active mcp-socket-server
cd /opt/mcp_socket_server
git add -A src/ tests/ config.example.yaml pyproject.toml README.md
git commit -m "chore: 0.2.1 版本 + default_timeout + 文档同步"   # 若 Task 6/7 已各自提交，本步只提交增量
HTTPS_PROXY=http://10.12.186.204:7897 git push origin main
```

---

## 依赖关系与执行顺序

```
Part A (socket_server 1.7.1):  Task 1 → Task 2 → Task 3 → Task 4 → Task 5(构建+真实验证+发版)
Part B (mcp 0.2.1):            Task 6 → Task 7 → Task 8
跨部依赖:                      Task 8 Step 4（真实靶机 e2e）需 Task 5 完成
```

两 Part 可并行开发（不同仓库）；Task 8 的真实靶机环节前不依赖 Part A。
