# 架构文档

## 架构总览

采用干净架构四层（洋葱模型），依赖只能从外层指向内层：

```
框架与驱动层 framework/        config / container / cli / serve / logging
   ↓
接口适配器层 interfaces/        sqlite_store / http_client / scheduler / proxy_server / proxy_scheme / ua_generator / sources/
   ↓
用例层 use_cases/              fetch / get / record / request_with_retry / check_proxies
   ↓
实体层 domain/                 proxy_pool / models / ports（纯业务规则，零第三方依赖、无 IO）
```

- **实体层（domain）**：`ProxyPool` 代理池领域模型 + `SourceSpec` 值对象 + `ProxySource/ProxyStore/HttpGetter/TaskScheduler` 抽象端口（Protocol）。纯内存，无 IO、无第三方依赖。
- **用例层（use_cases）**：应用业务逻辑，构造注入；只依赖 domain 的端口与模型。
- **接口适配器层（interfaces）**：实现 domain 端口（文件存储、requests 封装、异步调度器、pproxy 封装、各代理源适配器）。
- **框架层（framework）**：`config.py` 配置常量、`container.py` 组合根（依赖组装）、`cli.py` 命令分发、`serve.py` 服务器主循环、`logging.py` 日志。

组合根 `build_container()` 是唯一组装点：构造 `ProxyPool` → 从 `.cache.db` 恢复状态 → 组装各用例 → 暴露给 CLI。

## 核心机制

### 1. 代理生命周期

```
拉取入组 ──成功使用──▶ trusted 信任池（永不过期）
   │                    ▲
   │                    │ 记录成功
   │                    │
   │ 连续失败累计        │
   │ consecutive_fails >= 阈值       │
   ▼                    │
blacklist 黑名单 ◀───────┘
（ttl 到期后自动解除，可重新入池）
```

一条代理的状态机：**普通组 →（请求成功）→ trusted →（连续失败超阈值）→ blacklist →（黑名单过期）→ 可重新被拉取入池**。

### 2. 分组与 TTL

`PROXY_GROUPS`（见 `config/pool.toml` 的 `[groups]`，组名/语义/TTL 全部由配置决定，代码不硬编码组名，此处不重抄配置值）：

| 组 | 说明 |
|---|---|
| `default-http` / `default-https` / `default-socks5` | API 源的 HTTP/HTTPS/SOCKS5 代理 |
| `freeproxy-world-http` / `-https` / `-socks5` | FreeProxy.World 三类代理 |
| `github-http` / `-https` / `-socks5` | GitHub 静态列表的 HTTP/HTTPS/SOCKS5 代理 |
| `Proxy-pool-standby-queue` | serve 代理服务队列（pproxy 上游），由心跳探测维护 |
| `trusted` | 请求成功晋级的代理 |
| `blacklist` | 失败淘汰代理的归属，不可请求 |

TTL 规则：代理入组时记录 `joined_at`，`now - joined_at >= ttl` 即过期；`ttl=None` 永不过期。过期条目在 `_cleanup_all_expired()` 统一清理（`get_available_proxies` / `export_state` 时会触发），`find_proxy` 遇到过期代理也即时删除。

### 3. 连续失败计数与淘汰

`consecutive_fails` 为**连续失败计数**：每次失败 +1，**任何成功（请求成功/心跳成功/补测成功）清零**——瞬时失败不会永久累积，只有连续失败达到阈值才淘汰。

- 每次代理请求失败：该代理 `consecutive_fails += 1`（`RecordProxyUseCase.record_fail`）；成功：`consecutive_fails` 清零（`record_success`）。
- 淘汰规则（`ProxyPool.get_available_proxies`）：
  - 普通组：`consecutive_fails >= MAX_CONSECUTIVE_FAILS` 不再参与可用集合；
  - `trusted` 组：`consecutive_fails >= TRUSTED_MAX_CONSECUTIVE_FAILS` 不再参与（容错更高）；
  - 所有组：超过 TTL 自动移除。
- `RequestWithRetryUseCase` 中，连续失败次数达到所在组阈值（`should_blacklist`：trusted 组 `TRUSTED_MAX_CONSECUTIVE_FAILS`、其余组 `MAX_CONSECUTIVE_FAILS`）即拉黑（移入 blacklist 组，连续失败计数清零）。
- 黑名单过期后可被拉取用例重新发现并入池。

### 4. 源暂停机制

- **主动暂停**：`SOURCES` 中配置 `pause_ttl` 的源，每次拉取完成后强制暂停（多阶段爬取较重的源靠它避免高频重复拉取，时长见 `config/sources.toml`）。
- **被动暂停**：拉取后计算该源的去重率 `known / total`（known = 已在 trusted 或 blacklist 的代理数），超过 `SOURCE_DEDUP_THRESHOLD` 即暂停 `SOURCE_PAUSE_TTL`（两个阈值见 `config/sources.toml` 的 `[fetch]`）。
- `source_pause_remaining(name)` 一次给出「是否暂停」（返回值非 None）与剩余秒数（未暂停或过期返回 None，过期时自动解除并移除记录）——不做第二次判定重算；手动恢复与过期自动解除都走 `resume_source()`（移除记录与置脏同处发生，漏置脏则增量保存跳过该表、DB 残留已解除的暂停行）。暂停状态持久化于 `source_paused`；`/api/source/pause|resume` 的源名必须在 `config.SOURCES` 内（否则 400——幽灵暂停记录会在快照里永不显示、UI 也解不掉）。

### 5. 拉取流程（FetchProxiesUseCase）

三阶段：

1. **逐源拉取**：遍历启用的源（跳过暂停中的源），`fetch_proxies(retries=FETCH_PROXY_RETRIES=3)`；源异常只记日志不中断整体，该源在 `collect` 里以 `None` 返回（区别于「成功但无代理」的 `[]`），**全部源都异常**时按拉取失败上报（HTTP 500），不报成「拉取完成 0 个」。带 `pause_ttl` 的源不论成败都暂停（统一在 `apply` 入池时落地，暂停集合与时长不变）。`plan(source_name)` 传源名时只拉该源（面板单源拉取按钮）；源名不在配置里或未启用则抛 `ValueError`（面板转 400——不能返回 200「拉取完成」而一个源都没拉，也不能与「源存在但暂停中所以本轮无事可做」混同），三阶段与暂停规则不变。
2. **跨源去重入组**：
   - 跳过：已在本次拉取中出现过的（跨源重复）、已存在于池中任一组的、黑名单中的；
   - 命中 `config/sources.toml` 的 `[[fetch_filter]]` 声明式规则的代理直接入黑名单，不进入可用池（如端口 80 的 Cloudflare 假代理）；规则按书写顺序取首个命中，`match` 可限定生效的源；
   - 入组名由 `_resolve_group` 计算：`group_by_protocol` 源按代理 scheme 前缀分拣为 `group-scheme`（如 `default` → `default-http`/`-https`/`-socks5`，无 scheme 前缀按 http），否则固定入 `group`；入组时按 `domain.SUPPORTED_SCHEMES`（池支持的出站协议，当前 http/https/socks5）校验，不受支持或目标组未定义则拒入并记日志。
3. **去重率检查**：见「源暂停机制」。

三段按「选源 / 网络 / 入池」拆成 `plan`（读池）→ `collect`（纯网络，不碰池）→ `apply`（写池）：**代理池是纯内存可变结构，必须单写线程**——serve 心跳轮次（fill/heartbeat）在事件循环线程改池，故 Web 面板只把 `collect` 放 executor 线程（同 `_do_purity` 的三步编排），入池回事件循环线程；CLI 等单线程路径用 `execute(source_name)` 串起三段。

### 6. 取代理策略（GetProxiesUseCase）

- `get_available_proxies()` 遍历 `REQUESTABLE_GROUPS`（8 个可请求组，含代理服务队列与 trusted），排除黑名单、超失败数代理与 `exclude` 集合；
- **trusted 优先**：先随机取 trusted 中的代理，不足再从其它可用组随机补齐——避免在大量死代理（如 freeproxy-world 假代理）中随机命中失败；
- 池空时自动触发一次 `fetch.execute()`（`cli get` 的 `--fetch` 回调）再取；
- 随机源可注入（`rng`）以测试确定性。

### 7. 带重试请求（RequestWithRetryUseCase）

```
for attempt in range(max_retries):
    取 1 个代理（exclude 已试过的）→ 请求
    成功 → record_success（晋级 trusted，consecutive_fails 清零）→ 返回 (proxy, resp)
    失败 → record_fail（consecutive_fails+1）；consecutive_fails >= 2 → 拉黑
全部失败 → 抛出最后一个异常；无可用代理 → RuntimeError("无可用代理")
```

`record_success` 对**代理服务队列成员复制进 trusted 并清零其 standby 副本的连续失败计数、保留在队列**（serve 上游不被掏空，连续语义：成功即清零），其余组代理移动晋级；取代理时 `get_available_proxies` 对多组副本自动去重，不会重复返回。

### 8. 并发调度器（ProxyScheduler）

通用**异步**任务调度器（asyncio 实现，`TaskScheduler` 端口，`run` 为协程；`check_proxies` 用例只用 `run`）：

- 协程式任务：`async def run(tasks, phase_name, do_task, on_success=None)`，`do_task` 为协程 `(proxy, task) -> result`，任务经 `asyncio.create_task` 并发执行（`asyncio.wait(FIRST_COMPLETED)` 调度）；
- 每个代理限流 `task_interval`（同代理两次任务间隔）；
- `do_task` 返回 dict 且带 `success` 键时以它为成败依据（探测任务的实际结果）；任务异常 → 该代理 `consecutive_fails+1`，超过 `max_consecutive_fails` 丢弃该代理（后续不再分配任务），未超限自动重试该任务；
- 取消：cancel 在途子任务后 re-raise；
- 轮询分配 `assign_round_robin`：第 i 个任务分给第 i % n 个存活代理，每代理可分多任务（Phase2/4 共用 `_crawl_pages`，页数远大于代理数时全部页面都被覆盖，不会跳过）；

### 9. 并发探测（CheckProxiesUseCase）

- `execute` 为协程：并发测试多个代理访问目标 URL，返回 `{proxy: {success, status_code, elapsed, error}}`；
- 网络请求经注入的 async 请求器（`AsyncHttpGetter`，aiohttp）在事件循环内原生发出，不经 `asyncio.to_thread`（避免 ssl 模块 C 层内存泄漏）；
- 未执行到的代理补 `{"success": False, "error": "未执行"}`；
- 证书校验策略由 `interfaces/proxy_scheme.verify_target` 统一给出、经组合根注入（用例不判断 scheme 前缀）：**http/socks4/socks5 代理严格校验（verify=True）**，隧道端到端校验目标证书、筛掉伪造证书的中间人代理；**https 代理不校验**（TLS 直连代理本身，校验的是代理自签证书，免费代理会全灭）。

### 10. serve — 本地代理服务器机制

见「serve 机制」章节。

### 11. HTTP 客户端（HttpGetter）

- 统一走 `requests`，默认**不校验证书**（`verify=False`）：免费代理多自签证书，校验会误杀；
- **多 IP 遍历重试**：仅对**明文 HTTP 且未走代理**的直连请求，失败时按 DNS 全部 A 记录用 `IP + Host 头` 遍历重试（规避部分节点 "TCP 通但 HTTP 层被 RST" 导致的整体失败）；
- 返回 duck-typed 响应，用例层不 import requests。

### 12. User-Agent 生成（UAGenerator）

- 基于 `fake-useragent` 的浏览器 UA 列表；
- 按 `seed`（如代理地址）经 md5 → 确定性随机选择 UA，同一 seed 结果稳定并缓存；
- Chrome UA 附带 `Sec-Ch-Ua` 系列头（版本号从 UA 中正则提取），Firefox/Safari 不带；供代理源构建浏览器化请求头。

## serve 机制（重点）

`src/framework/serve.py` + `src/interfaces/proxy_server.py`：

### 本地代理服务器（PproxyServer）

- 基于 **pproxy**，本地监听 URI 由 `[server]` 的 `host`/`port`/`listen_protocols` 拼出（`interfaces/proxy_server.listen_uri`，如 `http+socks4+socks5://host:port`，一个端口同时提供所列协议）。入站协议是**客户端侧**能力，取值限于 pproxy 入站协议（`INBOUND_PROTOCOLS`：http/socks4/socks5；https 需证书未启用），与 `proxy_types`（出站上游过滤）是两套词汇；
- **协议能力边界**（TCP only）：SOCKS4/SOCKS4a 与 SOCKS5 CONNECT 完整支持；**UDP 不中继**——`UDP ASSOCIATE` 与未实现的 `BIND(0x02)`/未知 CMD 统一回 RFC 1928 REP `0x07`（command not supported），客户端据此退回 TCP，**绝不静默直连**（UDP 直连=真实 IP 泄漏，等同 no-direct 的红线）。不实现 UDP 的依据：上游是 HTTP/HTTPS/SOCKS5 免费代理——HTTP(S) 协议上无法承载 UDP，SOCKS5 上游还缺 UDP ASSOCIATE 客户端（vendor 只有服务端 `accept`，且 `ProxySimple` 未覆盖 `udp_open_connection`，数据报会被发到上游的 TCP 端口），实测命中率≈0；完整实现需新增 ASSOCIATE 客户端 + 中继端口生命周期 + 每上游 UDP 能力探测，收益与代价不成比例。回归：`tests/vendor/test_pproxy_vendor.py::TestUdpAssociateNotRelayed`、`TestSocks5UnsupportedCommand`。SOCKS4a 由 `Socks4.accept` 解析（IP 为 `0.0.0.x` 时域名在 userid 之后），域名作为目标交给上游远端解析；`socks4://` 作 pproxy 上游时按 4a 下发域名（不再本机 `getaddrinfo`，避免 DNS 泄漏）——**池策略已移除 socks4 上游**（`SUPPORTED_SCHEMES`，入池即被拒；免费源不产出它），该转发能力只在 pproxy 机制层保留，入站 `socks4://` 监听不受影响。探测仍按 scheme 判定（`AsyncHttpGetter` 对 socks4/socks5 走 `aiohttp_socks.ProxyConnector` 并显式传 `rdns=True`——python_socks 的 SOCKS4 连接器把 `rdns=None` 当 False 会在本机解析，与"域名交上游解析"矛盾；判据集中在 `proxy_scheme.needs_socks_connector`）。见 `TestSocks4a`、`tests/interfaces/test_http_client.py`。
- 池内代理字符串 → pproxy 上游 URI：`https://` 前缀（HTTPS 代理，代理本身走 TLS）转换为 `http+ssl://`；http/socks5 原样传递。映射规则集中在 `interfaces/proxy_scheme.engine_uri`（协议知识的唯一归属，调用方不判断 scheme 前缀）；
- **rserver 动态同步**：`sync(proxies)` 对上游列表做集合差集增删，rserver 是同一列表引用，运行中增删对 pproxy schedule 实时生效；
- 调度算法 `salgorithm="rc"`（随机）：每次请求随机选存活代理，坏代理有概率被跳过；不用 `fa`（first_available）是因为 fa 会固定命中第一个坏代理导致持续失败。
- **绝不直连（no-direct）**：`PproxyServer` 恒以 `direct_fallback=False` 启动 handler——**无可用上游时按协议回错误**（HTTP `502` / SOCKS5 REP `0x01` general failure / SOCKS4 CD `0x5B`），**不回退以本机真实 IP 直连目标**。pproxy 原语义是 `schedule(...) or DIRECT`：上游列表空（队列未补入/全部淘汰）时请求会**直连出去并返回成功响应**，对"只为换出口 IP 而用代理"的场景是静默 IP 泄漏（客户端无从察觉）。需要直连的场景须显式把 `direct://` 放进上游列表（那时 `schedule` 返回 `ProxyDirect`，不算回退）。上游不可用（列表为空、或成员全部被标记失败）↔ 恢复的状态翻转写日志（`sync` 检测，WARNING/INFO），否则运维只见"客户端全部 502"不知原因。回归：`tests/vendor/test_pproxy_vendor.py::TestNoDirectFallback`、`tests/e2e/test_serve_e2e.py::TestServeE2E::test_no_direct_leak_when_upstream_empty`、`tests/interfaces/test_proxy_server.py::TestPproxyServer::test_no_direct_fallback`（pproxy 库缺省 `direct_fallback=True`，保留 CLI「`-r` 缺省即直连」语义，由本项目显式关闭）。附带修复：SOCKS4 的 `accept` 原**立即回 `00 5A`（granted）**，失败码永远发不出去（失败=授权后立刻断连），现与 SOCKS5 #169 同模式把应答推迟到隧道建立后，失败回 `00 5B`（见 `TestSocks4ReplyDeferred`）。
- **上游存活标记（alive 过滤器）**：pproxy 的 `schedule` 按 `o.alive` 过滤上游，但 `alive` 只由它自带的 `check_server_alive` 翻转、且仅在 CLI（`-a/--alived`）里启动——本项目走 API（`start_server`）不走 CLI，`alive` 会永远为 True，过滤器形同虚设（心跳判失败的代理在补测成功/拉黑前仍被 rc 随机选中）。修复：**存活判定唯一来源 = 代理池心跳**，`sync(proxies, unhealthy=...)` 把"正在退避补测"的成员写进上游对象的 `alive=False`（其余回到 True，标记随 Connection 复用保持），serve 每轮 `sync_upstream()` 都带上 `standby.pending_retries()`；不再另起 pproxy 的存活探测（重复探测，且"TCP 能连"≠代理可用）。全部上游不可用时 `schedule` 返回 None → 走 no-direct 拒绝。回归：`tests/interfaces/test_proxy_server.py::TestUpstreamHealth`、`tests/e2e/test_serve_e2e.py::TestServeE2E::test_unhealthy_upstream_not_selected`（两个 mock 上游用不同响应体区分，断言标记的上游一次都没被选中）、`tests/e2e/test_serve_loop_e2e.py::TestUpstreamHealthWiring`（心跳失败确实随 sync 下发）。
- **长连接空闲回收（会话双向活动）**：上游 pproxy 的转发通道按**单向** `read` 空闲超时（`SOCKET_TIMEOUT=60s`）结束通道，而流式下载/SSE/长轮询期间 client→upstream 方向天然长期静默——超过 60s 的响应必然被误杀（表现为"长连接进行到约 1 分钟时直接断开"）。vendor 内化修复：`stream_handler` 为每次转发创建 `SessionActivity`（单调时间戳），client/remote 两条通道**任一方向**收到字节即 `touch()`；通道读改为 `proto.read_session`——本方向空闲超时不再终止会话，仅当**双向共同静默满 `SOCKET_TIMEOUT`** 才结束通道（僵尸 keep-alive / 上游挂死的回收语义保持不变，无轮询开销：空闲方向每窗口至多醒一次）；写侧 `drain` 超时保留（对端停读、缓冲写满属真死，掐断合理）。回归测试：`tests/vendor/test_pproxy_vendor.py::TestSessionIdleReclaim`（patch 两处 SOCKET_TIMEOUT=2s 控时，验证流式会话完整存活 + 双向僵尸会话仍在窗口内回收）。

### 代理服务队列（Proxy-pool-standby-queue）

- 组容量 `STANDBY_QUEUE_CAPACITY`（30），永不过期；是 pproxy 的唯一上游来源。容量约束的是**进上游的数量**与 serve 自动补入的缺口，**不约束队列成员数**：手动添加 / autoload 使成员数超出容量时，超出者不进上游，但仍受心跳探测与淘汰——两个视图见下「队列成员与上游候选」；
- **填充（fill）**：队列有缺口时，候选数 = 缺口 × `standby_fill_probe_multiplier`（至少 缺口 + `standby_fill_probe_min_extra`，保证存活命中率），候选**按配置的填充组优先级**（`standby_fill_groups`，trusted 默认最前）从可用集合收集，按 `--type` 过滤类型，去重后并发探测（async）；**流式入队**——每个代理探测完成即处理（不等全部完成）：探测成功先过**入队筛选规则链**（见下），未通过则清掉各组副本 move 入 `enqueue_rejected_group`（不可请求）并不入队；通过后立即入队并实时 sync pproxy 上游（`on_change` 回调），探测失败 `record_fail` 累计到 `max_consecutive_fails` 才拉黑（单次失败不拉黑）；**队列按响应时间（latency）升序维护**，队列满时新代理更快则踢队尾 move trusted、较慢则自身 move trusted（探测成果不浪费）；
- **入队筛选（`[[server.enqueue_rules]]`）**：候选入队前按 `priority` 升序逐条判定（数字小先测），**未通过即短路后续规则**；内置四个指标（延时 `max_latency` / 国家 `allow` / 纯净度 `max_score` / 测速 `min_download_mbps`，参数键与纯判定在 `domain.enqueue_filter.METRIC_SPECS`，新增指标只加一条），默认全部 `enabled=false`。测量**惰性**：走到哪条才测哪个（延时直接取 fill 探测的 `elapsed`，国家走 `IpGeoUseCase.resolve_country`，纯净度走 `CheckPurityUseCase.resolve_score`，测速走 `PspeedTester.download_mbps` 并带 1h 缓存 + 全局串行锁），前序规则不过则昂贵测量（外网/带宽）完全不发生；**未测到值一律放过**（geoip 关闭/源未启用/超时都不该算代理不合格）并在判定明细里标注。**未通过的候选移入 `enqueue_rejected_group`（`ttl="never"`、不可列入 `requestable_groups`，启动即校验）**——留在原组会下一轮反复被选中重测。日志：每候选 DEBUG 输出判定路径（含被短路的规则不入路径）、每轮 INFO 汇总各规则淘汰数。运行时可从设置页逐指标启停/改阈值（优先级只读），落地在 `EnqueueRuleSettings` + `standby.update_enqueue_rules`。
- **心跳（heartbeat）**：并发探测队列**全部成员**（`standby_members()`，不按容量截断；严格校验证书，async）；存活**更新 latency + 清零连续失败计数、保留在队列（不晋级 trusted）**，队列按新 latency 重排；失败计 1 次：未达阈值（普通组 `MAX_CONSECUTIVE_FAILS=5`，trusted `TRUSTED_MAX_CONSECUTIVE_FAILS=10`）进入**退避补测**，达到阈值拉黑移出——一次瞬时失败不拉黑；
- **退避补测（retry_probe）**：心跳失败的代理由主循环启动独立异步复核任务（不占心跳轮次），间隔 1s/2s/4s…指数退避（上限 30s）反复探测：成功清零连续失败计数恢复，失败继续累计直至阈值拉黑；
- **主循环**：启动时先 fill → 启动服务器 → 周期性「fill → heartbeat → 为待补测代理启动复核任务 → sync → 保存」，两轮之间如再缺再 fill；轮次**绝不重叠**（以上一轮完成时刻 + interval 为下一轮最早起点，避免刷屏与 sleep 计时误差）；**流量采样独立循环**（固定 2s，不随心跳放宽——锁定单一上游时心跳降频但流量照常采，见"流量统计"）；Ctrl+C 优雅退出（取消心跳/采样与复核任务 → 关服务器 → 保存）；
- **成功复制**：代理服务队列成员被请求成功（`record_success`）时**复制进 trusted 并清零 standby 副本的连续失败计数、保留在队列**（serve 上游不被掏空，连续语义：成功即清零）；拉黑（`remove`）时清理其在所有组的副本（含 trusted 残留），单次遍历实现；
- **默认上游类型**：CLI 未指定 `--type` 时取 `[server].proxy_types`（仅启动初始值，面板可运行时增删）。该键只过滤**进入代理服务队列的上游代理类型**（fill 候选筛选），**不参与本地监听 URI 的构造**——后者由 `[server].listen_protocols` 决定（见上文本地代理服务器一节）。
- **固定代理（pin）**：Web 面板可固定队列内代理（`POST /api/standby/pin`，持久化 `pinned`+`pinned_at` 到 `.cache.db`）；固定代理**排队列最前（按固定时间 `pinned_at` 升序），正常心跳正常计数连续失败，但不管怎样都不会被踢出/拉黑/补测**——fill 只踢非固定队尾（全固定时新代理 move trusted），heartbeat 失败只计数不拉黑不补测不移出；**超出 `capacity` 的固定代理不进上游**（保留在组内不移出、不丢失固定状态；仍受心跳探测，但失败只计数不拉黑不补测——固定代理语义不变）。
- **队列成员与上游候选（两个视图）**：`standby_members()` 返回组内全部成员（不排序、不截断），用于面板展示、心跳探测与指标采样——面板显示实际成员数 vs capacity（如 35/30）；`standby_proxies()` 返回按「固定优先（`pinned_at` 升序）+ 非固定按 latency 升序」排序并截断到 capacity 的候选，只用于 pproxy 上游（`upstream_proxies()`）。两者分离的原因：容量若同时约束成员数，超出者会既不参与上游也不被探测、不计数不淘汰（组 ttl="never"，latency 恒 None 时排序永远垫底），成为永久滞留的成员；修复前手动添加 / autoload 超容量时即为此状态，回归 `tests/use_cases/test_standby.py::TestStandbyOverCapacity`。`/api/status` 把锁定代理（已 move 出队列组）作为列表**首行**并入 `standby`（行带 `locked=true`，队列组若留有历史副本按地址去重），面板据此渲染「已锁定」行、其余行灰显——锁定代理不在队列组内，只能由该合并结果下发；该合并视图由 `StandbyQueueUseCase.queue_rows()` 提供，面板展示与目标探测候选查代理共用同一口径。队列行除延迟/连续失败/固定/纯净度缓存外，另下发 `joined_at`（进入队列组的时间，前端算「入队 x」；锁定行的值是锁定时刻故不展示）、`speedtest`（测速任务状态，见"单代理测速"）与 `purity_state`（纯净度检查中间态 checking/error，结果本身由 SQLite 缓存经 `purity` 字段承载）——后两者供前端刷新页面后恢复"测速中/检查中/结果"。
- **锁定单一上游（lock）**：Web 面板可锁定队列内代理为 pproxy 唯一上游（`POST /api/standby/lock`，move 到内建隐藏组 `LOCKED_GROUP="_proxy-pool-locked"`，不在配置文件/前端组列表，持久化到 `.cache.db`）。锁定后 `upstream_proxies()` 只返回该代理（pproxy 上游只有它），**fill 跳过**（不补入），**心跳仍探测锁定代理测速**（latency 数据加延迟趋势图，失败只计数不拉黑不移出不补测），无论心跳是否成功都只用该代理；锁定期间采样点带 `locked=True` 标记，**健康率趋势图该点置 null 断开、仪表盘健康率卡片隐藏**（健康率/队列长度无意义），延迟趋势图连续显示测速；其他队列代理保留不动，解锁（`POST /api/standby/unlock`）后 move 回队列恢复原调度。与 pin 的区别：pin 只是不被踢出（rc 调度仍可能选其他代理），lock 是上游**只有**这一个代理。
- **指标采样（metrics）**：每轮 heartbeat 结束采一次队列指标（avg/min/max 延迟、健康率、队列长度 + `locked` 标记是否锁定期间采集），存内存 `deque`（`StandbySample` NamedTuple 紧凑存储），按 `METRICS_WINDOW` 时间窗口清理过期点；**延迟只统计无连续失败（`consecutive_fails == 0`）的成员**——latency 是最近一次成功探测的响应时间，失败成员保留着旧值，计入平均会让延迟显得比实际好（失败期间没有可用延迟），与健康率同口径；`/api/status/history`（`?since=<ts>` 增量拉取，另带 `retention_window` = `METRICS_WINDOW`）下发历史，前端累积重绘——**刷新页面不丢失趋势**（多标签一致），重启清空（仅运行时观测）。**心跳动态调整时采样频率跟着变、旧点按时间自然过期，留存窗口始终 = `METRICS_WINDOW`**（全局留存时长，与流量共用；`STANDBY_CHART_WINDOW` 只影响队列页显示范围，不参与留存），无需重建缓冲区。注：队列指标与流量历史体量相同（1800s / 2s 采样 ≈ 900 点），均已从 `/api/status` 分离（该端点仅返回实时状态 ~6KB），避免 2s 轮询重复传输。
- **图表渲染（浏览器 CPU）**：仪表盘 3 个趋势图（延迟/健康率/流量）为 ECharts 实例，`animation:false` + option 全量 `setOption`（1s 采样下最坏 ≈1800 点/序列，按服务端留存时长裁剪而非点数上限），组件 `ChartBox.vue` 统一 init/resize/dispose；历史由 statusStore 增量合并（`/api/status/history?since=`，响应带 `retention_window` 供前端按时间裁剪本地缓冲——采样节奏可变，点数上限与留存时长不等价），option 为纯 computed——旧手写前端的增量渲染状态机与全量重建 CPU 高峰问题随架构消失。流量图的平均延迟按 ts 匹配最近的前一个队列采样点（两序列节奏不同、不能按索引对齐，见 `utils/series.matchPrevAvg`；容差取参考序列自身的一个采样周期，写死常数在锁定态会漏配）。坐标轴约定（`chartOptions.ts`，纯函数单测）：**对数轴上的 0/负值既不绘制也不参与值域**（空闲时差分为 0 的字节/s 会把整条对数轴的值域拖到 0，`log(0)` 取不到界 → 刻度与轴名一起消失；线性轴保留 0），**某轴整列无可绘制数据时套兜底区间**（对数 0.01-10 / 线性 0-1）让空轴仍留有可读刻度；三张图左轴均带单位名（`延迟 s` / `健康率 %` / `字节/s`），对数刻度文本按有效位格式化（`toLocaleString` 默认最多 3 位小数会把 1e-4 显示成 `0`，对数轴上读到 0 是错的）。
- **流量统计（traffic）**：统计状态与口径全在 `TrafficStatsUseCase`（use_cases 层）——`PproxyServer` 的 `modstat` 钩子只做转发：`i∈{0,1}` 上行 / `i∈{2,3}` 下行 → `record_bytes`，`i∈{4,5}` 连接建立 `+1` / 关闭 `-1` → `record_conn`；host 归一化与空值判断也在用例内（调用方只透传 pproxy 给的原始 `host_name`）。**独立采样循环固定 2s**（`serve` 的 `traffic_sample_loop` 调 `traffic.sample`）：差分出瞬时速率与 EMA、按 `METRICS_WINDOW` 清理 pair、把 pair 增量并入待落盘历史。采样与心跳节奏解耦：锁定单一上游时心跳放宽到 `HEARTBEAT_LOCK_INTERVAL`（见 `config/serve.toml`）但流量照常 2s 采样（正被使用的锁定代理流量图不跟着变稀疏），探测阻塞/退避期间采样也不中断；`/api/status/history` 下发 `traffic_history` + `traffic_total`，前端从累计值序列差分算速率曲线。注：pproxy `modstat` 只统计 `channel` 转发的 body，请求头在 `connected` 回调里直接写不经计数（GET 请求头不计入上行，POST/CONNECT body 正常计数）。
- **按代理 IP 流量统计（per-proxy traffic）**：pproxy（vendor/pproxy 本地维护）`stream_handler` 选定上游 `roption` 后调 `modstat(user, remote_ip, host_name, roption)`——原版 pproxy 不传 `roption`，已内化进 vendor 源码（`server.py` stream_handler 签名 + 调用点）。`modstat` 里读 `roption.bind`（上游代理地址；显式 `direct://` 上游为 `"DIRECT"`，本项目默认无直连故无回退直连项）作 key 分发到 per-proxy 计数器；`/api/proxy/history?bind=` 按需查流量历史（`TrafficSample` 序列，前端差分算速率）。累计值与采样序列都在 `TrafficStatsUseCase` 内，仅内存、重启清零，不持久化（累计值不再随 `/api/status` 全量下发）。
- **历史流量条目与流量统计页（traffic history）**：`(代理, 域名)` 的采样增量按 `panel.toml [traffic].flush_interval` 节流批量落到 `traffic_history` 表，主键 `(bind, host, ip)`——**IP 变动即新条目**（旧行冻结在切换前的累计值，从而能看出同一域名经不同出口的 CDN 漂移落点）。geo 不冗余存，按 `ip` 关联 `ip_geo_cache`；**不能关联 `target_geo_cache`**（其主键是 `(bind, host)`，IP 一变旧落点就被覆盖）。写入是**增量累加**（SQL 侧 `upload = upload + excluded.upload`）：进程重启后内存增量归零也不会冲掉已落盘的历史。保留期与清理见 `[traffic]` 节（每小时的采样轮次顺带清理）。面板「流量统计」页两个选项卡：**实时**（`/api/traffic/live`，连接级口径——此刻仍开着的连接，关闭即从列表消失）、**历史**（`/api/traffic/history`，按流量总计降序，条数上限见 `[traffic].history_limit`）。页面上只有这两个列表，没有页头 KPI 卡片（曾用 `/api/traffic/summary` 提供「活跃连接数 / 历史条目数 / 全局累计」，随仪表盘一并去掉，接口与用例的 `connection_count()` 同步删除，不留死接口）。
- **按代理 IP 延时历史（per-proxy latency history）**：`heartbeat`/`retry_probe` **每次探测都记一笔** `ProxyLatencySample(ts, latency)` 到 `dict[bind, deque]`（key 为 `bind_of(proxy)` 剥 scheme，与 per-proxy 流量统一 key），**失败记 `latency=None`**——失败也是探测结果，缺采样点会把曲线画成直线、也算不出成功率；按 `METRICS_WINDOW` 窗口清理过期点；**代理离开队列后其 bind 的 key 在每轮心跳开头回收**（留存集合 = `standby_members()` + 锁定代理，与面板队列行一致），key 不随历史累积；`/api/proxy/history?bind=` 单代理按需查询、`/api/standby/series?since=` 队列逐代理增量下发（见下）。仅内存，重启清零。
- **队列逐代理序列（`/api/standby/series?since=`）**：队列页行内趋势图的数据源，一次返回队列（含锁定代理）每条的延时与累计流量序列 `{cursor, window, series: {bind: {lat, up, down}}}`；`since` 为增量游标（只返回 `ts > since` 的点），`window` 为**队列页图表的显示窗口**（秒，= `STANDBY_CHART_WINDOW`，只服务队列页行内图，不参与数据留存）——前端本地缓冲按它裁剪，不在前端再硬编码一份（否则改配置后两边窗口各自漂移，显示跨度对不上）；渲染前降采样。仪表盘不走此接口（走 `/api/status/history`，按真实数据点绘制、不裁剪）。逐代理全量体量适中（30 × 2 序列 × 留存窗口内采样点），故与 `/api/status/history` 同机制走游标增量，且**不进 2s 的 `/api/status` 热路径**；两条历史都不产新数据时不下发空序列。
- **队列行下质量带（StandbyPage.vue + Sparkline.vue）**：每个代理一块（摘要行 + 行下质量带）。摘要行是序号/代理（地址 + 协议·归属地·入队时长 + 纯净度 tag）/延迟/连续失败/操作（纯净度·测速·固定·锁定）；质量带**无逐项显示开关、四块全部常显**——延时趋势、流量速率、纯净度与测速结果都在，没有采样的块显示"暂无采样/未检查/未测速"占位（进页面即全部可见，不需要点开关或悬浮）；面板只保留「收起/展开图表」一个密度开关（收起回到单行便于纵览，跨会话记住）。**行内趋势图为自绘 SVG**（`Sparkline.vue`，`sparkSegments` 纯函数产出坐标串）：x 轴按**时间戳**映射（探测失败期留真实空白，`null` 点切断为多段）、对数/线性 y 轴自适应、多序列共用值域；**时间窗口由页面统一给定**（`seriesTimeWindow`：起 = 全行最早采样点，止 = 现在），全部行及同一行的两条曲线共用同一原点——各行按自己点集归一化的话，"刚入队只有两点"与"跑了半小时"的行长得一样、采样已停止的行也会贴到右边缘装成最新；孤立单点段写两遍同一坐标（浏览器不绘制单点 `<polyline>`，零长度子段的圆头端帽才显示出那个孤立的成功点）；两张图左侧各留一列标出该图的数据值区间（上 = 最大、下 = 最小，与坐标映射同源 `sparkDomain`，迷你图没有坐标轴不标就读不出量级），只要给了格式化器就占住该列（避免绘图区在行间错位）；**流量速率的非正值置 null**（既不绘制也不参与值域，零流量不再画成落在图表中间的平线、也不把尖峰压平，与仪表盘流量图同规则），累计值仍由「累计 ↑x ↓y」文字承担，故"无速率"与"无采样"可区分。不用 ECharts——几十行各建一个实例的开销（option 解析 + 组件体系 + ResizeObserver）不可接受，ECharts 只用于仪表盘大图。**代理质量由后端心跳机制自动管理**（失败计数→退避补测→拉黑淘汰），前端不做主动质量标记/高亮（曾有的 qualityFlags 低质量标签与色条已移除）。
- **队列归属地（`/api/standby/geo`）**：按 `bind` 下发国家/省/市，前端 5 分钟节流刷新（`useStandbyGeo`，由 2s 状态节拍驱动、不另起定时器）。归属变化以月计，故不进 2s 热路径；实现走 `IpGeoUseCase.resolve`（主线程读缓存 → executor 定位缺失 → 主线程写缓存，三步各自降级），`geoip.enabled=false` 时不注册该路由（前端按 `geo_enabled` 跳过）。前端缓存跨路由保留，每次刷新按当前队列的 `bind` 集合淘汰离队条目（`pruneStandbyGeo`，与 `useStandbySeries` 的 `pruneBuffers` 同一口径，防随驻留时长无界增长）；**保留集为空时不淘汰**——本函数有 5 分钟节流，进入页面首帧队列尚未填充即清空缓存，所有行的归属地列会空窗到下一拍。
- **顶栏轮询与连接态（stores/status.ts + App.vue）**：`statusStore` 固定 2s 轮询 `/api/status`；顶栏暂停按钮停轮询、页面切后台（`document.hidden`）同样停（与日志面板同一可见性口径），切回前台立即补一次——历史走 `?since=` 游标增量，后台期间的点不丢也不重复；暂停期间顶栏连接态显示**「已暂停」**（中性色第三态）——不发请求就无从判断连接，不能沿用暂停前的「已连接」装作后端还在（后端此刻可能已挂）。轮询有三重保护：① 定时轮询（`tick`）带在途门，上一轮未回时跳过本轮，避免后端僵死时每 2s 叠一个请求；② `/api/client` 的 `request` 统一带 15s `AbortSignal.timeout`（后端 TCP 连上但不回包时请求最终会失败并走重试路径，不会永久挂起）；③ 请求序号只接受最新一次响应，丢弃晚到的旧快照——写动作后 `refreshNow` 取到的新队列不会被另一个更早发出的轮询回来覆写成旧值。用户触发的补拉（切回前台、`refreshNow`）**不走**在途门：它们要的是"立刻取一次"，被在途请求挡掉会让界面与 toast 对不上。
- **手动拉取中间态**：`POST /api/fetch` 的进行中状态由后端保存（`web/panel_state.py` 的 `PanelTasks`，内存）——`/api/status` 下发 `fetching_all`（全量拉取）与每源的 `sources[].fetching`（单源拉取，全量拉取时所有源为 true），前端以**后端状态**为准（本地点击态只补点击到下次轮询之间的空档）——**刷新/重开页面不丢失「处理中」**，重启清空（拉取随进程结束）。后端幂等：已有拉取在跑返回 `{"status": "running"}` 不重复起任务；拉取任务与请求协程解耦（客户端刷新/关页会取消请求协程，已拉到的数据仍照常入池），中间态在任务结束时由任务自身清除（若在请求协程侧清除，刷新会让面板提前显示空闲并放进第二个拉取）；**选源（`plan`）在闸门之前**：源名非法直接 400 且不占闸门（源名非法是请求本身的问题，不该落到 500，也不该与「源存在但暂停中所以本轮无事可做」混同）；非 `ValueError` 的选源异常由 aiohttp 兜底成 500——此时闸门从未被占，无需在异常路径里收尾。
- **单代理测速（pspeed 集成）**：`POST /api/proxy/speedtest` 端点按 bind 反查完整 proxy URL（standby 队列 + 锁定代理），调 `src/interfaces/speedtest.py` 的 `PspeedTester.measure`（pspeed `probe_one`，Cloudflare 下载+上传节点，10s `hard_limit`），`asyncio.to_thread` 包装避免阻塞事件循环；实现下沉后与入队筛选测速指标共用同一实例（筛选侧走带 1h 缓存 + 线程锁的 `download_mbps`，面板手动测速不读缓存）；返回精简后的 `{download: {...}, upload: {...}}`（含 `ok`/`err`）。失败腿在行内渲染为「失败」并把原因挂到 title 上（两腿都失败时徽标转红），未测出不再被显示成 `0.0`。pspeed 源码在 `vendor/pspeed/src`，通过 `sys.path.insert` 导入，依赖 requests + PySocks（已在 requirements.txt）。**仅手动触发**（操作列「测速」按钮或右键菜单），结果就地展示在行下质量带（`useSpeedTest` 按 bind 保留结果），不做后台轮询——30 个代理串行测速约 5 分钟且吃带宽。**任务状态由后端保存**（`web/panel_state.py` 的 `PanelTasks`，内存）：进行中（running）/结果/失败按 bind 记录，`/api/status` 每行下发 `speedtest` 字段，前端 `speedTestEntryOf` 以后端状态优先——**刷新页面不丢失"测速中/结果"**；后端幂等（同一 bind 进行中重复请求返回 `{"status":"running"}`，不重复起任务）。服务重启清空（测速为瞬时重操作），前端刷新后自然回落"未测速"。
- **纯净度检查（插件式检查源）**：`POST /api/proxy/purity` 按 bind 反查完整 proxy URL 后提取 IP，三步编排收敛在用例（`CheckPurityUseCase.resolve/inspect`：主线程查 SQLite 缓存 → executor 调检查源 → 主线程写缓存，线程安全），缓存命中（`ippure.toml` 的 `cache_ttl`，默认 30 天）直接返回；Web 端点保留缓存读取以在调 API 前做「同 IP 检查中」的幂等门控。检查源为**插件**（`src/interfaces/ippure/`：每个模块暴露模块级 `SOURCE` 元数据与 `create_checker` 工厂），由 `ippure.toml` 的 `[[plugins]]` 显式列出模块路径、`[ippure].source` 选中其中一个；结果统一为通用契约 `{score(0-100，越高越脏), usage_type(核心 `UsageType` 枚举，插件负责映射各自源的原值), extra(源特有字段)}`，核心与前端不出现任何具体源的名字、端点或字段名——新增源 = 加一个插件模块 + `ippure.toml` 两行。缓存按 `(源名, IP)` 分区（换源不复用旧源结果）。**密钥统一走环境变量**：`config/.env` 在 config 加载时注入 `os.environ`（已存在的真实环境变量优先），插件按 `SOURCE.env_keys` 声明所需变量，缺失时警告并禁用该功能（不阻断启动）；配置类错误（未知源名/模块导入失败/节未被任何插件声明/插件参数非法/规则非法）直接启动报错。**声明式代理**：`[[proxy_rules]]` 按 `match`（源名 glob，`*` 通配）取首个命中的 `use` 作为该源出网代理，无命中即直连。**中间态由后端保存**（`PanelTasks`，**键为 IP**——同 IP 多端口行共享幂等，防重复调有日限额的源）：checking/error 经 `/api/status` 每行 `purity_state` 字段下发，前端 `purityEntryOf` 按"后端终态（缓存结果/失败）→ 中间态（检查中）→ 本地 Map"解析——**刷新页面不丢失"检查中/失败"**，结果由 SQLite 缓存跨重启恢复；检查完成写缓存后清除中间态。`/api/status` 另下发 `purity_source`（`{name, label}`，null = 未启用），前端据此隐藏检查按钮、质量带显示"纯净度未启用"。
- **队列行顺序冻结与写动作回正（StandbyPage.vue）**：面板队列行顺序在**选中/拖拽/悬浮**期间冻结（`utils/merge.ts` 的 `mergeKeepOrder`：old 顺序优先、离队项移除、新入队项追加末尾），避免 2s 轮询把行从光标下挪走——行高翻倍后"读一行图表时行跑位"的影响更明显；而**固定/解固/锁定/解锁**这类用户显式动作成功后调 `store.refreshNow()`（忽略暂停，立即取 `/api/status`）并把渲染列表重置为下发顺序——否则选中态会让冻结永不解除，toast 已称「代理已移到队列最前」而列表原地不动。
- **右键上下文菜单 + 代理移组/删除/添加**：代理服务队列页面右键代理行弹出菜单（`StandbyPage.vue` + `ContextMenu.vue`）——「移出」hover 展开子菜单列出所有组（排除 standby 自身和 blacklist），点击调 `POST /api/proxy/move`（批量 `proxies` 数组，`pool.move_proxy` + sync pproxy 上游）；「拉黑」调 `POST /api/blacklist`（`record.remove` 从所有组移除并加入黑名单 + sync pproxy）；「测速」立即触发该代理测速（结果落在行下质量带里，**多选时菜单不展示测速项**）。代理组页面（`GroupsPage.vue`）右键代理行弹出菜单——非黑名单组：「移动」（子菜单选目标组，排除当前组和黑名单）+「拉黑」+「删除」（`POST /api/proxy/remove`，批量 `proxies` 数组，`pool.remove_from_group`，standby 组删除后 sync pproxy）；黑名单组：「还原」（按 source_group 各自回来源组）+「删除」。菜单点击外部/缩放自动关闭，滚动也关闭但**菜单内部滚动不算**（子菜单比视口高时可滚动，否则拖自己的滚动条会把菜单关掉）；摆放规则等价 Win32 菜单管理器（`utils/menuPosition.ts` 纯函数 + 单测）：主菜单以光标为锚点越界整体平移进视口，子菜单右侧放不下翻到父项左侧（箭头镜像）、下方放不下上移、比视口还高则限高内部滚动；视口取 `clientWidth/Height`（排除滚动条，对应 Win32 的"工作区"）。坐标写进组件内的**受控 `pos` 状态**而非直接绑 props（菜单已打开时再次右键只走重渲染，会把夹取结果覆盖回原始点击点而溢出视口）；子菜单展开由 JS 驱动而非 CSS `:hover`——**必须先渲染出来量尺寸再定位**，`:hover` 展开时量不到尺寸（量完前 `visibility:hidden`，无错位帧），触屏走同一个开合状态的点击路径（tap 合成的 `pointerenter` 按 `pointerType` 跳过，避免与 click 重复开合）。代理组页面「添加」按钮弹出模态框，`POST /api/group/{name}/add` 批量添加代理（裸地址自动补 http://，standby 组添加后 sync pproxy）。
- **列表框选/多选（useRowSelection.ts）**：两组列表行选中复用 `useRowSelection` composable（Windows 资源管理器式）——空白处/表头/行上按住拖拽出蓝色矩形框选（`marquee-select` 半透明蓝，`pointer-events:none`），覆盖行按数据索引选中（虚拟滚动跨未渲染区域用 vue-virtual `getVirtualItemForOffset` 换算索引）；单击行单选、Ctrl+单击切换、Shift+单击锚点范围、Ctrl+拖拽切换框内、Ctrl+A 全选（`isSelectionDisabled` 为整体禁用开关：队列页锁定单一上游时鼠标**与键盘**都不产生选中——只挡鼠标会被 Ctrl+A 绕过，灰显行会叠上选中高亮并一直冻结列表顺序）、Esc 清空、点击列表外部空白清空选中（document mousedown 排除按钮/输入/下拉/浮动层）；拖到视口边缘自动滚动。选中集 `Set<proxy>` 为响应式状态，行高亮由模板 class 绑定驱动（`tr.selected` 蓝底 + 左缘指示条），列表重渲染天然保持。
- **队列页无浮动层**：代理统计不再用 hover 浮窗——延时/流量趋势与质量指标全部内联在行下质量带（见上），悬浮只用于冻结行顺序（鼠标停在表内时行不跑位）。2s 轮询重渲染不会打断文本选区（Vue 仅 patch 变化节点），旧版"选区冻结"补丁与 `useProxyTooltip` 链路已整体删除。
- **本地链路门控（network gate）**：fill/heartbeat/retry_probe 在探测前和探测后各调一次 `LocalNetworkGate.check()`，两次都必须正常才允许执行正常拉黑操作——探测前不可用则跳过本轮（不计数/不拉黑/不补测），探测后复检不可用则本轮失败不归因到代理，队列保持现状、恢复后自动续跑。判定：check 并发直连多个探测目标（`network_probe_urls`，任一返回状态码 <400 即正常——既容错单目标抽风/被墙，也不会被中间设备/门户的错误页骗过），并发调用共享同一次在途探测（不重复直连）。**结论缓存是单向的**：只缓存"不可用"（`NETWORK_UNHEALTHY_CACHE_TTL` 内直接复用、不再探测），"可用"每次真实探测——陈旧"可用"会让探测后复检误判为链路正常而把失败归因到代理（本末倒置，删的是好代理）；失败结论只会让本轮跳过，复用只花时间，同时抑制了补测任务 1s 一轮的轮询放大（缓存期内不发起探测，恢复感知最慢即该时长）。必要性：心跳 2s 一轮且失败候选每轮重复探测，5 次连续失败只需 10~20 秒（生产日志实测中位 13.7~17.2s）即触发 `remove_all_copies` 真删，链路抖动必须拦在这一层。retry_probe 在链路不可用时暂停补测（暂停/恢复各只记一次日志）并重置自身退避快速轮询门控（轮询命中失败结论缓存，不产生外网请求）。
- **Web 面板代理组虚拟滚动**：代理组页面（`GroupsPage.vue`）对大量代理（数千条）用 `@tanstack/vue-virtual` 虚拟滚动——`scrollMargin` 对齐列表在滚动内容中的起点，上下 spacer 行保持滚动条总高，行高动态测量（`measureElement`），overscan 缓冲；`#main-content` 设 `overflow-anchor:none` 禁用浏览器 scroll anchoring 避免干扰；右下角悬浮回到顶部按钮。**关键不变式**：`scrollMargin` 必须严格等于「列表起点在滚动内容空间的偏移」= 直接量 `tbody` 顶（`updateScrollMargin`），**不能减去顶部 spacer 高**——spacer 是 `tbody` 的首个子元素、只把渲染行往下推，且 vue-virtual 的 `item.start` 已含 `scrollMargin`、区间判定直接拿 `scrollTop` 与之比较；多减会让滚动状态下的每次改窗口尺寸都往下漂移一个 spacer（切组那次因加载骨架把 `scrollTop` 夹回 0 而未显形），导致列表整屏空白 + 总高膨胀（回归用例 `TestGroups::test_scroll_margin_survives_resize`）。
- **组名标签映射**：`_status_snapshot` 为每个组下发 `label`（中文显示名）+ `kind`（语义类型 `trusted`/`blacklist`/`standby`/`normal`），从 config 的 `TRUSTED_GROUP`/`BLACKLIST_GROUP`/`STANDBY_GROUP` 派生——前端不硬编码组名，改配置即生效。`GroupsPage.vue` 用 `label` 显示下拉框 + 来源列，`DashboardPage.vue` 用 `kind` 查找 KPI 卡片对应的组。
- **控制台日志面板（LogPanel）**：侧边栏底部按钮启停的日志视窗（vendored Vue 3 + TypeScript 组件，位于 `frontend/src/vendor/logpanel/`，样式走 scoped CSS + `--lp-*` 变量；契约 source/rules/hooks 由宿主注入，可整体复用到其它 Vue 3 项目）。**双形态**：桌面为悬浮窗（px 几何、可拖拽缩放），窄屏/横屏手机自动切**底部抽屉**（`form` prop 默认 `auto`，可显式锁定）。三条轴严格正交：**形态**看视口宽度（`SHEET_MEDIA_QUERY = (max-width:640px), (max-width:900px) and (max-height:480px)`，宽而矮的桌面窗口仍用悬浮窗）、**触控**看主指针类型（`(pointer: coarse)`：目标尺寸、长按、`-webkit-touch-callout`）、**视口度量**（悬浮几何用布局视口——不受捏合缩放影响，几何持久化才不会被写坏；抽屉高度与键盘内缩用可视视口——软键盘只改它）。形态与触控都落到根节点属性（`data-form`/`data-detent`/`data-touch`）驱动 CSS，不在组件间透传 props；`resize` 开关在抽屉下把关档位切换（关掉则固定半屏且不渲染抓手），`drag` 只对悬浮形态有意义。后端 `src/framework/logbuffer.py` 的 `PanelLogHandler` 在 `setup_logging()` 挂到 root logger（级别 DEBUG，与 stderr「控制台」通道一致，逐 logger 覆盖天然同步），镜像进 2000 条有界环形缓冲，entry 带单调 `seq` 游标与异常 `stack` 字段。API：`GET /api/logs?since=<seq>` 增量拉取（返回 `entries` + `cursor` + `buffer_start`），`POST /api/logs/clear` 清空缓冲（不影响 stderr/文件）。前端 `useLogPanel.ts` 以 1s 轮询增量合并本地缓冲（`appendEntries` 有界裁剪）；**面板隐藏（v-model:visible=false）或页面切到后台（visibilitychange）都立即停轮询，零 /api/logs 请求**，恢复时启动并立即拉一次；`needsFullReload` 检测增量失效（serve 重启 seq 回绕 / 缓冲淘汰越过游标）时全量重拉并清空重绘；主题经 `.lp-root` 把 `--lp-*` 全量映射到 `--color-*` 令牌自动跟随亮暗（组件默认值写在 `--lp-*-c` 解析层，宿主在组件根或任意祖先设变量均可生效）。模块标签前端剥 `src.` 前缀，时间显示 `HH:MM:SS.mmm`，`rules.key` 用服务端单调 `seq`。
- **LogPanel 渲染管线**（组件内部）：视图状态机（`composables/useLogView.ts`）订阅源的增量信号、按帧重读缓冲快照，过滤/搜索/统计全部由 computed 派生；**暂停只冻结快照**（含统计），恢复时一次性补齐；条目经 `hooks.onEntry` 变换后方进入视图。搜索在 `utils/filter.ts` 里创建匹配器时编译一次正则（逐条重建正则是过滤的主要开销），正则非法与 UI 标红一致地降级为字面量。虚拟列表用 `@tanstack/vue-virtual`：`anchorTo: 'end'` 让头部淘汰/末尾追加后按 key 锚定视口首行，滚动容器 `overflow-anchor: none` 避免浏览器滚动锚定与库互相拉扯。**行高必须全量预量**：正文换行使行高不一致，只量可视行会让未渲染区域永远按估算值撑高，总高随滚动被不断修正——原生滚动条滑块按 `scrollTop/scrollHeight` 映射，总高一变滑块就与鼠标位置脱节（表现为拖动滚动条"跟不上鼠标"）；组件因此用隐藏测高池（零高 + 裁剪，宽度与行一致）把每行按批（64 行/批、单帧 10ms 预算）量一遍 `offsetHeight`，按当前视图下标 `resizeItem` 写回库内尺寸缓存，未量行先用已量行的累计均值兜底，测完后总高恒定（宽度变化作废重测）。**行宽不足时切紧凑布局**：行默认是单行流式（展开图标/时间戳/等级/模块/正文同排，正文 `flex:1;min-width:0`），行宽被元信息吃光时正文只能占右侧一条窄缝、频繁换行；判据取**行自己的渲染宽度**（`entry.contentRect.width`，即滚动容器内容盒宽，含滚动条占宽；面板宽 ≠ 行宽——悬浮形态侧栏是 151px 左栏，面板 560 时行宽只剩 399，按面板宽判断会漏掉最坏情况；宽度为 0 的隐藏态跳过，否则 `v-show` 收起会被误判成窄行、重新显示时多切一轮模式把滚动位置带偏）。命中即把 `data-compact` 挂到 `.lp-list` 上由 CSS 把正文压到独立整行（元信息独占首行，模块名过窄时先省略号截断）——可见行与测高池都在该容器内，两处布局天然一致，不透传 props（漏传一处就会静默量错行高）。这条轴是**行宽轴**、与形态/触控正交：同一形态下窗口拖窄也会切。**翻转必须整表作废重测**：`heightId` 不含布局模式，不重测测高池会认为「已量过」而永不重测、错误行高永久固化；`remeasure()` 同时清空累计均值（跨布局的旧样本会把兜底高留在旧值上，清零后首批实测即收敛到新布局）。阈值 `COMPACT_MAX_WIDTH = 520`（正文只剩约 28 字符时切换）。**两处必须注意**：`getItemKey` 要与 `count` 绑定同一份快照（若闭包读实时数据，库比对首尾 key 时旧 options 也会读到新 key，环形淘汰被判成"无变化"而不做位置补偿）；**贴底跟随不能用库的 `followOnAppend`**——Vue 适配器在选项变更时立即调 `_willUpdate`，此刻本框架尚未提交 DOM（撑高块还是旧高度），按 `end` 算出的目标偏移是过期的，写回反而把视口拉到上面，因此跟随在本次提交后（`nextTick`）由组件自己发起，并在**内容高度变化时（ResizeObserver 观察撑高块）重新贴底**——新条目之外，行高实测修正（估算 → 实测）同样会撑高内容，只靠条目变化触发会在测高收敛后停在离底若干像素处。跟随意图由滚动手势维护：上滚即停、滚回底部即恢复；判定直接量 DOM（库内 scrollOffset 由其自身滚动监听更新，与组件监听器的执行顺序不确定），程序侧滚动只发生在跟随或锚定语义内，不会把用户手动关闭的跟随顶回去。**判定要隔一帧复核**：内容刚增长时位置还没跟上（程序侧贴底在同帧稍后才写），此刻"不在末尾"并不等于用户上滚，不隔帧复核就会在测高修正期间把跟随意图误关（实测负载下必现，表现为跟随被关、日志不再自动滚动）；一帧后仍不在末尾才算用户上滚。贴底落点用库的 `scrollToEnd`，另外两种写法都会破坏跟随：`scrollToOffset` 会在库里留下一个**固定偏移**的 reconcile 状态，内容继续增长时把视口反复拉回旧偏移；直接写 DOM 则库里缓存的偏移滞后，它自己的尺寸修正会把视口写回错误位置（实测拖高窗口后停在离底 102px 处）。**抽屉形态另有三条**：① 重测只由「渲染宽度」驱动（抽屉取布局视口宽、悬浮取几何宽；`watch([renderedWidth, interacting])`），高度变化（档位切换、软键盘、地址栏伸缩）与换行无关不该触发全量重测，手势中跳过、抬手补一次；② 滚动容器自身尺寸变化也要贴底（同一个 ResizeObserver 同时观察撑高块与 `.lp-list`）——窗口或抽屉变高时 `scrollTop` 不变而可视高度变大，跟随态下末行会落到视口下方，此时内容高度没变、只盯撑高块发现不了（这也是修掉了悬浮窗拖高后不贴底的老问题）；③ 档位拖拽改的是**高度**而不是位移：抽屉底边钉在屏幕边缘，用 transform 整体上移会让面板抬离底边、下方露出空隙（实测回归用例 `test_drag_keeps_bottom_pinned`）；拖拽中关掉高度过渡跟手即时变化，抬手由 `height` 过渡从手指位置平滑滑到目标档位（早期只过渡 height、位移走 transform 的写法会先瞬移回原档位再播动画）；拖拽锚点取**起手点**而非越过死区的位置，否则整段拖拽都与手指差一个死区距离。④ **尺寸只由触控轴决定，形态轴只做布局不改尺寸**：早期版本对标题栏/图标按钮/搜索框各放大两次（触控一次、抽屉又一次，标题栏 30→34→46），实测两端尺度差 1.5~1.6 倍、观感"手机端大一档"；现在触控轴只把可点目标抬到手指可用（标题栏 32、图标按钮 26、操作按钮 28、展开图标 16），抽屉轴只管搜索独占首行 / 按钮图标化 / 横条侧栏 / 空间分配（第二行要在 360px 窄屏的 344px 里塞下 4 个等级 chip + 筛选开关 + 3 个操作，chip 与按钮在那里收窄是宽度所迫）。唯一无法消除的差异是搜索输入 16px——iOS 聚焦小于 16px 的输入框会整页缩放。另：组件内统一 `box-sizing: border-box`（`.lp-root :deep(*)`），该属性不继承、只在根节点设会让各 part 声明的尺寸被 padding/border 撑大（抽屉标题栏声明 46px、实际渲染 55px）；工具栏折成三行会吃掉抽屉四成高度、日志区只剩一半（回归用例 `test_sheet_chrome_leaves_room_for_logs`）；日志正文字号两形态统一 12px（回归用例断言两形态 computedStyle 相等）。抽屉的档位与筛选条开合单独持久化（`sheet_detent`/`side_open`），悬浮几何继续用原 `geom` 键——两套尺寸互不污染，窄屏的尺寸不会写进桌面窗口的几何。安全区垫在根节点（`--lp-safe-*` 解析层 + 宿主 `viewport-fit=cover`，否则 iOS 上 `env()` 恒为 0），面板内 Toast 因绝对定位基准是 padding box，需自己让开安全区。

```
浏览器 ──▶ 本地 pproxy(127.0.0.1:8082) ──rc 随机──▶ 代理服务队列上游(存活代理)
                                                    ▲
                              fill 补入 / heartbeat 探测（并发，严格校验证书）
```

## 代理源

通用 HTTP API 源继承 `HttpSourceFetcher` 基类（`sources/base.py`）：URL 构建 + 重试直到非空 + scheme 前缀补全。重试是全源唯一实现 `retry_fetch / retry_fetch_async`（同策略的同步/异步双版：环绕“请求 + 校验 + 解析”的 attempt，日志口径与尝试耗尽语义一致）——github_lists（非子类）与 freeproxy_world（异步）同样复用，不各自写循环。基类类属性 `BASE_URL / DEFAULT_PARAMS / RETRIES / TIMEOUT / PROTOCOL`，子类提供 `_parse`。

| 模块 | 机制 |
|---|---|
| `sources/ip89.py` | 89IP，纯文本响应，正则 `\d+\.\d+\.\d+\.\d+:\d+` 提取；每源可指定 `num` |
| `sources/goodips.py` | Goodips，JSON 接口 `code==0` 时 `data` 为 `[{ip, port}]` |
| `sources/proxy_scdn.py` | ProxyScdn，JSON 接口 `code==200` 时 `data.proxies` 为代理列表 |
| `sources/geonode.py` | Geonode，JSON 分页接口（每页 limit 条），逐页拉取直到空页 / 不足一页 / 达到总数；只保留 `protocols` 含 http 的代理 |
| `sources/freeproxy_world.py` | 见下（多阶段爬取） |
| `sources/github_lists.py` | 见下（GitHub 静态列表，5 仓库 8 条列表合并） |

### FreeProxy.World 多阶段爬取

先获取可用的 **HTTPS 中转代理**，再用中转代理爬取三类代理页（异步实现：asyncio 调度 + 请求经 `asyncio.to_thread` 投递）：

1. **Phase1**：直连请求 HTTPS 代理第 1 页（获取中转代理 + 总页数）；
2. **Phase2**：用 HTTPS 代理并发（16）爬取 HTTPS 第 2..N 页；失败页面用未使用代理第二轮重试；
3. **Phase3**：用中转代理分别拉取 http/https/socks5 三类代理第 1 页（逐个代理尝试，全部失败回退直连——与 Phase1 共用 `_try_first_page` 重试循环）；
4. **Phase4**：并发爬取各类后续页（与 Phase2 共用 `_crawl_pages`，Phase4 按 round-robin 分配）。

- 验证过的中转 HTTPS 代理（working + unused）连同三类爬取结果一起去重返回（带 scheme 前缀），由拉取用例按 scheme 分拣入 `freeproxy-world-{scheme}` 组；
- 页面有效性校验：含 `<td style="font-weight: 500;">` 才算有效页（CF 墙/空页返回失败）；
- 走代理时用 requests（HTTPS 代理需 TLS 到代理）；直连用 `http_client`；
- 并发/限流/超时/请求头参数：`FPW_MAX_CONCURRENT=16`、`FPW_PROXY_RETRY=1`、`FPW_TASK_INTERVAL=2`、`FPW_REQUEST_TIMEOUT=15`、`FPW_ACCEPT_LANGUAGE="en-US,en;q=0.9"`，均由 `config/sources.toml` 的 `[fetch.freeproxy_world]` 注入；
- 该源产出的**端口 80 代理是 Cloudflare 假代理**（CONNECT 400），由 `[[fetch_filter]]` 的 `:80$` 规则命中后入黑名单。

### GitHub 静态列表（github_lists）

把各仓库的纯文本列表合并为**一个源**（proxifly 的 socks5 列表与 hookzof 同源同内容，只保留后者避免重复下载）：

1. 逐条列表拉取（每条重试到非空，单条失败只记日志并跳过，不影响其它列表）；
2. 按行解析：**条目自带 scheme 优先**（proxifly 自带 `http://` / `https://` / `socks5://`），裸 `ip:port` 按所在列表的协议补全（clarketm 的列表无协议字段，按 http 处理）；格式非法的行（表头、注释、无端口、不受支持的 scheme）直接丢弃；
3. 跨列表去重后合并返回，由拉取用例按 scheme 分拣入 `github-http` / `github-https` / `github-socks5`。

`raw.githubusercontent.com` 直连在部分网络被 SSL 中断，镜像前缀取环境变量 `GITHUB_LISTS_MIRROR`（未设置或为空即直连，代码不内嵌任何镜像地址）——用环境变量而非新增配置节，便于本机配镜像而 CI（GitHub 出口）保持直连。源的暂停时长与承载组的 TTL 见 `config/sources.toml` 与 `config/pool.toml`。

### 源启用/禁用

在 `config/sources.toml` 的 `[[sources]]` 列表中配置；`name` 为模块导入路径（同时是源暂停记录 key），`group` 为入组名，`group_by_protocol` 决定是否按 scheme 分拣，`pause_ttl` 为拉取后强制暂停时长。

## 数据持久化

`SqliteProxyStore` 读写项目根 `.cache.db`（WAL 模式 SQLite，组级增量 upsert）；文件缺失/损坏视为空状态。领域规则在 `ProxyPool.export_state() / restore_state() / export_group()`：

```json
{
  "proxy_groups": {
    "default-http": {
      "ttl": 3600,
      "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": 1700000000, "consecutive_fails": 0}]
    }
  },
  "source_paused": {
    "src.interfaces.sources.freeproxy_world": {"paused_at": 1700000000, "ttl": 18000}
  }
}
```

- `export_state()` 先清理过期条目再导出；
- `restore_state()` 恢复时：**组 ttl 以运行时配置为准**（改配置即生效，文件里的 ttl 不覆盖）；过滤已过期条目与已过期源暂停。
- CLI 各命令在 `finally` 中调用 `c.save_full()` 保证退出时落盘；serve 主循环用 `c.save()` 增量保存（只写 dirty 组，~0.3ms）。

## IP 归属地图（Web 面板）

Web 面板「IP 分布」页用 ECharts 世界地图展示池中代理的地理分布（城市级散点，点大小 + 颜色深浅 = 该城市 IP 密度）。

**开关**：`[web].enabled` 控制面板总开关（false 时不构造/启动 aiohttp 面板，且纯净度检查/geo 用例整体不构造——二者仅面板消费，其 requests/geoip2 依赖链也随之不 import）；`[geoip].enabled` 控制归属定位（false 时不加载 mmdb 定位器、不注册 `/api/geo/map` 路由，前端按 `/api/status` 的 `geo_enabled` 隐藏地图区块，组明细不带 `geo` 字段）。两者独立，均可按需关闭省内存。

- **定位引擎**：`GeoLite2Locator`（`src/interfaces/geoip_client.py`）封装 MaxMind GeoLite2 mmdb（默认 `vendor/geoip/GeoLite2-City.mmdb`，配置 `[geoip].db_file`），按库的 database_type 自动识别 City/Country：City 返回国家/省市/经纬度，Country 仅国家级（省/市/坐标恒 None，由用例层国家中心坐标兜底）；国家/城市名取中文（zh-CN）优先。
- **缓存**：定位结果持久化到 `.cache.db` 的 `ip_geo_cache` 表（`SqliteProxyStore.get_geo_batch/save_geo_batch`），TTL 由 `[geoip].cache_ttl` 控制（默认 90 天，IP 段归属变化以月计）；重复解析只发生在缓存缺失时。
- **用例**：`IpGeoUseCase`（`src/use_cases/ip_geo.py`）拆成三步供 API 编排——`collect_ips`+`query_cache`（主线程 SQLite 读）→ `locate`（executor 线程，纯 mmdb 读）→ `save_cache`（主线程 SQLite 写），遵守 SQLite 线程绑定约束；`aggregate` 为纯计算，按「国家+省+市」聚合出散点与国家级汇总。
- **API**：`GET /api/geo/map` 返回 `{total_ips, resolved_ips, unresolved_ips, countries, points}`，points 为城市级（City 库）或国家级（Country 库）坐标点（`{name, country, country_code, lat, lng, count}`）。首次请求全量解析 2400+ IP 约 3 秒，之后纯读缓存毫秒级。
- **前端**：`GeoMap.vue` 懒加载 `static/world.json` 注册 ECharts 地图，静态散点（`effectScatter` 波纹保留开关、默认关——动画每帧重绘整张地图致浏览器 CPU 高企）+ `visualMap` 颜色映射密度，主题切换 watch 重绘配色，roam（缩放/平移）状态在数据刷新间保留；尺寸自适应用 `ResizeObserver`（在 `onMounted` 同步建立、`onBeforeUnmount` 断开），**不用 `window` 的 `resize` 监听**——后者的 handler 若建在 `onMounted` 的 `await` 之后，组件在 await 期间卸载时清理钩子拿不到 handler，监听器会残留在 `window` 上；同理两个 `watch`（status 心跳、主题）必须在 **setup 顶层**注册，写在 `await` 之后会脱离组件 effect scope、卸载后仍存活（依赖 Pinia 单例 store，进出页面 N 次即 N 个常驻 watcher）。ECharts 为 npm 依赖（tree-shake 打进懒加载 chunk），地图数据随构建产物分发（无外网依赖）。

## 目标地区探测（3D 地球后端）

仪表盘 3D 地球展示「本机 → 代理 → 目标服务器」的实时流量分布。目标落点来自**穿对应代理做 DoH 解析**的结果：CDN 按出口就近调度，同一域名经不同代理解析到不同机房，只有从该代理出口发查询才能得到它的真实落点。

**开关与依赖**：`[globe].enabled`（默认 true）且 `[geoip].enabled=true` 时，serve 组装 `TargetGeoUseCase`（`src/use_cases/target_geo.py`）并注册 `GET /api/globe`；否则不构造用例、不注册路由，`/api/status` 下发 `globe_enabled=false`（前端隐藏卡片）。无面板（`[web].enabled=false`）时无人请求 `/api/globe`，探测永不激活。

- **数据源（PproxyServer pair 维度）**：vendor/pproxy 的 `modstat` 内化传入 `roption` 后，按 `(bind, host)` 记录会话 `last_seen` 与分方向字节；独立 2s 采样差分出瞬时速率并算 EMA（`up_rate/down_rate` 防抖供线宽、`up_inst/down_inst` 供光点流速），pair 超容量按 last_seen 淘汰、按 `METRICS_WINDOW` 清理。
- **激活窗口**：`/api/globe` 每次请求调 `touch()`；只有 `activation_window`（60s）内有过请求，`probe_cycle` 才真正探测——没人看地球时零外网消耗（只记录内存流量）。
- **探测循环**：serve 第三周期任务 `target_probe_loop`（2s，与心跳/采样并列），未激活直接返回；候选 = `probe_window` 内活跃 pair ∩ 当前队列（含锁定代理的 `queue_rows` 口径，`bind_of` 匹配）∩ 非跳过目标；按 last_seen 降序每轮取 `probe_concurrency * 2` 个、`Semaphore` 限并发；每 pair 每 `target_cache_ttl`（24h）探测一次，失败退避 60s 起指数翻倍封顶 15min、成功清零。
- **穿代理解析（DohResolver）**：`HostResolver` 端口实现，经该代理请求 DoH JSON 接口（`Accept: application/dns-json`），端点按 `doh_endpoints` 顺序回退（默认 Cloudflare → Google → alidns 兜底国内出口）；HTTP 200 合法 JSON 但无 Answer 是有效负结果（空列表，不回退——否则负结果会被端点放大成多次查询）。证书策略复用 `verify_target`（http/socks 端到端校验；https 代理 TLS 只到代理本身，校验会让免费代理全灭）。
- **跳过与特例**：内网/保留/回环/链路本地/组播/未指定 IP、单标签主机名、`.local` 在候选阶段过滤（不占探测名额）；公网 IP 字面量跳过 DoH 直接定位；IDN 编码失败的主机名跳过（不落盘不记退避）。
- **缓存与隐私边界（`target_geo_cache` 表）**：主键 `(bind, host)`，存解析 IP + 归属（国家/省/市/经纬度）与 `cached_at`；负结果行（ip/geo 全 NULL）照常落盘、TTL 内不重探；Geo 全不命中落 ip 行。每小时一次（内存节流）按 `prune_after`（30 天）清理过期行。目标域名会随 DoH 查询发给所配 DNS 端点（穿代理解析的必要代价），结果只落本机 `.cache.db`；`/api/globe` 的 `flows` 含目标域名，面板默认监听 `0.0.0.0`——局域网可见范围与现有日志/队列数据一致，可在 `[web].host` 收紧监听或 `[globe].enabled=false` 关闭。**清理方式**：`[globe].enabled=false` 只停新探测，已缓存的目标域名需停掉 serve 后清表（如 `sqlite3 .cache.db "DELETE FROM target_geo_cache"`，或用任意 SQLite 客户端删该表内容）；删整个 `.cache.db` 会一并重置代理池状态，一般不必。日常老化靠 `prune_after`（30 天）自动清理。
- **API**：`GET /api/globe` 返回 `{self, nodes, flows, stats}`（调用即 touch）：`self` = `[globe].self_lat/lng` 手填坐标（未配置为 null，前端不画第一段弧）；`nodes` = 队列每行（bind + 归属含坐标 + latency/pinned/locked/consecutive_fails + 按 bind 聚合速率）；`flows` =「活跃 pair ∩ 队列 bind」经缓存聚合的「代理 → 目标城市」弧（按 `(bind, 国家码, 城市)` 分组求和、hosts 每组 ≤8 个、按总速率降序取 `max_flows` 组，无坐标不产弧）。同一帧还带 `stats` 探测观测（累计 DoH 成功/失败、此刻退避中的 pair 数、最近一轮候选/发起/待探测计数与时刻）——只有计数、不含 host，用于评估穿免费代理的 DoH 成功率；面板地球卡片脚注渲染成「成功 N · 失败 M（成功率 P%）」，退避中/待探测仅在非零时附上。单次 <20KB，前端 5s 轮询，不进 2s 的 `/api/status` 热路径。
- **前端（`GlobePanel.vue`）**：globe.gl（含 three 链）在卡片进入视口后才加载（DashboardPage 的 `defineAsyncComponent` + 组件内动态 `import('globe.gl')`，主 chunk 不含 three，three 独立 chunk ~1.9MB/547KB gzip）；5s 轮询 `/api/globe`，不可见或页面切后台暂停动画并**停轮询**（停轮询即停止 touch → 后端停止探测）；球面样式（贴图/经纬网/大气光晕）由设置页「地球样式」卡片决定（纯前端偏好，`stores/globeStyle` + localStorage，改完即时生效）：贴图取 three-globe 官方示例资源、已降采样入库 `frontend/public/globe/`（4 张，经 `/globe/{name}` 白名单路由分发，随构建产物离线分发，不依赖 ECharts 的 world.json），默认「真实地球 + 无网格 + 无光晕」——官方深色贴图（earth-night/earth-dark）依赖深色星空底，浅色卡片上会显成暗球，而默认大气光晕是 alpha 混合的 lightskyblue、在浅色底上是一圈蓝雾；主题切换读 CSS 变量重建点位/弧配色；WebGL 不可用/初始化失败渲染降级文案；卸载时断开 IO/RO 并 `_destructor()`。数据映射与探测统计文案为纯函数（`globeData.ts`：两段弧、速率→线宽/流速、CSS 变量取色叠透明度、`formatProbeStats`），Vitest 单测覆盖。

## 性能与资源占用（serve 长期运行）

serve 长期运行时的资源特征与已落实的优化（避免周期性刷屏式资源消耗）：

- **日志降噪（磁盘写）**：心跳轮次/任务级日志（心跳探测、心跳成功、心跳完成、上游同步、调度器任务成功/失败/重试、锁定跳过填充）均为 DEBUG 级别——文件日志不再每轮写盘，仅事件级（补入/替换/拉黑/补测/锁定/解锁）与异常保留 INFO/WARNING。降噪前 3 小时写 ~42MB/30 万行日志，降噪后文件仅事件级输出。**控制台与文件分级**：级别由 `config/logging.toml` 决定（默认控制台 DEBUG 观察周期调度、文件 INFO 不刷盘），`[logging.loggers]` 可按 logger 覆盖（如 `aiohttp.access = "WARNING"` 抑制面板轮询访问日志落盘）；`--debug` 忽略配置、双通道强制 DEBUG 全量落盘。
- **状态快照瘦身（带宽/序列化）**：`/api/status` 只返回实时状态（~6KB），历史数据（standby_history 窗口 60s ≈ 2KB；traffic_history 窗口 1800s ≈ 200KB）改走 `/api/status/history`，前端首次全量 + `?since=` 游标增量拉取并本地累积——2s 轮询不再重复传输 200KB 历史（原每 2s 216KB，一天 ~9GB）。
- **锁定降频（外网请求）**：锁定单一上游时代理不淘汰不拉黑，心跳仅为测速展示，间隔放宽到 `HEARTBEAT_LOCK_INTERVAL`（30s），避免每 2s 打一次外网探测目标。
- **SQLite 写节流**：`save()` 30 秒节流 + 组级增量（`export_group` ~0.015ms），WAL 写约 9KB/s 级，无需进一步调整。
- **目标地区探测（外网请求）**：仅在面板打开时探测（激活窗口），每 pair 每 24h 一次 + 失败退避封顶 15min，并发 ≤ `probe_concurrency`（3）；`/api/globe` ≤ 30 nodes + 60 flows（单次 <20KB），5s 一拉，不进 2s 的 `/api/status` 热路径。
- **maxminddb Windows 非 ASCII 路径修复**：`GeoLite2Locator` 显式 `MODE_MMAP`（纯 Python mmap）。Windows 下 maxminddb C 扩展把路径按 UTF-8 字节传给 ANSI fopen，含非 ASCII 字符的路径（如中文目录名）打不开 mmdb；纯 Python 实现走 Python open，Unicode 路径正常，同为内存映射，查询性能差异可忽略。

## 配置项速查（config/ 目录，按领域拆分）

配置按领域拆为多个 TOML（`src/framework/config.py` 导入时按固定文件名加载并导出同名常量；`[paths]` 下 cache_db/log_dir/autoload 相对**数据根**解析，只读资源（`vendor/geoip`）相对**代码根**（项目根）解析；每个顶层节固定归属一个文件，放错文件/缺文件直接报错，无 include/合并机制）：

- `config.toml`：`[paths]` 路径 + `[web]` 面板开关/端口
- `pool.toml`：`[pool]` 阈值/语义组/可请求组 + `[groups]` 分组定义
- `sources.toml`：`[fetch]`（含 `[fetch.freeproxy_world]`）+ `[[sources]]` + `[[fetch_filter]]` 入池筛选规则（`github_lists` 源的镜像前缀走环境变量 `GITHUB_LISTS_MIRROR`，未设置即直连）
- `serve.toml`：`[server]` 端口/队列/心跳/链路门控/metrics/代理类型
- `panel.toml`：`[geoip]` 归属地图 + `[globe]` 3D 地球与目标地区探测
- `ippure.toml`：`[ippure]` 纯净度开关/超时/缓存 TTL + `[[plugins]]` 检查源插件模块路径 + `[[proxy_rules]]` 声明式代理规则 + 插件自声明配置节（节名 = 插件 `SOURCE.name`，如 `[abuseipdb]` / `[ipok]`）
- `logging.toml`：`[logging]` 控制台/文件级别分档 + `[logging.loggers]` 逐 logger 覆盖
- `.env`：密钥（KEY=VALUE；加载时注入进程环境变量，**已存在的真实环境变量优先**；git 忽略）

环境变量 `PROXY_POOL_CONFIG` 指向替代配置**目录**（e2e 隔离用）；`PROXY_POOL_DATA` 指向替代**数据根**（桌面封装用：把 `.cache.db` / `logs/` / `autoload` 落到可写位置；缺省回退项目根，CLI 与测试行为不变）。

**组抽象化**：所有组在 `[groups]` 定义（组名 → `{ ttl }`，仅声明 ttl），代码不硬编码组名。特殊语义组与可请求组均显式声明：
- `[pool].trusted_group` / `blacklist_group`：信任池 / 黑名单组名
- `[server].standby_group`：serve 代理服务队列组名
- `[pool].requestable_groups`：参与可用集合的组（`request`/`get` 取代理遍历，未列出的组不参与）

| 配置项 | 默认值 | 语义 |
|---|---|---|
| `trusted_group` | `trusted` | 信任池：请求成功代理的晋级目标，永不过期 |
| `blacklist_group` | `blacklist` | 黑名单：连续失败淘汰代理的归属，不可请求，10800s 后过期可重新入池 |
| `standby_group` | `Proxy-pool-standby-queue` | serve 代理服务队列：pproxy 上游，由心跳探测维护，永不过期 |

| 常量 | 默认值 | 说明 |
|---|---|---|
| `PROXY_GROUPS` | `[groups]` 派生 | 组名 → TTL 秒数（None 永不过期） |
| `REQUESTABLE_GROUPS` | `[pool].requestable_groups` | `request`/`get` 可从中取代理的组（显式列表声明） |
| `TRUSTED_GROUP` / `BLACKLIST_GROUP` | `[pool]` 指定 | 信任池 / 黑名单组名 |
| `STANDBY_GROUP` | `[server]` 指定 | serve 代理服务队列组名 |
| `MAX_CONSECUTIVE_FAILS` | 5 | 普通组连续失败淘汰阈值 |
| `TRUSTED_MAX_CONSECUTIVE_FAILS` | 10 | 信任池组连续失败上限 |
| `SOURCE_PAUSE_TTL` | 1200 | 去重率过高时的源暂停时长 |
| `SOURCE_DEDUP_THRESHOLD` | 0.92 | 源去重率阈值 |
| `FETCH_PROXY_RETRIES` | 3 | 每源拉取重试次数 |
| `FETCH_FILTER_RULES` | `sources.toml` 的 `[[fetch_filter]]` | 入池筛选规则（`pattern` 正则命中完整代理 URL 即拉黑，`match` 为源名 glob，按声明顺序取首个命中） |
| `STANDBY_QUEUE_CAPACITY` | 30 | serve 代理服务队列容量 |
| `STANDBY_FILL_GROUPS` | trusted 排最前 + 各可请求组 | 代理服务队列填充候选组（按优先级顺序，靠前优先） |
| `STANDBY_FILL_PROBE_MULTIPLIER` / `STANDBY_FILL_PROBE_MIN_EXTRA` | 2 / 5 | 填充候选数 = 缺口 × 倍率，至少 缺口 + 增量 |
| `ENQUEUE_RULES` | `[[server.enqueue_rules]]` | 入队筛选规则（指标 + `priority` 升序 + 阈值，默认全部不启用）：未知指标/未知键/缺必填/优先级非正或重复/同指标重复均在启动时报错 |
| `ENQUEUE_REJECT_GROUP` | `enqueue-rejected` | 未通过入队筛选的候选归属组（`ttl="never"`）：必须在 `[groups]` 定义且不可列入 `requestable_groups`，启动即校验 |
| `HEARTBEAT_INTERVAL` | 2 | serve 心跳间隔秒 |
| `HEARTBEAT_LOCK_INTERVAL` | 30 | 锁定单一上游时的心跳间隔秒（锁定代理不淘汰不拉黑，仅测速展示，降低探测频率与外网请求） |
| `HEARTBEAT_PROBE_URL` | `https://cp.cloudflare.com/generate_204` | 心跳探测目标 |
| `HEARTBEAT_CONCURRENCY` | 16 | 心跳探测并发数 |
| `HEARTBEAT_TIMEOUT` | 5 | 心跳单代理超时秒（免费代理成功延迟实测中位 2.13s/p90 2.84s，3s 余量不足易误判失败） |
| `METRICS_WINDOW` | 1800 | 采样数据留存时长秒（全局）：流量与队列指标的采样点都按此清理，仪表盘据此画满可用数据点；心跳动态调整时窗口始终不变 |
| `STANDBY_CHART_WINDOW` | 60 | 代理服务队列页图表的显示窗口秒数：只影响队列页行内趋势图的裁剪/画图范围，不参与数据留存（留存见 `METRICS_WINDOW`） |
| `NETWORK_PROBE_URLS` | Cloudflare 204 + gstatic 204 + example.com | 链路门控探测目标（任一返回状态码 <400 即认为链路可用） |
| `NETWORK_PROBE_TIMEOUT` | 3 | 单个探测目标超时秒 |
| `NETWORK_UNHEALTHY_CACHE_TTL` | 20 | "链路不可用"结论复用时长秒（0 = 每次真实探测）：只缓存失败结论（陈旧"可用"会授权误拉黑），缓存期内不再探测——同时是链路恢复的最大感知延迟，并抑制异常期各调用方轮询的探测放大 |
| `SERVER_PROXY_TYPES` | `[server].proxy_types`（当前 `["https"]`） | serve 默认上游代理类型（CLI 未指定 `--type` 时的初始值，面板可运行时增删）：只过滤进入队列的上游类型，不影响本地监听协议 |
| `WEB_ENABLED` | `true` | 面板总开关（`[web].enabled`；false 时不构造 aiohttp 面板；纯净度/geo 用例仍会为启用中的入队筛选规则构造，否则与其 requests/geoip2 依赖链一起不 import） |
| `CACHE_DB` / `LOG_DIR` | 数据根 `.cache.db` / `logs/` | 框架层路径（`[paths]` 下相对数据根解析；数据根由 `PROXY_POOL_DATA` 覆盖，缺省=项目根） |
| `IPPURE_SOURCE` | `abuseipdb` | 启用的纯净度检查插件名（= 插件 `SOURCE.name` = 其配置节名）；空字符串 = 禁用该功能 |
| `IPPURE_PLUGIN_MODULES` | `[[plugins]]` 列出的模块路径 | 可选检查源插件（`importlib` 加载，模块须暴露 `SOURCE` 与 `create_checker`） |
| `IPPURE_TIMEOUT` / `IPPURE_CACHE_TTL` | 10 / 2592000 (30d) | 检查请求超时秒数 / 结果缓存有效期（秒，按 `(源名, IP)` 分区） |
| `IPPURE_PROXY_RULES` | `[[proxy_rules]]` | 声明式代理规则（`match` 源名 glob → `use` 代理 URL；按声明顺序取首个命中，无命中直连） |
| `IPPURE_SECTIONS` | `ippure.toml` 的插件节 | 插件自声明配置节（原样透传给插件，节名须与某插件 `SOURCE.name` 一致） |
| `GEOIP_ENABLED` | `true` | 归属定位开关（`[geoip].enabled`；false 时不加载 mmdb，Container.geo 为 None） |
| `GEOIP_DB_FILE` | `vendor/geoip/GeoLite2-City.mmdb` | GeoLite2 City/Country 离线数据库路径（`[geoip].db_file`，相对代码根，按库类型自动识别） |
| `GEOIP_CACHE_TTL` | 7776000 (90d) | IP 归属缓存有效期（秒），`[geoip].cache_ttl` |
| `GLOBE_ENABLED` / `GLOBE_SELF_LAT`+`GLOBE_SELF_LNG` | `true` / 无（须成对） | 3D 地球开关（false 时不构造用例、不注册 `/api/globe`）/ 手填本机坐标（决定本机节点与第一段弧） |
| `GLOBE_DOH_ENDPOINTS` | Cloudflare → Google → alidns | DoH 端点（顺序即回退顺序，须非空 http(s)） |
| `GLOBE_MAX_FLOWS` / `GLOBE_PROBE_CONCURRENCY` / `GLOBE_PROBE_TIMEOUT` | 60 / 3 / 8 | `/api/globe` 下发的最大弧分组数 / DoH 探测并发上限 / 单次 DoH 超时秒 |
| `GLOBE_TARGET_CACHE_TTL` / `GLOBE_ACTIVATION_WINDOW` | 86400 / 60 | (代理,目标) 缓存有效期秒（含负结果）/ 面板活跃才探测的窗口秒 |
| `GLOBE_PROBE_WINDOW` / `GLOBE_PRUNE_AFTER` | 600 / 2592000 | 有流量才作为探测候选的窗口秒 / 缓存行清理阈值秒（30 天） |

## view.py — .cache.db 只读查看器

独立于业务逻辑的 tkinter GUI（纯标准库）：直接读 `.cache.db` 展示各组代理明细（加入时间、连续失败次数、过期倒计时、状态着色）与源暂停状态。含纯数据逻辑（可单测）：`load_cache` / `build_proxy_rows`（状态 expired/invalid/alive，临近过期 60s 预警）/ `build_group_summary` / `build_source_rows`。支持打开任意缓存文件、关键字过滤、5 秒自动刷新。

## 作为库使用

```python
from src.framework.container import build_container

c = build_container()

# 用代理请求 URL，自动重试
proxy, resp = c.request_with_retry.execute("https://example.com", max_retries=5)
print(f"使用 {proxy}，状态码 {resp.status_code}")

# 并发测试代理
results = c.check_proxies.execute(["1.1.1.1:8080", "2.2.2.2:3128"], "https://example.com")
for proxy, info in results.items():
    print(f"{proxy}: {'成功' if info['success'] else '失败'}")

c.save_full()  # 持久化到 .cache.db
```

## 桌面封装（desktop/）

`desktop/` 把项目包成免安装的 Windows 桌面程序（Electron + 内嵌 Python 3.11），不改动 `src/` 的任何业务逻辑；Python 侧唯一配套改动是配置层的**数据根**（`PROXY_POOL_DATA`，见上节）。

**进程与窗口**：Electron 主进程负责启动器窗口（只提供「代理端口 + 启动」）、面板窗口（内嵌加载 `http://127.0.0.1:<web-port>/`）与系统托盘；网页端口是内部页面地址，每次启动由 `ports.findFreePort` 绑 0 取一个系统空闲端口（并避开代理端口），不交给用户填。serve 作为子进程运行，用 `-u -B -s -X utf8` 启动内嵌解释器（`-s` 隔开宿主 user site-packages，保证自包含）。

**目录模型**（打包后，`desktop/dist/win-unpacked/`）：

```
proxy-pool.exe
.cache.db / logs/            ← 运行数据，写在 exe 同级（PROXY_POOL_DATA 指向该目录）
resources/python/            ← 内嵌 Python 3.11 运行时 + 依赖（只读）
resources/pyapp/             ← cli.py + src + vendor（含 geoip）+ config（只读）
```

**就绪判定**：serve 在首轮探测（fill）之后才 bind 面板，故 Electron 侧按「TCP 可连 → `/api/status` 200 → `/` 含 `#app` 挂载点」三段轮询，最后一段用于捕捉「前端未构建」的静默失败。

**构建**：`desktop/scripts/prepare.mjs`（下载官方 embeddable 运行时 → 组装 payload → 安装依赖并自检）与 `desktop/scripts/dist.mjs`（electron-builder `dir` 目标）。用法见仓库根 `README.md` 的「桌面版（Electron）」一节。
