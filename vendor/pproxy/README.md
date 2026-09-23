# vendor/pproxy — 本地维护的 pproxy 代理库

基于 **pproxy 2.7.9**（上游 [qwj/python-proxy](https://github.com/qwj/python-proxy)，MIT License）fork 的本地维护副本。
上游 bug 修复无法随 pip 分发，且项目有按代理统计流量等定制需求，故整体 vendor 化，修复直接内化进源码。

**引用方式**：`src/interfaces/proxy_server.py` 将 `vendor/` 注入 `sys.path` 后 `import pproxy`，项目运行与测试均使用本副本，与 pip 安装的上游版本无关。

## 本地修复清单（相对上游 2.7.9 的全部差异，均在 `proto.py` / `server.py`）

### proto.py

| 修复 | 说明 |
|---|---|
| `sslwrap` verbose 静默 | 原 `verbose=None` 时退化为 `print` 刷 stdout，改为静默 |
| SOCKS5 认证失败应答 | 无可接受认证方法回 `05 FF`（RFC 1928 §3）、凭证错误回 `01 01`（RFC 1929 §2）后再关闭，不再裸断连 |
| SOCKS4 应答推迟 + 失败应答 | `Socks4.accept` 原在握手后**立即回 `00 5A`（granted）**，之后无论目标连接是否成功，客户端都只看到"已授权"——失败码永远发不出去（失败表现为"授权后立刻断连"）。改为与 Socks5 同模式：成功应答推迟到隧道建立后（`client_connected`），并新增 `error_reply` 回 `00 5B`（request rejected or failed），与 SOCKS5 的 REP 码 / HTTP 的 502 对齐 |
| SOCKS5 UDP ASSOCIATE | 支持 RFC 1928 §7 UDP 中继握手；未配置 `-ul` 时回 `0x07` command not supported |
| SOCKS5 成功应答推迟（#169） | 目标连接成功后回 `05 00`；失败时经 `error_reply` 回 RFC 1928 REP 错误码（refused→`05`、DNS/IDNA 失败→`04`、超时→`06`、BLOCK→`02`），客户端不再收到空响应 |
| HTTP 非代理形式请求 | 直接访问代理端口（path-only GET）回真实 `404 Not Found` 再关闭，不裸断连 |
| WebSocket 握手修复 | `Sec-WebSocket-Accept` 改用 RFC 6455 标准 GUID + 完整 20 字节摘要（原用固定字符串且截断 16 字节）；`guess()` 比较真实请求头（原 `StreamReader == bytes` 恒 False） |
| 无效转义序列（#196） | `netloc_split` 正则加 `r` 前缀，消除 Python 3.12+ `SyntaxWarning` |
| `get_event_loop` → `get_running_loop`（#200/#205） | Socks4/Socks5 连接路径，兼容 Python 3.14 |

### server.py

| 修复 | 说明 |
|---|---|
| `sslcontexts` 弱引用 | 原全局 `list` 只 append 不清理 → SSLContext 泄漏/堆损坏；`_WeakSSLContexts` 弱引用容器，Connection 移除后可 GC |
| `modstat` 钩子传 `roption` | 4 参签名（原 3 参），按代理统计流量（项目 per-proxy 流量功能依赖） |
| 目标连接失败回错误应答（#169） | `stream_handler` 在 `open_connection`/`prepare_connection` 失败时调协议 `error_reply`——**含连接超时**（超时曾单独一个分支只 `raise`，绕过 `error_reply`，客户端的超时场景仍收到空响应）；须传**原始异常**，包装成 `Exception` 后 Socks5 映射不出 `0x06` |
| BLOCK 规则应答 | 命中规则回 RFC 1928 REP `0x02` 再关闭 |
| `direct_fallback` 关闭直连回退 | 原 `schedule(...) or DIRECT`：上游列表空/无可用项时请求会以**本机真实 IP 直连**目标并回成功响应（静默 IP 泄漏）。新增 `direct_fallback` 参数（缺省 `True` 保留 pproxy CLI「`-r` 缺省即直连」语义），置 `False` 时无可用上游即按协议回错误（HTTP 502 / SOCKS5 REP `0x01`），TCP 与 UDP（`datagram_handler`）两路一致；本项目的 `PproxyServer` 恒传 `False`，需直连须显式把 `direct://` 放进上游列表 |
| 规则文件缺失友好报错 | `compile_rule` 文件不存在抛 `ArgumentTypeError`，不再裸回溯 |
| 跳转链双下划线（#203） | 只把含 `://` 的段视为独立跳转，fragment auth 用户名里的合法 `__` 不被误切 |
| `proxy_by_uri` 参数校验顺序 | 先查 `err_str` 再访问 `protos`（原 `protos` 为 None 时抛 TypeError 掩盖真实错误） |
| UDP 监听端口显示 | `print_server_started` 支持 `DatagramTransport`（无 `.sockets`，经 `get_extra_info('socket')` 取真实端口） |
| `get_event_loop` → `get_running_loop`（#200/#205） | 多处异步路径，兼容 Python 3.14 |

## 测试与维护

- **回归测试**：`tests/vendor/test_pproxy_vendor.py` — 覆盖上述修复（CLI e2e + 单元断言），修改本目录后必须全绿。
- **升级上游**：替换 `vendor/pproxy/` 内容 → 手工重放本清单中的修复（或用 `git diff` 对照上游逐个合入）→ 跑 `tests/vendor/` + `tests/interfaces/test_proxy_server.py` + `tests/e2e/`。
- **查看与上游差异**：`git diff --no-index <pip-pproxy路径>/pproxy vendor/pproxy`（本地 pip 安装的 pproxy 即上游原版）。
