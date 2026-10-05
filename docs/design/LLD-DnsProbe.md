# LLD：DNS 探测队列（流量统计子模块）

> 状态：已实现（原「目标服务器地区探测」重构为事件驱动的 DNS 探测队列，全量测试通过）
> 关联：`src/use_cases/dns_probe.py`、`src/use_cases/traffic_stats.py`、
> `src/interfaces/doh_resolver.py`、`src/domain/ports.py`、`src/interfaces/sqlite_store.py`、
> `src/framework/serve.py`、`config/panel.toml`
> 地球侧只读本模块产出的最终数据，见 `LLD-Globe3D.md`。

## 0. 决策记录（已与用户对齐）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 触发时机 | **事件驱动**：流量统计发现新的 (代理, 域名) 会话即入队，常驻消费者**立刻**探测——不做定时轮询、不需要"激活窗口"（有没有人看地球都要探测） |
| D2 | 归属 | 探测队列是**流量统计模块的子模块**（`TrafficStatsUseCase` 持有），不再是 3D 地球的后端；地球只消费结果 |
| D3 | 落盘职责 | 队列**只探测、不碰存储**；结果经 sink 回调交回流量统计（`note_probe_result` → `save_dns_probe_batch`）落盘，由此复活"探测结果供历史条目/实时连接补目标 IP" |
| D4 | 调度记忆 | **全内存** `_schedule: pair → (下次可探测时刻, 下次失败后的退避秒数)`；成功后冷却 `cache_ttl`，失败指数退避 60s→封顶 15min；由流量统计的小时级清理同拍 `prune` |
| D5 | geo 依赖 | **geo 可选**：`geo=None` 时只产出 ip 行（不做经纬度定位）；探测总开关只看 `[dns_probe].enabled`，与 geoip/globe/web 解耦 |

## 1. 背景与动机

### 1.1 重构前：探测被 3D 地球"绑架"

原「目标服务器地区探测」用例（`TargetGeoUseCase`）挂在 3D 地球后端：`/api/globe` 每次请求调 `touch()` 刷新激活窗口，前端 `GlobePanel` 只在滚进视口、开始 5s 轮询时才触发探测（D4 曾定为"面板打开时激活"）。

问题在于**浏览流量大多发生在没人看地球的时候**：后台跑代理、命令行用代理、面板停在其它页面时，`modstat` 照样记录 (bind, host) 流量，但因为没人请求 `/api/globe`、激活窗口为空，探测永不发生。

### 1.2 由此产生的空 IP 落盘缺陷

历史流量条目按 `(代理, 域名, IP)` 分区落盘，IP 维度来自探测结果缓存。重构前，域名常常**在探测前就被历史条目落盘定格成空 IP**：`flush` 把 `(bind, host, '')` 行写进 `traffic_history`，之后即便探测补齐了 IP，也**没有任何机制回填**这条历史行（IP 一变就是新条目，空 IP 行永远钉在那里）。结果是面板「目标 IP」列大量显示 `-`，历史条目也无法反映真实落点。

### 1.3 重构后：入队即探测 + 延后写

1. **取消激活窗口**：`note_pair` 一发现新会话就入队，队列立刻探测，不再依赖"有没有人看地球"；`activation_window` / `probe_window` 及 `touch()` / `is_active()` 全部删除。
2. **延后写**：`TrafficStatsUseCase.flush` 对「无 IP 且探测仍在途（`is_pending`）」的 pair 延后落盘，从源头不再产出 `(bind, host, '')` 空行；探测出终态或进程退出 `force=True` 时才写。
3. **改名与归属**：用例 `TargetGeoUseCase` → `DnsProbeQueue`（`src/use_cases/dns_probe.py`），端口 `TargetGeoStore` → `DnsProbeStore`，表 `target_geo_cache` → `dns_probe_cache`（旧表启动即删，不做迁移）。

## 2. 事件驱动模型

```
TrafficStatsUseCase.note_pair(bind, host)   ← 会话建立时调用（modstat 热路径）
    └─ dns_probe.enqueue(bind, host)         ← 同步 O(1)：跳过判定 + 在途去重 + 冷却判定，纯内存，不读库
               │（入队成功）
               ▼
DnsProbeQueue.run() 常驻消费者（serve 起 asyncio.create_task(dns_probe.run())）
    await asyncio.Queue.get() → await sem.acquire() → asyncio.create_task(_probe)
               │
               ▼  _probe（受 [dns_probe].probe_concurrency 限并发，不空转不轮询）
    ① proxy_lookup(bind)：查不到（代理已离队）→ 跳过（不落盘、不退避）
    ② host 是公网 IP 字面量 → 跳过 DoH，直接定位
    ③ 否则穿该代理 HostResolver.resolve（DoH JSON，多端点顺序回退）
    ④ 可选 geo.resolve 定位（最多 _MAX_GEO_IPS=2 个 A 记录，取首个命中）
    ⑤ 经 sink 回调把结果行交回流量统计
               │
               ▼
TrafficStatsUseCase.note_probe_result(bind, host, row) → save_dns_probe_batch 落盘
```

要点：

- **入队即干活**：`enqueue` 只把 pair 塞进 `asyncio.Queue`；`run()` 拿到一个就起一个探测任务，没有定时器、没有空轮询。队列有积压时"消费前先取信号量"形成天然背压——空闲时不占名额，积压时不无界并发。
- **enqueue 是热路径的 O(1)**：只做跳过判定 / 在途去重 / 冷却判定，全部纯内存；首次会话与冷却期结束后的会话各触发一次探测，其余（TTL 内的重复会话、仍在途）直接丢弃。
- **结果由流量统计写库**：队列自己不认识 store，只经 `set_sink(traffic.note_probe_result)` 注入的回调交付结果；`set_sink` 后置注入破除"统计持有队列、队列回调统计"的构造期循环依赖。

## 3. 调度记忆：退避与冷却

`_schedule: dict[pair, tuple[float, float]]`，值为 `(下次可探测时刻, 下次失败后的退避秒数)`。

- **成功/负结果**：`_note_success` 置 `now + cache_ttl`，并把退避档位复位为初始值（60s）。冷却期内同一 pair 的新会话在 `enqueue` 阶段直接丢弃。
- **失败**：`_note_failure` 按指数翻倍推进（60s 起、封顶 `_BACKOFF_MAX=900s`=15min），仅当新会话到来且退避已过才重试。
- **明确跳过不算失败**：代理已离队、非法主机名（IDN 编码失败等）直接返回，不写 `_schedule`、不退避——它们不是探测链路的问题。
- **内存清理**：`prune(now)` 丢弃"最后调度时刻早于 `now - cache_ttl`"的陈旧记录；由 `TrafficStatsUseCase` 的小时级清理同拍调用（见 §5）。

## 4. 跳过规则（保持原口径不变）

`_skip_target(host)` 在 `enqueue` 阶段判定，命中即**不入队**（不占并发名额、不落盘、不退避）：

| 目标 | 处理 |
|---|---|
| 内网 / 保留 / 回环 / 链路本地 / 组播 / 未指定 IP | 跳过（穿代理探测无意义） |
| 公网 IP 字面量 | **保留**，`_probe` 中跳过 DoH 直接定位（IP 无解析语义） |
| 单标签主机名（无点） | 跳过（通常是内网机器名） |
| `.local` 后缀 | 跳过 |
| 空 host | 上游 `normalize_host` 已丢弃，不入队 |

`_probe` 阶段另有两种"非失败"的放弃：

- `proxy_lookup(bind)` 为空（代理已离队）→ 跳过，不落盘不退避（代理回归队列后有新会话会再次入队）。
- `resolver.resolve` 抛 `ValueError`（主机名本身非法，如 IDN 编码失败）→ 跳过，不落盘不退避。

## 5. 可选 geo 与结果交付

- **geo 可选**：`DnsProbeQueue(geo=None)` 时 `_locate_row` 直接返回 `{"ip": addresses[0]}`，只产出 ip 行、不做经纬度定位——地理库未配置（`[geoip].enabled=false`）时探测仍可用，只是没有归属坐标。
- **`_locate_row`**：对地址逐个（最多 `_MAX_GEO_IPS=2` 个）调 `geo.resolve([ip], label="dns_probe")`，取**首个定位成功**的写行（多 A 记录只增定位开销）；全部未命中（保留段等）或未配置 geo 时落 `ip` 非空、geo 全空的行——消费方不使用，但 TTL 内不重复探测（避免死磕定位不出的地址）。
- **负结果照常交付**：DoH 成功但无 A 记录时交回空行 `{}`，消费方落"已探测无结果"行（TTL 内不重探）。
- **交付失败兜底**：sink 未接线时记 WARNING 丢弃；sink 抛异常记 exception，均不外抛（不打死探测任务）。

## 6. 结果落盘与流量统计的配合

探测结果的落盘、查询与清理都在 `TrafficStatsUseCase` 内（见 `LLD-TrafficStats.md`）：

- `note_probe_result(bind, host, row)` → `save_dns_probe_batch({(bind, host): row})`（落盘失败只记日志）。
- `dns_probe_rows(pairs)` → `get_dns_probe_batch(pairs)`（读失败返回空 dict，不打断调用方）。
- **延后写（消除空 IP 行）**：`flush` 把「无 IP 且探测仍在途（`dns_probe.is_pending`）」的 pair 延后到探测终态再写；`force=True`（进程退出）时照写当前值，保证不丢。
- **小时级清理**：`_prune` 同拍执行三件事——`prune_traffic_history`、`dns_probe.prune(now)`（内存调度表）、`prune_dns_probe(now - dns_probe_prune_after)`（缓存行）；任一失败只记日志、不影响其余。

## 7. 表结构（`dns_probe_cache`，interfaces/sqlite_store.py）

```sql
DROP TABLE IF EXISTS target_geo_cache;   -- 旧表直接丢弃，不做迁移
CREATE TABLE IF NOT EXISTS dns_probe_cache (
    bind         TEXT NOT NULL,     -- 上游代理地址（bind_of，不含 scheme 前缀）
    host         TEXT NOT NULL,     -- 归一化目标域名（IP 字面量原样）
    ip           TEXT,              -- 穿代理 DoH 解析出的 A 记录（取首个可用）
    country      TEXT,
    country_code TEXT,
    province     TEXT,
    city         TEXT,
    lat          REAL,
    lng          REAL,
    cached_at    REAL NOT NULL,     -- 探测完成时间（TTL 判定在用例层）
    PRIMARY KEY (bind, host)
);
```

- 端口 `DnsProbeStore`（`src/domain/ports.py`）只做纯读写，TTL/清理阈值判断在用例层：`get_dns_probe_batch(pairs)` / `save_dns_probe_batch(rows)`（upsert by `(bind, host)`）/ `prune_dns_probe(before_ts)`。
- "已探测无结果"行（ip/geo 全空）照常保存，TTL 内不重复探测；消费方只使用 `lat/lng` 非空的行画点。
- 表随 `_SCHEMA` 脚本自动创建。

## 8. 配置项（`config/panel.toml` 的 `[dns_probe]`）

```toml
[dns_probe]
enabled = true                 # 探测总开关（false 时不构造队列、不探测）
probe_concurrency = 3          # 并发探测上限
probe_timeout = 8              # 单次 DoH 超时秒
cache_ttl = 86400              # 同一 (代理,目标) 两次探测之间的最小间隔秒
prune_after = 2592000          # 缓存行清理阈值秒（30 天未刷新即删，目标域名不留长期痕迹）
doh_endpoints = ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve", "https://223.5.5.5/resolve"]  # 顺序即回退顺序
```

`config.py` 解析为常量 `DNS_PROBE_ENABLED` / `DNS_PROBE_CONCURRENCY` / `DNS_PROBE_TIMEOUT` / `DNS_PROBE_CACHE_TTL` / `DNS_PROBE_PRUNE_AFTER` / `DNS_PROBE_DOH_ENDPOINTS`，并校验：键均在 `_DNS_PROBE_KEYS` 内、数值键为正、`doh_endpoints` 非空且为 http(s)。

## 9. 与 3D 地球的边界（地球只读）

- `src/use_cases/dns_probe.py`、`src/use_cases/traffic_stats.py`、`src/interfaces/doh_resolver.py`、`src/domain/ports.py` 的探测端口段里**不含任何 globe/地球字样**（硬约束）。
- 3D 地球（`/api/globe`）**不驱动探测**、不再 `touch()`：它读 `ctx.traffic.active_pairs()` + `ctx.traffic.dns_probe_rows(pairs)`（读 `dns_probe_cache`），用纯函数 `aggregate_flows`（`src/framework/web/globe_data.py`）聚合出「代理→目标城市」弧。详见 `LLD-Globe3D.md`。
- 地球的 `enabled` 与 geoip 只影响**要不要展示地球**，不再影响探测是否发生。

## 10. serve 接线（framework/serve.py）

- 仅在 `config.DNS_PROBE_ENABLED` 时构造 `DnsProbeQueue`（`resolver=DohResolver(config.DNS_PROBE_DOH_ENDPOINTS, c.async_getter, timeout=config.DNS_PROBE_TIMEOUT)`、`proxy_lookup=_proxy_lookup(c.standby)`、`geo=c.geo`、`concurrency`/`cache_ttl` 取自配置）。
- `TrafficStatsUseCase(..., dns_probe_store=c.store, dns_probe=dns_probe, dns_probe_prune_after=config.DNS_PROBE_PRUNE_AFTER)`；再 `dns_probe.set_sink(traffic.note_probe_result)`（后置注入）。
- 任务 `probe_task = asyncio.create_task(dns_probe.run())`（`run()` 是常驻消费者）；探测关闭时连任务都不建。
- `DohResolver` 随队列在 serve 内构造（serve 专属依赖，不进容器）；`_proxy_lookup` 复用面板队列行口径（含锁定代理）。

> 删除项：原 `target_probe_loop` / `TARGET_PROBE_INTERVAL` / `TargetGeoUseCase` / `PanelContext.target_geo` 均已移除。

## 11. 错误处理与边界

| 场景 | 处理 |
|---|---|
| DoH 超时/非 200/非 JSON/无 Answer | 该 pair 记失败，内存退避（60s→15min 指数），不落缓存行 |
| DoH 成功但无 A 记录 | 落"已探测无结果"行（ip/geo 全空），TTL 内不再探测 |
| A 记录存在但 GeoLite2 未命中（保留段等） | 落 `ip` 非空、geo 全空的行；消费方不使用 |
| host 是公网 IP 字面量 | 跳过 DoH，直接定位 |
| 内网/保留地址、单标签主机名、`.local` | 入队阶段跳过，不落盘 |
| 非法主机名（IDN 编码失败等） | 跳过，不落盘不退避 |
| bind 已离队（代理淘汰/锁定变化） | 跳过，不落盘不退避；已有缓存行保留（回归可复用） |
| 探测任务意外异常 | 记 exception、视为一次失败进入退避，不打死消费者 |
| geo 未配置 | 只产 ip 行，不做经纬度定位 |
| 缓存写失败 | 只丢本次持久化、不中断探测（同 `IpGeoUseCase` 降级口径），记 exception |

## 12. 测试要点

| 文件 | 用例 |
|---|---|
| `tests/use_cases/test_dns_probe.py` | 入队即探测（`run` 消费）；跳过规则（内网/单标签/`.local`/非法主机名）；IP 字面量直接定位；代理离队跳过；负结果落空行；成功冷却 `cache_ttl`、失败指数退避与封顶；`is_pending`；`prune` 清理；geo=None 只产 ip 行；sink 未接线/抛异常不失控 |
| `tests/framework/test_globe_data.py` | `aggregate_flows` 纯函数：分组求和、hosts 上限与降序、无坐标不产弧、按总速率截断 `max_flows` |
| `tests/interfaces/test_sqlite_store.py::TestDnsProbeCache` | `dns_probe_cache` upsert / 批量读 / 主键分区 / `prune_dns_probe` |
| `tests/framework/test_config.py` | `[dns_probe]` 键校验（缺省、非法数值、空端点表、节归属） |
| `tests/interfaces/test_doh_resolver.py` | 端点顺序回退 / JSON 解析（A 与 CNAME 混排）/ 非 200 / 坏 JSON / IDN |
| `tests/e2e/test_dns_probe_e2e.py` | **不发任何 `/api/globe` 请求**，仅发一次经代理的目标请求 → `dns_probe_cache` 出现该 (bind, host) 行（证明入队即探测）；随后 `/api/globe` 读到该弧；`[dns_probe].enabled=false` 时不落盘 |