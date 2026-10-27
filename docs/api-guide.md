# socket_server TCP 接口指南

## 协议格式

### 请求

```
[4字节总长度 i][4字节类型 i][载荷]
```

- 总长度 = 类型(4B) + 载荷 的字节数，小端序
- 载荷为 JSON 时使用 UTF-8 编码

### 响应

无长度前缀，服务端直接 sendall 原始字节。不同 datatype 响应格式不同，见下表。

### 文件上传流程（特殊）

文件上传在同一 TCP 连接上分4步完成，21→22→23→24：

| 步骤 | datatype | 发送内容 | 服务端响应 |
|------|----------|----------|-----------|
| 1 | 21 | `{"filepath":"/remote/path","gzip":false}` | `21 ok` |
| 2 | 22 | `[8字节文件长度 Q]` | `22 ok` |
| 3 | 23 | `[4字节长度 i][文件原始字节]` | `23 ok` |
| 4 | 24 | 空 | `24 ok` |

注意：步骤3不带类型前缀，格式为 `[4字节长度][文件内容]`。若步骤1中 `"gzip":true`，则步骤3发送的是 gzip 压缩后的内容，长度字段对应压缩后大小。

---

## 接口清单

| datatype | 功能 | 请求载荷(JSON) | 响应格式 | 响应示例 |
|----------|------|----------------|----------|----------|
| **0** | pcap发包 | `{"uplink_iface":"eth0","downlink_iface":"eth1","pcaps":["/tmp/a.pcap"],"speed":1.0}` | JSON | `{"status":"ok","total":1500,"sent":1498,"lost":2}` |
| **1** | 执行命令 | `{"args":"ls -la"}` | `[4B gzip长度][gzip(JSON)]` | 解压后: `{"code":0,"stdout":"...","stderr":""}` |
| **3** | 下载文件 | `{"gzip":false}` | `[8B文件长度 Q][文件内容]` | 二进制文件数据 |
| **4** | 路由信息查询 | 无 | JSON | `{"0.0.0.0":{"Gateway":"10.0.0.1","Genmask":"0.0.0.0",...}}` |
| **5** | 开启tcpdump抓包 | `{"iface":"eth0","file":"/tmp/capture.pcap"}` | JSON | `{"res":"ok"}` |
| **6** | 停止tcpdump抓包 | `{"file":"/tmp/capture.pcap"}` | JSON | `{"res":"ok"}` |
| **7** | 判断文件是否存在 | `{"file":"/etc/hostname"}` | JSON | `{"res":true}` |
| **8** | 判断目录是否存在 | `{"dir":"/tmp"}` | JSON | `{"res":true}` |
| **9** | 创建目录 | `{"dir":"/tmp/new_dir"}` | JSON | `{"res":null}` |
| **10** | 修改MTU | `{"eth":"eth0","value":2000}` | JSON | `{"res":null}` |
| **11** | 获取文件大小 | `{"path":"/etc/hostname"}` | JSON | `{"res":128}` |
| **14** | 获取版本号 | 无 | JSON字符串 | `"1.3.0"` |
| **15** | 解压zip文件 | `{"file":"/tmp/a.zip","outdir":"/tmp/output"}` | 纯文本 | `ok` |
| **16** | Python操作 | `["1+2"]` | `[4B gzip长度][gzip(JSON)]` | 解压后: `3` |
| **18** | 查询命令是否存在 | `{"cmd":"ls"}` | JSON | `{"res":true}` |
| **19** | 查询版本详情 | 无 | JSON | `{"version":"1.3.0","repo":"weihang1258/socket_server","latest_version":"1.4.0","has_upgrade":true}` |
| **20** | 升级落地(1.8.0+，中央先经 :9001 推包到 versions/<版本>/.staging/) | `{"version":"1.8.0","sha256":"<64hex>","force":false}` | JSON | `{"ok":true,"version":"1.8.0"}` 或 `{"error":"原因"}`；受理后后台重启生效 |
| **121** | 开始scapy抓包 | `{"iface":"eth0","count":100}` | 纯文本 | `ok` |
| **122** | 停止scapy抓包 | 无 | JSON | `ok` 或 `{"error":"未在抓包"}` |
| **123** | 下载pcap包(需先121) | 无 | `[8B长度 Q][pcap二进制]` | pcap文件数据 |
| **131** | 拨测(需chromium) | `{"url":"http://example.com","chromium_path":"/opt/socket/chrome-linux/chrome"}` | `[4B长度 i][JSON]` | `{"status":"ok","load_time":1.2,...}` |
| **171** | socket监听-启动 | `{"port":8080}` | JSON | `{"pid":["1234"]}` |
| **172** | socket监听-清理数据 | 无 | 纯文本 | `ok` 或 `error` |
| **173** | socket监听-保存数据 | `{"file":"/tmp/data.log"}` | 纯文本 | `ok` 或 `error` |
| **174** | socket监听-传输数据 | 无 | `[8B长度 Q][gzip(JSON)]` | gzip压缩的监听数据 |
| **175** | socket监听-停止 | 无 | 纯文本 | `ok` 或 `{"error":...}` |
| **200** | 提取pcap五元组流 | `{"dir":"/tmp/pcaps"}` | `[4B长度 i][JSON]` | `[{"src_ip":"1.2.3.4","src_port":1234,"dst_ip":"5.6.7.8","dst_port":80,"proto":6},...]` |

---

## 响应格式说明

| 标记 | 含义 |
|------|------|
| 纯文本 | 直接 recv 即可，如 `ok`、`error`、`21 ok` |
| JSON | recv 后 `json.loads(data.decode('utf-8'))` |
| `[4B长度 i][gzip(JSON)]` | 先取4字节长度，再取对应长度的 gzip 数据，解压后 JSON 解析 |
| `[8B长度 Q][内容]` | 先取8字节长度(Q, 小端)，再取对应长度的原始字节 |
| `[4B长度 i][JSON]` | 先取4字节长度，再取对应长度的字节，UTF-8 解码后 JSON 解析 |

---

## 注意事项

1. **文件上传**必须使用21→22→23→24四步协议，不能跳步，需在同一TCP连接内完成
2. **步骤3(23)** 发送格式特殊：`[4字节长度][文件内容]`，不带4字节类型前缀
3. **scapy抓包(121/122/123)** 需在同一连接内操作，跨连接无法停止/下载
4. **拨测(131)** 首次调用会自动安装chromium依赖库（需sudo权限）
5. **修改MTU(10)** 需sudo权限，执行后等待5秒生效
6. **tcpdump抓包(5/6)** 需系统安装tcpdump命令
7. **datatype 14** 仅返回版本号字符串，**datatype 19** 返回完整版本信息含升级状态

## HTTP 文件服务（1.7.0+）

与 TCP :9000 并行的旁路 HTTP 服务，默认 :9001（`/opt/socket/config` 的 `http_port=` 可改，0=关闭）。

| 方法 | 路径 | 参数 | 成功 | 失败 |
|------|------|------|------|------|
| GET | `/files?path=<绝对路径>` | query: path | 200 文件流（octet-stream, attachment） | 400 缺 path / 404 不存在 |
| POST | `/files` | multipart: `path`(文本) + `file`(二进制) | 200 `{"ok":true,"path":...,"size":N}` | 400 缺字段 / 500 写盘失败 |

curl 示例：
```bash
curl -o out.pcap "http://<host>:9001/files?path=/home/tmp/tmp.pcap"
curl -F "path=/home/tmp/deploy.zip" -F "file=@deploy.zip" http://<host>:9001/files
```

注意：开启了 firewalld 的靶机需放行 9001 端口（`firewall-cmd --add-port=9001/tcp --permanent && firewall-cmd --reload`）；内网默认无 firewalld 阻拦（与 TCP 9000 同级）。

**大小不限（1.8.2+）**：上传下载全程流式（边收边写/边读边发），服务内存恒定
（实测 2GB 传输 RSS 峰值 ~20MB），GB 级文件无压力；1.8.1 及更早整载内存，
仅建议 MB 级，且文件内容含 `\r\n--<boundary>` 前缀序列时可能被截短（已修复）。
上传中落盘为 `<path>.part` 临时文件，收完原子顶替，中断不留半截文件。
