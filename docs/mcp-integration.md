# MCP 对接文档

socket_server 作为执行单元，通过独立的 MCP server 暴露给 LLM 平台调用。本文档定义两者的边界、契约与对接方式。

## 1. 架构

```
┌──────────────────────────────────────────────────────────────────┐
│              LLM 客户端（Claude / Cursor / 平台）                  │
│   用户："在机房A所有靶机抓 eth0 的包" → LLM 调用 capture_start     │
└──────────────────────────┬───────────────────────────────────────┘
                           │ MCP 协议（SSE / streamable HTTP）
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│            mcp_socket_server（独立项目，中央节点，1 份）             │
│                                                                  │
│  工具层（批量 targets）  鉴权/审计  靶机注册表  连接池  状态锁      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  TCP 客户端（源自 socket_server/test_e2e.py）              │    │
│  │  send_request / recv_raw / recv_file / recv_text          │    │
│  └──────────────────────┬───────────────────────────────────┘    │
└─────────────────────────┼────────────────────────────────────────┘
                          │ TCP 9000，并行多连接
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
   ┌────────────┐  ┌────────────┐  ┌────────────┐
   │ 靶机 A      │  │ 靶机 B      │  │ 靶机 N      │
   │socket_server│  │socket_server│  │socket_server│
   │  :9000     │  │  :9000     │  │  :9000     │
   │ (systemd)  │  │ (systemd)  │  │ (systemd)  │
   └────────────┘  └────────────┘  └────────────┘
```

## 2. 项目边界

| | socket_server（本仓库） | mcp_socket_server（新项目） |
|---|---|---|
| 角色 | 执行单元，跑在靶机 | LLM 入口 + 中央调度，跑在中央节点 |
| 部署份数 | N（每台靶机 1 份） | 1（中央节点） |
| 位置 | `/opt/socket_server` | `/opt/mcp_socket_server`（同级） |
| 运行时 | CentOS 7 + Python 3.8 + PyInstaller 单文件 | 中央节点 + Python 3.10+ |
| 依赖 | scapy/requests/pyppeteer 等 | mcp SDK + asyncio + 本仓库客户端库 |
| 发版 | GitHub Release + 自动升级 | 独立 git 仓库，独立发版 |
| 安全边界 | TCP 9000（内网信任） | 对外暴露，需鉴权/审计/注入防护 |

**不要把 MCP 代码塞进 socket_server 仓库。** 部署位置、依赖栈、发版节奏、安全模型都不同，物理隔离。

## 3. 共享契约：协议定义

两个项目唯一的耦合点是 **socket_server 的 TCP 协议**（datatype 编号 + 参数 JSON schema）。权威定义在 socket_server 仓库，MCP server 据此生成工具 schema。

### 协议帧格式
```
[4 字节长度 i][4 字节 datatype i][payload]
```
- 长度 = datatype(4) + payload 字节数
- payload 多为 JSON（`json.loads(s=data)`），文件类为二进制

### 主要 datatype（MCP 工具映射基础）

| datatype | 功能 | 状态 | MCP 锁类 | 并发 |
|----------|------|------|---------|------|
| 0 | scapy 发包 | 写 | replay | 同靶机排他 |
| 1 | 执行命令 | 写 | shell | 同靶机排他，危险需确认 |
| 3 | 文件下载 | 写 | file_io | 按 path |
| 4 | 路由信息 | 读 | None | 自由并发 |
| 5 | tcpdump 开始抓包 | 写 | capture | 按 (iface,path) 排他，不同组合可并发 |
| 6 | tcpdump 停止抓包 | 写 | capture | 释放对应 path 锁 |
| 7/8 | 文件/目录存在 | 读 | None | 自由并发 |
| 9 | 创建目录 | 写 | file_io | 短排他 |
| 11 | 文件大小 | 读 | None | 自由并发 |
| 14 | 获取版本号 | 读 | None | 自由并发 |
| 18 | 命令是否存在 | 读 | None | 自由并发 |
| 131 | 拨测（需 chromium） | 写 | browser | 同靶机排他 |
| 171-174 | 抓包/发包相关 | 写 | capture/replay | 见上 |
| 200 | （见 handlers.py） | - | - | - |
| ~~121/122/123~~ | ~~scapy 抓包~~ | **弃用** | - | 勿暴露 |

> 完整 datatype 列表见 `socket_server/handlers.py` 的 `do()` 函数。MCP server 工具 schema 应从此处派生，勿手抄。

## 4. TCP 客户端库

MCP server 连接 socket_server 的客户端逻辑，**源自 socket_server 仓库的 `test_e2e.py`**：

```python
# 核心函数（test_e2e.py 已实现，MCP server 复用）
def send_request(sock, datatype, payload_bytes=b""):
    body = struct.pack("i", datatype) + payload_bytes
    msg = struct.pack("i", len(body)) + body
    sock.sendall(msg)

def recv_raw(sock, close_after=True): ...      # 收 4B 长度 + 原始字节
def recv_text_response(sock, close_after=True): ...  # 收文本响应
def recv_gzip_response(sock, close_after=True): ...  # 收 gzip 压缩响应
def recv_file_response(sock, close_after=True): ...  # 收文件（8B 长度 + 内容）
def do_file_upload(sock, filepath_remote, content, use_gzip=False): ...
```

**协议变更同步规则**：socket_server 改 datatype/参数时，同步更新 `test_e2e.py` 客户端逻辑；MCP server 跟随 `test_e2e.py` 演进。建议把 `test_e2e.py` 的客户端部分抽成独立 `socket_client.py` 模块便于复用。

## 5. MCP server 设计要点

### 5.1 批量优先
每个工具带 `targets: string[]`，一次调用 fan-out 到多靶机并行执行，返回逐台结果：
```json
{"ok": 45, "failed": [{"target": "10.0.0.5", "reason": "..."}], "timed_out": []}
```

### 5.2 靶机注册表
LLM 不记 IP，按标签选机（`@机房A`）。`list_targets` 工具查询在线靶机。

### 5.3 连接池（每靶机分池）
| 参数 | 值 | 说明 |
|------|----|----|
| min_idle | 0 | 突发型命令，不留常连 |
| max_conn | 3-5 | 单靶机并发上限（socket_server 单线程） |
| idle_timeout | 10 min | 空闲超时关闭 |
| borrow_timeout | 10s | 池满等待取连接超时 |

借出即心跳校验，异常/超时连接直接销毁不归还。

### 5.4 状态锁（按冲突类）
每靶机一把多模式锁，按 `lock_class` 仲裁（见上表）：
- 读类（version/isfile/routeinfo）：共享，自由并发
- capture：按 (iface, path) 排他，不同组合可并发
- SYSTEM（version_switch/upgrade/firewall_disable）：全局排他，执行前先停在跑的长操作

### 5.5 鉴权审计
- MCP 层 token 校验 + 调用日志落盘
- 审计维度：(用户, 靶机, 操作, 结果, 时间)
- 危险操作（switch/exec/firewall）二次确认

### 5.6 长操作连接早归还
`capture_start` 启动后 tcpdump 在靶机后台跑，MCP 侧 TCP 连接空闲——立即归还连接池，锁持有到 `capture_stop`。

## 6. MCP server 工具清单（建议首批）

只读类（先跑通链路）：
- `list_targets` — 列在线靶机 + 标签
- `version_query(targets)` — 查版本（datatype 14）
- `isfile(targets, path)` / `isdir(targets, path)` — 文件/目录存在
- `routeinfo(targets)` — 路由信息
- `command_exists(targets, cmd)` — 命令是否存在

写类（加确认后开放）：
- `capture_start(targets, iface, path, filter)` — datatype 5
- `capture_stop(targets, path)` — datatype 6
- `file_upload(targets, remote_path, content)` / `file_download(targets, path)` — datatype 22/3
- `cmd_exec(targets, command, whitelist)` — datatype 1，命令白名单
- `boce_run(targets, url, ...)` — datatype 131
- `version_switch(targets, version)` — 危险，二次确认
- `firewall_disable(targets)` — 危险，二次确认

## 7. 部署拓扑

```
中央节点（1 台）
├── mcp_socket_server（Python 3.10+，SSE/HTTP 监听）
└── 连接池 → 各靶机 :9000

靶机（N 台）
└── socket_server（systemd，:9000，自动升级）
```

## 8. 协议演进流程

1. socket_server 改 datatype/参数 → 更新 `handlers.py` + `test_e2e.py` 客户端 + 本文档 datatype 表
2. 发 socket_server 新版（GitHub Release，自动升级到靶机）
3. mcp_socket_server 跟随更新客户端库 + 工具 schema → 发版
4. MCP server 重启加载新 schema

双向不耦合：socket_server 发版不依赖 MCP server，反之亦然。契约（datatype 表）是唯一同步点。
