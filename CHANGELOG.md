# Changelog

本文件记录 socket_server 各版本的变更。版本号见 [`socket_server/version.py`](socket_server/version.py)。

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
