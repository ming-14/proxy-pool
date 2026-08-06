项目更新时，**必须同步更新此文档**

---

# proxy-pool

代理池管理工具。自动从多个免费代理源拉取代理，支持分组管理、TTL 过期、成功/失败追踪、黑名单淘汰、源暂停、多线程并发调度，并提供本地 HTTP/SOCKS4/SOCKS5 正向代理服务器（自动心跳 + 无缝切换）。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`PySocks`、`fake-useragent`、`pproxy`

## 快速开始

```bash
# 拉取代理
python cli.py fetch

# 查看池状态
python cli.py status

# 用代理访问 URL（自动重试换代理）
python cli.py request https://httpbin.org/ip

# 并发测试池中代理
python cli.py test https://httpbin.org/ip -c 20

# 启动本地代理服务器（HTTP/SOCKS4/SOCKS5），自动心跳 + 无缝切换
python cli.py serve --port 8080

# 查看 .cache 的可视化只读查看器（tkinter 界面）
python view.py
```

---

## 架构总览

采用干净架构四层（洋葱模型），依赖只能从外层指向内层：

```
框架与驱动层 framework/        config / container / cli / serve / logging
   ↓
接口适配器层 interfaces/        json_store / http_client / scheduler / proxy_server / ua_generator / sources/
   ↓
用例层 use_cases/              fetch / get / record / request_with_retry / check_proxies
   ↓
实体层 domain/                 proxy_pool / models / ports（纯业务规则，零第三方依赖、无 IO）
```

- **实体层（domain）**：`ProxyPool` 代理池领域模型 + `SourceSpec` 值对象 + `ProxySource/ProxyStore/HttpGetter/TaskScheduler` 抽象端口（Protocol）。纯内存，无 IO、无第三方依赖。
- **用例层（use_cases）**：应用业务逻辑，构造注入；只依赖 domain 的端口与模型。
- **接口适配器层（interfaces）**：实现 domain 端口（文件存储、requests 封装、异步调度器、pproxy 封装、各代理源适配器）。
- **框架层（framework）**：`config.py` 配置常量、`container.py` 组合根（依赖组装）、`cli.py` 命令分发、`serve.py` 服务器主循环、`logging.py` 日志。

组合根 `build_container()` 是唯一组装点：构造 `ProxyPool` → 从 `.cache` 恢复状态 → 组装各用例 → 暴露给 CLI。

---

## 核心机制

### 1. 代理生命周期

```
拉取入组 ──成功使用──▶ trusted 信任池（永不过期）
   │                    ▲
   │                    │ 记录成功
   │                    │
   │ 失败累计            │
   │ fails >= 阈值       │
   ▼                    │
blacklist 黑名单 ◀───────┘
（10800s 过期后自动解除，可重新入池）
```

一条代理的状态机：**普通组 →（请求成功）→ trusted →（失败超阈值）→ blacklist →（黑名单过期）→ 可重新被拉取入池**。

### 2. 分组与 TTL

`PROXY_GROUPS`（见 `config/config.toml` 的 `[groups]`，组语义由配置决定，代码不硬编码组名）：

| 组 | TTL | 说明 |
|---|---|---|
| `default-http` / `default-https` / `default-socks5` | 3600s | API 源的 HTTP/HTTPS/SOCKS5 代理 |
| `freeproxy-world-http` / `-https` / `-socks5` | 18000s | FreeProxy.World 三类代理，5 小时（与该源暂停时长一致） |
| `Proxy-pool-standby-queue` | 永不过期 | serve 备用队列（pproxy 上游），由心跳探测维护 |
| `trusted` | 永不过期 | 成功使用过的代理 |
| `blacklist` | 10800s | 失败过多的代理，3 小时后过期可重新入池 |

TTL 规则：代理入组时记录 `joined_at`，`now - joined_at >= ttl` 即过期；`ttl=None` 永不过期。过期条目在 `_cleanup_all_expired()` 统一清理（`get_available_proxies` / `export_state` 时会触发），`find_proxy` 遇到过期代理也即时删除。

### 3. 失败计数与淘汰

- 每次代理请求失败：该代理 `fails += 1`（`RecordProxyUseCase.record_fail`）。
- 淘汰规则（`ProxyPool.get_available_proxies`）：
  - 普通组：`fails >= MAX_FAILS`（2）不再参与可用集合；
  - `trusted` 组：`fails >= TRUSTED_MAX_FAILS`（3）不再参与（容错更高）；
  - 所有组：超过 TTL 自动移除。
- `RequestWithRetryUseCase` 中，失败次数达到所在组阈值（`should_blacklist`：trusted 组 `TRUSTED_MAX_FAILS`、其余组 `MAX_FAILS`）即拉黑（移入 blacklist 组，失败计数清零）。
- 黑名单过期后可被拉取用例重新发现并入池。

### 4. 源暂停机制

- **主动暂停**：`SOURCES` 中配置 `pause_ttl` 的源，每次拉取完成后强制暂停（freeproxy-world 多阶段爬取较重，拉完统一暂停 5 小时 / 18000s）。
- **被动暂停**：拉取后计算该源的去重率 `known / total`（known = 已在 trusted 或 blacklist 的代理数），超过 `SOURCE_DEDUP_THRESHOLD`（0.92）即暂停 `SOURCE_PAUSE_TTL`（1200s）。
- `source_pause_remaining(name)` 返回剩余秒数（未暂停为 None，过期自动解除并移除记录），`is_source_paused()` 委托它判断。暂停状态持久化于 `source_paused`。

### 5. 拉取流程（FetchProxiesUseCase）

三阶段：

1. **逐源拉取**：遍历启用的源（跳过暂停中的源），`fetch_proxies(retries=FETCH_PROXY_RETRIES=3)`；源异常只记日志不中断整体；带 `pause_ttl` 的源拉完立即暂停。
2. **跨源去重入组**：
   - 跳过：已在本次拉取中出现过的（跨源重复）、已存在于池中任一组的、黑名单中的；
   - 禁用代理（端口 80，FreeProxy.World 大量 Cloudflare 假代理）直接入黑名单，不进入可用池（`DISABLED_PORTS = {80}`）；
   - 入组名由 `_resolve_group` 计算：`group_by_protocol` 源按代理 scheme 前缀分拣为 `group-scheme`（如 `default` → `default-http`/`-https`/`-socks5`，无 scheme 前缀按 http），否则固定入 `group`。
3. **去重率检查**：见「源暂停机制」。

### 6. 取代理策略（GetProxiesUseCase）

- `get_available_proxies()` 遍历 `REQUESTABLE_GROUPS`（8 个可请求组，含备用队列与 trusted），排除黑名单、超失败数代理与 `exclude` 集合；
- **trusted 优先**：先随机取 trusted 中的代理，不足再从其它可用组随机补齐——避免在大量死代理（如 freeproxy-world 假代理）中随机命中失败；
- 池空时自动触发一次 `fetch.execute()`（`cli get` 的 `--fetch` 回调）再取；
- 随机源可注入（`rng`）以测试确定性。

### 7. 带重试请求（RequestWithRetryUseCase）

```
for attempt in range(max_retries):
    取 1 个代理（exclude 已试过的）→ 请求
    成功 → record_success（晋级 trusted，fails 清零）→ 返回 (proxy, resp)
    失败 → record_fail（fails+1）；fails >= 2 → 拉黑
全部失败 → 抛出最后一个异常；无可用代理 → RuntimeError("无可用代理")
```

`record_success` 对**备用队列成员只复制进 trusted、保留在队列**（serve 上游不被掏空），其余组代理移动晋级；取代理时 `get_available_proxies` 对多组副本自动去重，不会重复返回。

### 8. 并发调度器（ProxyScheduler）

通用**异步**任务调度器（asyncio 实现，`TaskScheduler` 端口，`run` 为协程；`check_proxies` 用例只用 `run`）：

- 协程式任务：`async def run(tasks, phase_name, do_task, on_success=None)`，`do_task` 为协程 `(proxy, task) -> result`，任务经 `asyncio.create_task` 并发执行（`asyncio.wait(FIRST_COMPLETED)` 调度）；
- 每个代理限流 `task_interval`（同代理两次任务间隔）；
- `do_task` 返回 dict 且带 `success` 键时以它为成败依据（探测任务的实际结果）；任务异常 → 该代理 `fails+1`，超过 `max_fails` 丢弃该代理（后续不再分配任务），未超限自动重试该任务；
- 取消：cancel 在途子任务后 re-raise；
- 两种任务分配方式：`assign_sequential`（按未用代理顺序分配，Phase2 用）与 `assign_round_robin`（存活代理轮询分配，Phase4 用）。

### 9. 并发探测（CheckProxiesUseCase）

- `execute` 为协程：并发测试多个代理访问目标 URL，返回 `{proxy: {success, status_code, elapsed, error}}`；
- 网络请求经 `asyncio.to_thread` 投递（requests 同步调用），不阻塞事件循环；
- 未执行到的代理补 `{"success": False, "error": "未执行"}`；
- 证书校验按代理类型区分：**http/socks5 代理严格校验（verify=True）**，隧道端到端校验目标证书、筛掉伪造证书的中间人代理；**https 代理不校验**（TLS 直连代理本身，校验的是代理自签证书，免费代理会全灭）。

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

---

## serve 机制（重点）

`src/framework/serve.py` + `src/interfaces/proxy_server.py`：

### 本地代理服务器（PproxyServer）

- 基于 **pproxy**，本地监听 `http+socks4+socks5://host:port`（一个端口同时提供三种协议）；
- 池内代理字符串 → pproxy 上游 URI：`https://` 前缀（HTTPS 代理，代理本身走 TLS）转换为 `http+ssl://`；http/socks5 原样传递；
- **rserver 动态同步**：`sync(proxies)` 对上游列表做集合差集增删，rserver 是同一列表引用，运行中增删对 pproxy schedule 实时生效；
- 调度算法 `salgorithm="rc"`（随机）：每次请求随机选存活代理，坏代理有概率被跳过；不用 `fa`（first_available）是因为 fa 会固定命中第一个坏代理导致持续失败。

### 备用队列（Proxy-pool-standby-queue）

- 有界组，容量 `STANDBY_QUEUE_CAPACITY`（10），永不过期；是 pproxy 的唯一上游来源；
- **填充（fill）**：队列有缺口时，候选数 = 缺口 × `standby_fill_probe_multiplier`（至少 缺口 + `standby_fill_probe_min_extra`，保证存活命中率），候选**按配置的填充组优先级**（`standby_fill_groups`，trusted 默认最前）从可用集合收集，按 `--type` 过滤类型，去重后并发探测（async）；存活代理 `move_proxy` 入备用队列（单一归属），死代理直接拉黑；
- **心跳（heartbeat）**：并发探测队列内代理（严格校验证书，async）；存活**保留在队列（不晋级 trusted）**，失败计 1 次：未达阈值（普通组 `MAX_FAILS=2`，trusted `TRUSTED_MAX_FAILS=3`）进入**退避补测**，达到阈值拉黑移出——一次瞬时失败不拉黑；
- **退避补测（retry_probe）**：心跳失败的代理由主循环启动独立异步复核任务（不占心跳轮次），间隔 1s/2s/4s…指数退避（上限 30s）反复探测：成功清零失败计数恢复，失败继续累计直至阈值拉黑；
- **主循环**：启动时先 fill → 启动服务器 → 周期性「fill → heartbeat → 为待补测代理启动复核任务 → sync → 保存」，两轮之间如再缺再 fill；轮次**绝不重叠**（以上一轮完成时刻 + interval 为下一轮最早起点，避免刷屏与 sleep 计时误差）；Ctrl+C 优雅退出（取消心跳与复核任务 → 关服务器 → 保存）；
- **成功复制**：备用队列成员被请求成功（`record_success`）时**只复制进 trusted、保留在队列**（serve 上游不被掏空）；拉黑（`remove`）时清理其在所有组的副本（含 trusted 残留），单次遍历实现；
- **类型限制**：默认 `SERVE_PROXY_TYPES = ("http", "socks5")`，**不含 https**——HTTPS 代理在 pproxy 中是 TLS 终结（`http+ssl://` 上游），浏览器访问 https 目标会拿到上游证书导致 `ERR_CERT_AUTHORITY_INVALID`；http/socks5 均为透传无此问题。

```
浏览器 ──▶ 本地 pproxy(127.0.0.1:8080) ──rc 随机──▶ 备用队列上游(存活代理)
                                                    ▲
                              fill 补入 / heartbeat 探测（并发，严格校验证书）
```

---

## 代理源

通用 HTTP API 源继承 `HttpSourceFetcher` 基类（`sources/base.py`）：URL 构建 + 重试直到非空（`_fetch_until_nonempty`）+ scheme 前缀补全。基类类属性 `BASE_URL / DEFAULT_PARAMS / RETRIES / TIMEOUT / PROTOCOL`，子类提供 `_parse`。

| 模块 | 机制 |
|---|---|
| `sources/ip89.py` | 89IP，纯文本响应，正则 `\d+\.\d+\.\d+\.\d+:\d+` 提取；每源可指定 `num` |
| `sources/goodips.py` | Goodips，JSON 接口 `code==0` 时 `data` 为 `[{ip, port}]` |
| `sources/proxy_scdn.py` | ProxyScdn，JSON 接口 `code==200` 时 `data.proxies` 为代理列表 |
| `sources/geonode.py` | Geonode，JSON 分页接口（每页 limit 条），逐页拉取直到空页 / 不足一页 / 达到总数；只保留 `protocols` 含 http 的代理 |
| `sources/freeproxy_world.py` | 见下（多阶段爬取） |

### FreeProxy.World 多阶段爬取

先获取可用的 **HTTPS 中转代理**，再用中转代理爬取三类代理页（异步实现：asyncio 调度 + 请求经 `asyncio.to_thread` 投递）：

1. **Phase1**：直连请求 HTTPS 代理第 1 页（获取中转代理 + 总页数）；
2. **Phase2**：用 HTTPS 代理并发（16）爬取 HTTPS 第 2..N 页；失败页面用未使用代理第二轮重试；
3. **Phase3**：用中转代理分别拉取 http/https/socks5 三类代理第 1 页（逐个代理尝试，全部失败回退直连——与 Phase1 共用 `_try_first_page` 重试循环）；
4. **Phase4**：并发爬取各类后续页（与 Phase2 共用 `_crawl_pages`，Phase4 按 round-robin 分配）。

- 验证过的中转 HTTPS 代理（working + unused）连同三类爬取结果一起去重返回（带 scheme 前缀），由拉取用例按 scheme 分拣入 `freeproxy-world-{scheme}` 组；
- 页面有效性校验：含 `<td style="font-weight: 500;">` 才算有效页（CF 墙/空页返回失败）；
- 走代理时用 requests（HTTPS 代理需 TLS 到代理）；直连用 `http_client`；
- 并发参数：`FPW_MAX_CONCURRENT=16`、`FPW_PROXY_RETRY=1`、`FPW_TASK_INTERVAL=2`、`FPW_REQUEST_TIMEOUT=15`；
- 该源产出的**端口 80 代理是 Cloudflare 假代理**（CONNECT 400），拉取时直接入黑名单（`DISABLED_PORTS={80}`）。

### 源启用/禁用

在 `config/config.toml` 的 `[[sources]]` 列表中配置；`name` 为模块导入路径（同时是源暂停记录 key），`group` 为入组名，`group_by_protocol` 决定是否按 scheme 分拣，`pause_ttl` 为拉取后强制暂停时长。

---

## 数据持久化

`JsonProxyStore` 读写项目根 `.cache`（紧凑 JSON，`ensure_ascii=False`）；文件缺失/损坏视为空状态。领域规则在 `ProxyPool.export_state() / restore_state()`：

```json
{
  "proxy_groups": {
    "default-http": {
      "ttl": 3600,
      "proxies": [{"proxy": "http://1.2.3.4:8080", "joined_at": 1700000000, "fails": 0}]
    }
  },
  "source_paused": {
    "src.interfaces.sources.freeproxy_world": {"paused_at": 1700000000, "ttl": 18000}
  }
}
```

- `export_state()` 先清理过期条目再导出；
- `restore_state()` 恢复时：**组 ttl 以运行时配置为准**（改配置即生效，文件里的 ttl 不覆盖）；过滤已过期条目与已过期源暂停；兼容旧 `.cache` 格式（组内 proxies 为 list）。
- CLI 各命令在 `finally` 中调用 `c.save()` 保证退出时落盘。

---

## 配置项速查（config/config.toml）

唯一配置数据源为 `config/config.toml`（`src/framework/config.py` 在导入时加载并导出同名常量；`[paths]` 下路径相对项目根解析）。

**组抽象化**：所有组在 `[groups]` 定义（组名 → `{ ttl, role?, requestable? }`），组语义由配置决定，代码不硬编码组名：
- `ttl`：整数秒数，`"never"` 表示永不过期（`None`）
- `role`：特殊语义组，每个角色必须恰好一个
- `requestable`：是否参与可用集合（默认 false）

| 角色 | 组（默认名） | 语义 |
|---|---|---|
| `trusted` | `trusted` | 信任池：请求成功代理的晋级目标，永不过期 |
| `blacklist` | `blacklist` | 黑名单：失败淘汰代理的归属，不可请求，10800s 后过期可重新入池 |
| `standby` | `Proxy-pool-standby-queue` | serve 备用队列：pproxy 上游，由心跳探测维护，永不过期 |

| 常量 | 默认值 | 说明 |
|---|---|---|
| `PROXY_GROUPS` | `[groups]` 派生 | 组名 → TTL 秒数（None 永不过期） |
| `REQUESTABLE_GROUPS` | `[groups]` 派生 | `request`/`get` 可从中取代理的组（requestable=true） |
| `TRUSTED_GROUP` / `BLACKLIST_GROUP` / `STANDBY_GROUP` | `[groups]` 的 role | 信任池 / 黑名单 / 备用队列组名 |
| `MAX_FAILS` | 2 | 普通组失败淘汰阈值 |
| `TRUSTED_MAX_FAILS` | 3 | 信任池组失败上限 |
| `SOURCE_PAUSE_TTL` | 1200 | 去重率过高时的源暂停时长 |
| `SOURCE_DEDUP_THRESHOLD` | 0.92 | 源去重率阈值 |
| `FETCH_PROXY_RETRIES` | 3 | 每源拉取重试次数 |
| `STANDBY_QUEUE_CAPACITY` | 10 | serve 备用队列容量 |
| `STANDBY_FILL_GROUPS` | trusted 排最前 + 各可请求组 | 备用队列填充候选组（按优先级顺序，靠前优先） |
| `STANDBY_FILL_PROBE_MULTIPLIER` / `STANDBY_FILL_PROBE_MIN_EXTRA` | 8 / 10 | 填充候选数 = 缺口 × 倍率，至少 缺口 + 增量 |
| `HEARTBEAT_INTERVAL` | 2 | serve 心跳间隔秒 |
| `HEARTBEAT_PROBE_URL` | `https://httpbin.org/ip` | 心跳探测目标 |
| `HEARTBEAT_CONCURRENCY` | 16 | 心跳探测并发数 |
| `HEARTBEAT_TIMEOUT` | 3 | 心跳单代理超时秒 |
| `SERVE_PROXY_TYPES` | `("http", "socks5")` | serve 默认允许的代理类型（不含 https，原因见 serve 机制） |
| `CACHE_FILE` / `LOG_DIR` | 项目根 `.cache` / `logs/` | 框架层路径（`[paths]` 下相对项目根解析） |

---

## CLI 命令

根目录 `cli.py` 是薄入口（引导 sys.path 后委托 `src.framework.cli.main()`）。

### serve — 本地代理服务器

```bash
python cli.py serve [--port 8080] [--host 127.0.0.1] [--queue-size 10]
                   [--interval 2] [--probe-url URL] [--type http|https|socks5]
```

- `--queue-size`：备用队列容量；`--interval`：心跳间隔秒；`--type` 可多次指定
- 心跳成功 → 代理保留在备用队列（不晋级 trusted）；失败 → 计次进入退避补测（独立复核，成功恢复），达到阈值（`MAX_FAILS`/`TRUSTED_MAX_FAILS`）拉黑移出；缺口自动探测补入

### fetch — 拉取代理

```bash
python cli.py fetch [--json]
```

### list / ls — 列出可用代理

```bash
python cli.py list [--group GROUP] [--json]
```

### get — 随机获取代理（trusted 优先，池空自动拉取）

```bash
python cli.py get [-n N] [--json]
```

### status — 查看代理池状态（各组数量/TTL/暂停源）

```bash
python cli.py status
```

### blacklist / trust

```bash
python cli.py blacklist 1.2.3.4:8080 5.6.7.8:3128
python cli.py trust 1.2.3.4:8080
```

### request — 用代理访问 URL（自动重试换代理）

```bash
python cli.py request URL [--timeout 10] [--retries 5] [--max-length N] [--headers]
```

成功代理自动晋级 trusted，失败过多的自动拉黑（备用队列成员只复制进 trusted、保留在队列，见「备用队列」）。

### test — 测试代理

单代理模式：`python cli.py test 1.2.3.4:8080 URL`（走 `http_client.get`，不校验证书）；
并发模式：省略 proxy 或加 `-c N`，从池中取 N 个代理并发测试，表格化输出（按 成功优先/耗时 排序）。

### 日志

`--debug` 输出 DEBUG 级；默认 `setup_logging()`：统一格式（毫秒时间戳）输出到 **stderr + logs/proxy-pool-{YYYYMMDD-HHMMSS-mmm}.log**（毫秒精度天然唯一不覆盖）；`urllib3`/`requests` 日志降为 WARNING。

---

## view.py — .cache 只读查看器

独立于业务逻辑的 tkinter GUI（纯标准库）：直接读 `.cache` 展示各组代理明细（加入时间、失败次数、过期倒计时、状态着色）与源暂停状态。含纯数据逻辑（可单测）：`load_cache` / `build_proxy_rows`（状态 expired/invalid/alive，临近过期 60s 预警）/ `build_group_summary` / `build_source_rows`。支持打开任意缓存文件、关键字过滤、5 秒自动刷新。

---

## 项目结构

```
proxy-pool/
├── cli.py                  # 命令行薄入口
├── view.py                 # .cache 只读查看器（tkinter）
├── AGENTS.md               # 工程规范
├── requirements.txt        # requests / PySocks / fake-useragent / pproxy
├── pytest.ini              # pytest 配置（testpaths=tests）
├── config/                 # 配置文件
│   └── config.toml         # 唯一配置数据源（分组/源/超时/路径等）
├── .cache                  # 代理池状态持久化文件（JSON）
├── logs/                   # 日志文件（毫秒时间戳命名）
├── src/                   # 主包（干净架构四层）
│   ├── domain/            # 实体层：纯业务规则，零第三方依赖、无 IO
│   │   ├── proxy_pool.py  # ProxyPool（分组/TTL/晋级/淘汰/源暂停/序列化）
│   │   ├── models.py      # SourceSpec 值对象、normalize_proxy/to_proxy_dict
│   │   └── ports.py       # 抽象端口：ProxySource/ProxyStore/HttpGetter/TaskScheduler
│   ├── use_cases/          # 用例层：构造注入
│   │   ├── fetch_proxies.py       # 拉取（三阶段：拉取/去重入组/去重率暂停）
│   │   ├── get_proxies.py         # 获取（trusted 优先，池空自动拉取）
│   │   ├── record_proxy.py        # 成功/失败记录、拉黑、信任
│   │   ├── request_with_retry.py  # 带重试换代理的请求
│   │   ├── check_proxies.py       # 并发测试代理
│   │   └── standby.py             # 备用队列维护（fill 补入/心跳/退避补测）
│   ├── interfaces/         # 接口适配器层
│   │   ├── json_store.py   # JsonProxyStore 文件存储
│   │   ├── http_client.py  # RequestsHttpGetter（不校验证书 + 多 IP 重试）
│   │   ├── scheduler.py    # ProxyScheduler 异步调度器（asyncio）
│   │   ├── proxy_server.py # PproxyServer（rserver 动态同步 + rc 调度）
│   │   ├── ua_generator.py # 确定性 UA 生成器
│   │   └── sources/        # 代理源适配器（base/ip89/freeproxy_world/geonode/goodips/proxy_scdn）
│   └── framework/          # 框架与驱动层
│       ├── config.py       # 配置加载（唯一数据源 config/config.toml）
│       ├── container.py    # 组合根（依赖组装）
│       ├── cli.py          # argparse 命令处理
│       ├── serve.py        # serve 主循环（调度 fill/heartbeat/补测 + 上游同步）
│       └── logging.py      # 日志模块
└── tests/                  # 复刻 src 结构的单测 + e2e
    ├── domain/             # ProxyPool 规则测试
    ├── use_cases/          # 用例测试
    ├── interfaces/         # 存储/调度器/UA/代理服务器/源测试
    ├── framework/          # CLI / view 测试
    └── e2e/                # serve 全链路 e2e（mock 上游：HTTP CONNECT + SOCKS5 握手）
```

---

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

c.save()  # 持久化到 .cache
```

---

## 测试

```bash
python -m pytest -v
```

- 单测：领域规则（TTL/晋级/淘汰/暂停）、用例（拉取三阶段、重试、晋级）、接口（存储/调度器/UA/代理服务器/各源解析）、CLI（fake container 注入）；
- e2e：`tests/e2e/` 用本地 mock 上游（模拟 HTTP CONNECT 隧道 + SOCKS5 握手）验证 serve 全链路：fill 补入 → pproxy 启动 → 请求经上游转发 → 心跳移出死代理 → 动态 sync 增删 → 无缝切换 → Ctrl+C 优雅退出，全程不依赖外网。
