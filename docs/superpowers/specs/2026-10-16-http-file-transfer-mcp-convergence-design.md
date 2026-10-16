# HTTP 文件传输 + MCP 工具收敛 设计文档

- 日期：2026-10-16
- 状态：已评审（用户逐节确认 + review 修订 3 处）
- 涉及仓库：`socket_server`（1.7.0）、`mcp_socket_server`（0.2.0）

## 0. 背景与目标

1. **文件传输体验**：现 MCP `file_upload`/`file_download` 把文件落在中央节点 `downloads/`，LLM/用户不知道文件在哪、拿不走（死数据）。
2. **工具收敛**：17 个 MCP 工具中 5 个（version_query/version_detail/filesize/command_exists/routeinfo）经审计（3 个月 audit_log 2368 次 + 靶机 journalctl datatype 统计 1283 次）确认无业务价值，删除；另 2 个（file_upload/file_download）被 HTTP 文件服务替代（见目标 1），共删 7 个。
3. **补齐自动化核心**：pcap 回放（datatype 0）与 TCP 监听（171-174）是 DPI 自动化测试核心，纳入 MCP。
4. 维护性：两仓独立发版节奏不变；socket_server 协议 datatype 零新增零修改。

## 1. socket_server：HTTP 文件服务（1.7.0）

### 1.1 模块与启动

- 新模块 `socket_server/http_file.py`，标准库 `http.server.ThreadingHTTPServer`，零新依赖。
- `serve` 启动序列：**先起 HTTP daemon 线程（:9001 双栈），再调 `start_tcp_server`（:9000，阻塞主线程）**——TCP 的 IPv6 serve_forever 永不返回，HTTP 必须在其之前就绪；HTTP 启动失败只告警，不影响 TCP 主服务。
- **双栈监听**（与 `server.py` TCP 实现逐行同构）：IPv4 `('0.0.0.0', port)` + IPv6 `AF_INET6 + IPV6_V6ONLY=1` 绑 `('::', port)`；IPv6 起不来降级仅 IPv4 并告警。

### 1.2 配置

`/opt/socket/config`（key=value）新增：

```
http_port=9001
```

- 默认 9001（config 无此行时开启）；`http_port=0` 显式关闭。
- 端口被占：告警降级/放弃，不影响 TCP。

### 1.3 接口定义（权威，MCP 描述同步）

```
GET  http://<host>:9001/files?path=<远端绝对路径>
  → 200 文件字节流 (Content-Type: application/octet-stream,
     Content-Disposition: attachment; filename="<basename>")
  → 404 文件不存在 / 400 缺 path

POST http://<host>:9001/files        (multipart/form-data)
  字段: path = 远端落盘绝对路径（文本字段）
        file = 文件内容（二进制字段）
  → 200 {"ok": true, "path": "...", "size": N}
  → 400 缺 path/file / 500 写盘失败
```

- IPv6 URL 形态：`http://[fd00::82]:9001/files?path=...`（方括号）。
- query 解码约定：服务端用 `urllib.parse.parse_qs` + UTF-8 解码 `path` 参数（curl 会对特殊字符自动百分号编码）；多值取第一个。
- curl 示例：
  - 下载 `curl -o out.pcap "http://10.12.131.82:9001/files?path=/home/tmp/tmp.pcap"`
  - 上传 `curl -F "path=/home/tmp/deploy.zip" -F "file=@deploy.zip" http://10.12.131.82:9001/files`

### 1.4 安全与实现要点

- 路径 `realpath` 归一化；**全盘可访问**（与 TCP 9000 文件接口同级安全模型，仅内网）。
- 上传父目录不存在自动 `makedirs`；流式按 `Content-Length` 读盘，不设硬限。
- multipart 解析用标准库（`email.parser` 或手切 boundary），必须正确处理二进制内容中含 boundary 串的对抗用例。
- 日志复用现有 logging：每次上传/下载记 path、字节数、来源 IP。
- firewalld：现有靶机 9000 可达即内网无阻拦；文档注明"开 firewalld 的靶机需放行 9001"。
- HTTP handler 不接入 TCP client tracking（不影响 upgrade idle 判定；autoupgrade.py:21 `IDLE_THRESHOLD = 1800` 即 30 分钟门槛兜底，已核实）。
- 3.8 兼容性已核实：`ThreadingHTTPServer` 在 Python 3.8.10 可导入（打包环境同版本）。

## 2. mcp_socket_server：工具收敛 17 → 12（0.2.0）

### 2.1 删除 7 个工具（连 socket_client 方法 / commands.py 注册 / 测试一起删）

| 工具 | 删除依据 |
|---|---|
| version_query | 审计 0 次（靶机侧 3 次含测试）；CLI `socket_server current` 覆盖（cli.py:70 `cmd_current` 已核实存在） |
| version_detail | 审计 0 次；release notes 给人不给 LLM |
| filesize | 审计 0 次；`cmd_exec` `stat -c %s` 替代 |
| command_exists | 审计 0 次；`cmd_exec` `which` 替代 |
| routeinfo | 审计 0 次；capture_start 缺省 iface 的 routeinfo 逻辑在**服务端内部**（capture.py:489-494 `tcpdump_start` 的 `eth=None` 分支直接调服务端 `routeinfo()`，与 MCP 工具无关），删 MCP 工具无影响 |
| file_upload | 被 HTTP multipart 上传替代（§1.3） |
| file_download | 被 HTTP GET 下载替代；download_dir 配置不生效 bug 随代码删除消失 |

### 2.2 新增 replay_run（datatype 0）

```json
{
  "name": "replay_run",
  "description": "在靶机回放 pcap 流量文件(scapy 双接口收发,支持限速/改五元组)。需先用 HTTP 上传把 pcap 传到靶机: curl -F \"path=/home/tmp/x.pcap\" -F \"file=@x.pcap\" http://<host>:9001/files。回放与抓包/拨测同靶机互斥;回放+抓包请分属两台靶机(跨机拓扑)。返回 {total_files,total_packets,total_bytes,uplink_packets,uplink_bytes,downlink_packets,downlink_bytes,flow_count,duration_seconds,target_mbps,actual_mbps}",
  "inputSchema": {
    "properties": {
      "targets":       {"type": "array", "items": {"type": "string"}, "description": "靶机列表(裸IP/host:port/@tag)"},
      "pcaps":         {"type": "array", "items": {"type": "string"}, "description": "靶机上 pcap 绝对路径列表"},
      "uplink_iface":  {"type": "string", "description": "上行物理网卡名,如 eth0"},
      "downlink_iface":{"type": "string", "description": "下行物理网卡名,单臂可同上行"},
      "uplink_vlan":   {"type": "integer", "description": "上行 VLAN ID(可选)"},
      "downlink_vlan": {"type": "integer", "description": "下行 VLAN ID(可选)"},
      "mbps":          {"type": "integer", "default": 0, "description": "限速 Mbps,0=不限速"},
      "force_ip_src":  {"type": "string", "description": "强制替换源 IP(可选)"},
      "force_ip_dst":  {"type": "string", "description": "强制替换目的 IP(可选)"},
      "force_sport":   {"type": "integer", "description": "强制替换源端口(可选)"},
      "force_dport":   {"type": "integer", "description": "强制替换目的端口(可选)"},
      "port":          {"type": "integer", "default": 9000},
      "description":   {"type": "string", "description": "一句话说明本次调用的目的（将展示给用户），不超过 50 字"}
    },
    "required": ["targets", "pcaps", "uplink_iface", "downlink_iface"]
  }
}
```

- 锁：新增 `LockClass.REPLAY`，per-target 排他（与 CAPTURE/SHELL/BROWSER 互斥，机制复用 `_exclusive_holder`）。
- 响应：PcapReplayer stats dict 直接 JSON 透传（datatype 0 现状）。
- 不透出：`force_build_flow`/`enable_pcap_cache`/`verbose` 等调优参数（YAGNI）。

### 2.3 新增 socket_listen（datatype 171-174 编排）

```json
{
  "name": "socket_listen",
  "description": "在靶机启动 TCP 端口监听接收流量,配合回放做对比测试。action: start=启动返回pid / clean=清空已收数据 / save=已收数据保存到靶机文件 / fetch=取回已收数据(超过 max_bytes 落盘靶机并截断返回)。注意: 已在监听时再 start 为幂等操作,返回现有 pid,更换 listen_port 需先等监听自然结束(服务端无 stop 动作,重启靶机进程才能换端口)。同一监听实例须按 start→...→save/fetch 顺序调用",
  "inputSchema": {
    "properties": {
      "targets":     {"type": "array", "items": {"type": "string"}},
      "action":      {"type": "string", "enum": ["start", "clean", "save", "fetch"], "description": "start=启动 / clean=清空 / save=存文件 / fetch=取回"},
      "listen_port": {"type": "integer", "default": 30001, "description": "仅 start 用:监听端口"},
      "file":        {"type": "string", "description": "仅 save 用:保存到靶机的绝对路径"},
      "max_bytes":   {"type": "integer", "default": 1048576, "description": "仅 fetch 用:返回内容上限(gzip 解压后明文字节数),超限截断并落盘靶机 /home/tmp/listen_<ts>.log"},
      "port":        {"type": "integer", "default": 9000},
      "description": {"type": "string", "description": "一句话说明本次调用的目的（将展示给用户），不超过 50 字"}
    },
    "required": ["targets", "action"]
  }
}
```

- 动作映射：start→171（`{"host":null,"port":listen_port}` 返回 `{"pid":[...]}`；**幂等**：已在监听时返回现有 pid，新 listen_port 被忽略；**失败判定**：`pid` 为空列表时该 target 判 failed，reason 注明"监听进程未起来，检查端口占用"）；clean→172（`ok`/`error`，`error` 判 failed）；save→173（`{"file":file}`，返回 `ok`/`error`）；fetch→174（`[8B len][gzip]` 解包；**失败判定**：未启动监听时 174 返回 `{"error":"未启动监听"}` JSON 而非 gzip 流，socket_client 需先探测响应形态，`error` 判 failed）。
- fetch 响应：`{target, size, data_b64(≤max_bytes), truncated, file_path(超限时)}`。**语义定死**：`max_bytes` 以 gzip 解压后明文字节数计（与 `size`/`data_b64` 解压后一致）；`size` = 全量明文字节数（非截断后），LLM 据此判断是否需要取全量。**超限落盘实现路径**：MCP 调 174 已拿到全量数据，超 `max_bytes` 时**再调一次 173（save）把数据存到靶机** `/home/tmp/listen_<ts>.log`（173 本就是在靶机侧写 `ss.data`），响应带 `file_path` + `truncated:true`，引导用 HTTP 下载取全量；未超限不产生额外往返。
- 锁：`LockClass.SYSTEM`——**从 stub 变为 per-target 排他实现**（见 §4 修订 1）。**持锁时长：逐动作短锁**（每次调用前 acquire、finally release）——与 CAPTURE 的跨调用长锁不同：监听四动作是离散操作，短锁足够防单次动作并发；跨调用状态由服务端单例 `ss` 天然保持（MCP 无状态），长锁反而有"LLM 忘了后续动作锁永不释放"的风险。代价：两个 LLM 会话交错对同一靶机 clean/fetch 可能读到意外数据——文档描述已写明"同一监听实例须按顺序调用"，属约定俗成。

### 2.4 保留工具描述同步（HTTP 用法写进描述）

- `isfile`：描述附 HTTP 上传/下载 curl 一行示例（multipart、host=注册 IP、IPv6 方括号、端口 9001≠TCP 9000）。
- `pcap_flow_extract`：附"pcap 可用 HTTP 下载到本地分析"提示。
- `replay_run`：附"回放前先用 HTTP 上传 pcap"提示（§2.2 已含）。
- schema 全量规范：业务工具统一 `targets` + `port`(默认 9000) + 可选 `description`；注册表工具（list/add/remove_target）不含 targets。

### 2.5 最终工具清单（12 个）

注册表 3：`list_targets` / `add_target` / `remove_target`
读支撑 3：`isfile` / `isdir` / `pcap_flow_extract`
写核心 6：`cmd_exec` / `capture_start` / `capture_stop` / `boce_run` / `replay_run` / `socket_listen`

永不暴露：datatype 16（eval 任意 Python——唯一不可替代场景是调试服务端进程自身，风险收益不成比例）、121-123（已弃用）。

## 3. 兼容性与发版

| 项 | 说明 |
|---|---|
| 打包 | spec `hiddenimports` 加 `socket_server.http_file`；增量 = http.server + email.parser 等标准库 pyc（<100KB 量级，16MB 二进制占比 <1%） |
| 旧 MCP + 新靶机 | 无感（TCP 协议零改动） |
| 新靶机 + 旧中央节点 | HTTP 9001 空跑无害 |
| config 未加 http_port | 默认 9001 开启 |
| 显式关闭 | http_port=0 |

**实现注意**：`/opt/socket/config` 现有 `port=9000` 行是历史遗留**未接线**配置（serve 的 TCP 端口实际来自 `cli.py` argparse 默认值，从未读 config）。`http_port=` 是新接线的 config key（读取模式同 `proxy=`/`autoupgrade=` 先例），实现时勿参照 `port` 的"配置"，也不顺手改动 `port`（超范围）。

发版顺序：
1. socket_server 1.7.0（HTTP 文件服务；datatype 无变化）
2. mcp_socket_server 0.2.0（工具收敛 + 2 新工具；独立发版）
3. 靶机自动升级铺开 1.7.0 后 MCP 文件传输即切 HTTP，中间态两者并存无破坏

## 4. Review 修订（已并入上文对应节）

1. **SYSTEM 锁 stub 缺口**：`locks.py:122` 现对 SYSTEM 直接 raise。修订：实现为 per-target 排他（复用 `_exclusive_holder`，去 stub + 3 测试）。不做全局排他（一台监听锁死所有靶机过度设计）。
2. **REPLAY×CAPTURE 同机互斥**：维持互斥不放松；"回放+抓包"跨机拓扑写进 replay_run 描述（真实业务即跨机）。
3. **fetch 体量风险**：加 `max_bytes`（默认 1MB）截断 + 落盘靶机 + `truncated` 标注，防大监听数据撑爆 MCP 响应。落盘实现：超限时 MCP 复用 173（save）在靶机侧写 `ss.data`，响应带 `file_path`。

## 5. 测试计划

### socket_server
- 单元：multipart 解析（正常/缺字段/文件名异常/二进制含 boundary 串）、路径归一化（**含穿越用例**：`path=/home/tmp/../../etc/passwd` → realpath 归一化后行为断言）、400/404/500 分支、query UTF-8 解码、http_port=0 不启动。
- 端到端（本机 serve）：curl 下载 sha256 比对；上传后 isfile+字节数核对；IPv6 `http://[::1]:9001` 全流程。
- `test_e2e.py` 加 HTTP 文件传输段（沿用逐步 check 风格）。

### mcp_socket_server
- 删 7 工具的测试同步删；`replay_run`/`socket_listen` mock 测试（mock_socket_server 补 datatype 0/171-174 handler）。
- locks.py SYSTEM per-target 排他 3 测试（获取/互斥/释放）；**REPLAY×CAPTURE/SHELL/BROWSER 互斥测试**（新锁类与既有锁类的交互）。
- **socket_listen 编排链路测试**：start 空 pid 判 failed；fetch 未启动监听（`{"error":...}`）判 failed；fetch 超限二次调 173 落盘并返回 `truncated+file_path`。

### 文档同步（随代码同 commit）
- `docs/api-guide.md`：补 HTTP 文件服务（GET/POST 9001）两行接口 + 示例。
- `docs/mcp-integration.md`：§2 边界表补一行"HTTP 文件服务随靶机二进制部署"；datatype 表注 `file_upload/download` MCP 工具已删、改用 HTTP。
- 全量 pytest；真实靶机端到端：replay_run 回放真实小 pcap；socket_listen start→fetch 链路；ListTools 校验 12 工具 schema（含 description）。

## 6. 明确不做（YAGNI）

- 全局 SYSTEM 锁、同机回放+抓包锁共存、multipart 以外的编码格式、HTTP 鉴权（内网信任模型不变）、下载白名单目录、python_cmd 暴露、121-123 复活。
