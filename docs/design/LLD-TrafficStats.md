# LLD：流量统计模块（归一化 + 流量统计页）

> 状态：设计待评审（未动代码）
> 关联：`src/interfaces/proxy_server.py`、`vendor/pproxy/proto.py`、`src/framework/serve.py`、
> `src/framework/web/api.py`、`src/use_cases/target_geo.py`、`frontend/src/pages/`

## 0. 决策记录（已与用户对齐）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 「实时流量」的口径 | **只算活跃连接**——列表显示此刻还开着的连接（从 pproxy 的连接事件钩子统计），连接关闭即从列表消失，不保留 30 分钟余温 |
| D2 | 「历史流量」的条目粒度 | **IP 变动即新条目**：行 = (代理, 域名, IP)，同一域名解析出新 IP 就新增一行、各记各的流量（可看出 CDN 漂移）；配保留期清理 |
| D3 | 「归一化」的边界 | **完整搬迁到 use_cases 层**：`_traffic_*` / `_per_proxy_*` / `_pair_*` 全部移出 `PproxyServer`，它只保留「把 pproxy 事件转成上报调用」的钩子 |
| D4 | DNS 结果的落盘 | **沿用现有 `target_geo_cache`**（已有表，格式即「网站-ip-地理信息」）；本设计不改其结构 |

默认口径（未特别反对即按此实现）：

- 实时页与历史页共用同一套维度：**域名（host）+ DNS 解析 IP + 地理信息 + 代理（bind）**。
- 历史条目的「流量总计」= 该条目自建立以来的累计上下行字节；「最后连接时间」= 该条目最近一次会话建立时刻。
- 地理信息不冗余存进历史表：按 IP 关联 `ip_geo_cache`（主键是 IP，归属稳定）。

## 1. 背景与目标

### 1.1 现状（调研结论）

**流量统计目前全部压在 `PproxyServer`（interfaces 层）内部**，且全部只在内存：

| 内存结构 | 维度 | 持久化 |
|---|---|---|
| `_traffic_upload` / `_traffic_download` | 全局累计 | 无 |
| `_traffic_history` | 全局采样序列（deque） | 无 |
| `_per_proxy_upload` / `_per_proxy_download` | 按代理（bind）累计 | 无 |
| `_per_proxy_traffic_history[bind]` | 按代理采样序列 | 无 |
| `_pair_last_seen` / `_pair_upload` / `_pair_download` / `_pair_rate_ema` / `_pair_rate_inst` / `_pair_prev` | 按 (代理, 目标) | 无 |

访问器：`traffic_total()`、`traffic_history()`、`per_proxy_traffic()`、`active_pairs(since)`、
`per_proxy_traffic_history(bind)`。

三个必须处理的事实：

1. **连接数被直接丢弃**。`vendor/pproxy/proto.py` 的 `channel` / `http_channel` 会调
   `stat_conn(1)` / `stat_conn(-1)`（连接建立/关闭），对应到 `modstat` 返回的 `m(i)` 的
   `i = 4 + roption.direct`。产品代码里这条分支是 `return lambda n: None`，所以 D1 要的
   「活跃连接」目前一条数据都没有，得从这条钩子捡回来。
2. **历史无数据源**。`metrics_window`（见 `config/serve.toml` 的 `[server]`）只留很短一段，
   且重启清零；`_MAX_PAIRS` 还有容量上限、超限按 `last_seen` 批量淘汰。所以 D2 的历史页
   必须新建持久化。
3. **`_pair_last_seen` 记的是「会话建立时刻」而非「最后传字节时刻」**，所以窗口口径下
   长时间下载的连接会中途消失——这正是 D1 选连接口径的原因。

`per_proxy_traffic()`（按 bind 的累计字典）目前**没有消费者**，`per_proxy_traffic_history(bind)`
只被 `/api/proxy/history` 使用（队列页/单代理趋势）。搬迁时按此对齐引用点。

### 1.2 目标

- 面板新增「流量统计」页，两个选项卡：**实时流量**（活跃连接）与**历史流量**（持久化条目）。
- 流量统计归一化到一个 use_cases 层模块，`PproxyServer` 不再持有任何统计结构。
- 全局流量、按代理流量、按 (代理, 域名) 流量共用同一套口径与同一份数据源。

### 1.3 非目标（v1 不做）

历史流量的按天聚合/报表、导出、告警；连接级的逐条明细（只保留聚合计数）；
目标端口维度；UDP（池不支持）；跨机器聚合。

## 2. 数据流

```
pproxy stream_handler
    └─ modstat(user, remote_ip, host_name, roption) → m(i)
         ├─ m(0|1)  上行字节 ─┐
         ├─ m(2|3)  下行字节 ─┼─→ TrafficStatsUseCase.record_bytes(bind, host, up, down)
         └─ m(4|5)  连接 ±1  ──→ TrafficStatsUseCase.record_conn(bind, host, delta)
                                        │
                    ┌───────────────────┴────────────────────┐
                    ▼                                        ▼
          内存态（采样、速率、活跃连接）            持久化（历史条目，按节流批量落盘）
                    │                                        │
                    ▼                                        ▼
         GET /api/traffic/live                    GET /api/traffic/history
                    └───────────────┬────────────────────────┘
                                    ▼
                    流量统计页（实时 / 历史 两个选项卡）
```

目标 IP 归属链路不变（D4）：会话建立的 pair 进 `TargetGeoUseCase` 的 DoH 队列 →
`target_geo_cache` 落 (bind, host) → 归属；实时页与历史页都用它补 IP/地理信息。

## 3. 后端设计

### 3.1 TrafficStatsUseCase（`src/use_cases/traffic_stats.py`，新增）

把 `PproxyServer` 的统计结构与方法整体搬到此模块（Use Cases 层，纯内存 + 一个 store 端口）。
`PproxyServer` 构造时注入该用例，`modstat` 只做转发。

```python
class TrafficStatsUseCase:
    def __init__(self, *, store: TrafficHistoryStore,
                 target_ip_lookup: Callable[[list[tuple[str, str]]], dict[tuple[str, str], str]],
                 metrics_window: float, flush_interval: float, retention: float,
                 max_pairs: int = ...) -> None: ...

    # ---- 上报入口（modstat 调用，事件循环线程，热路径 O(1)）----
    def note_pair(self, bind: str, host: str) -> None: ...      # 会话建立：记 last_seen
    def record_bytes(self, bind: str, host: str, up: int, down: int) -> None: ...
    def record_conn(self, bind: str, host: str, delta: int) -> None: ...

    # ---- 采样与落盘（serve 的采样循环调用）----
    def sample(self, now: float) -> None: ...      # 差分成速率/EMA、清理过期、并入待落盘增量
    def flush(self, now: float, *, force: bool = False) -> None: ...

    # ---- 查询（API 调用）----
    def total(self) -> tuple[int, int]: ...
    def traffic_history(self) -> list[TrafficSample]: ...
    def per_proxy_traffic(self) -> dict[str, dict[str, int]]: ...
    def per_proxy_traffic_history(self, bind: str) -> list[TrafficSample]: ...
    def active_pairs(self, since_ts: float) -> list[dict]: ...
    def live_connections(self) -> list[dict]: ...  # D1：此刻活跃的连接（见 3.2）
    def history_entries(self, limit: int) -> list[dict]: ...
    def history_count(self) -> int: ...
```

`target_ip_lookup` 是 `(bind, host)` 批量查目标 IP 的闭包（serve 内由 `SqliteProxyStore.get_target_geo_batch`
构造）：落盘时用它给条目定 `ip` 维度，取不到记空串。

`PproxyServer` 侧只剩钩子：

```python
def _make_modstat(self):
    traffic = self._traffic

    def modstat(user, remote_ip, host_name, roption):
        bind = roption.bind
        traffic.note_pair(bind, host_name)     # 会话建立：记 last_seen（host 归一化在用例内）

        def m(i):
            if i in (0, 1): return lambda n: traffic.record_bytes(bind, host_name, n, 0)
            if i in (2, 3): return lambda n: traffic.record_bytes(bind, host_name, 0, n)
            if i in (4, 5): return lambda n: traffic.record_conn(bind, host_name, n)
            return lambda n: None
        return m
    return modstat
```

> 搬迁后 `PproxyServer` 不再有 `sample_traffic()`；`serve.py` 的采样循环改调 `traffic.sample()`；
> `api.py` 的 `ctx.server.traffic_total()` 等全部改指 `ctx.traffic.*`。引用点一次性更新，
> 不留兼容接口。

### 3.2 连接级统计（D1）

`m(4 + roption.direct)` 是 remote 通道的 `stat_conn`：通道开始调 `+1`、结束调 `-1`，即
**一个会话一条 remote 连接**的生命周期。闭包已捕获 `bind` / `host`，可直接按 (bind, host) 计数。

- 结构：`_conn_count: dict[tuple[str, str], int]` + `_conn_first_seen: dict[tuple[str, str], float]`。
- 计数归零即删除该 key（不保留余温，符合 D1）。
- `record_conn` 需保证不出现负数（异常路径下 `-1` 多于 `+1` 时钳到 0 并记 WARNING）。
- client 侧另有一个 `roption.connection_change` 钩子（`vendor/pproxy` 内部方法），
  **不使用**：一个会话统计一次即可，避免双计。

### 3.3 持久化：`traffic_history` 表（D2）

```sql
CREATE TABLE IF NOT EXISTS traffic_history (
    bind      TEXT    NOT NULL,     -- 上游代理地址（bind_of，不含 scheme）
    host      TEXT    NOT NULL,     -- 归一化目标域名/IP 字面量
    ip        TEXT    NOT NULL,     -- 该 host 经该出口解析出的目标 IP（无结果记空串）
    upload    INTEGER NOT NULL DEFAULT 0,
    download  INTEGER NOT NULL DEFAULT 0,
    last_seen REAL    NOT NULL,     -- 最后一次会话建立时刻（秒）
    PRIMARY KEY (bind, host, ip)
);
```

- **粒度即主键**：IP 变了就是新行（D2）；旧行冻结在切换那一刻的累计值，不再更新。
- **不冗余存 geo**：按 `ip` 关联 `ip_geo_cache`（主键为 IP，归属稳定）。
  **不能关联 `target_geo_cache`**——它主键是 (bind, host)，IP 一变旧落点就被覆盖，拿不到历史。
- 清理：`prune_traffic_history(before_ts)`（按 `last_seen` 删，保留期见 `config` 的对应节）。

**流量如何归到具体 IP**（IP 是异步 DoH 得到的，晚于流量）：

采样时每个 pair 先算出「本窗口增量」（`up - prev_up`；首采即建连以来的全部字节——增量
与 `dt` 无关，首采不产生速率但必须产生增量），并入内存待落盘表 `_pending[(bind, host)]`。
落盘时批量查该 pair 当前 IP 得到条目 key `(bind, host, ip)`，**以增量 upsert 累加到 SQL 侧**
（`upload = upload + excluded.upload`）。

两个推论：

- **IP 变了就是新条目**：切换后的增量进新行，旧行不再被更新，自然冻结在切换前的累计值
  ——不需要"基线"这类额外状态。
- **写增量而非绝对值**：进程重启后内存归零也不会把已落盘的历史冲掉。

### 3.4 落盘时机与节流

采样每 2s 一次、活跃 pair 可达数千，**不能每轮都写 SQLite**。方案：

- 内存先累加，**按 `flush_interval` 节流批量 upsert**（`INSERT ... ON CONFLICT DO UPDATE`）；
- 进程退出时 flush 一次（`serve` 的清理路径）；
- 写失败只丢本轮持久化、不中断采样（记 exception，与 `IpGeoUseCase` 降级同口径）。

### 3.5 API（`src/framework/web/api.py`）

| 路由 | 说明 |
|---|---|
| `GET /api/traffic/live` | 实时条目：`{entries: [{bind, host, ip, geo, connections, up_rate, down_rate, first_seen, last_seen}]}` |
| `GET /api/traffic/history` | 历史条目：`{entries: [{bind, host, ip, geo, upload, download, last_seen}], total_entries}`，按流量总计降序 |

- 注册条件：无（与 web 同生命周期）；`geoip` 关闭时条目的 `geo` 恒为 `null`，流量部分不受影响。
- 历史**不做 `since` 增量**：条目会被持续累加更新，"只返回新点"的增量语义对它不成立；
  改为固定上限（`config.TRAFFIC_HISTORY_LIMIT`）+ 按需刷新。
- `/api/status` 的 `traffic_total` 保持不变（顶栏/仪表盘在用），取值改到新用例。

## 4. 前端设计

### 4.1 页面与路由

- 新增一级导航「流量统计」（`frontend/src/App.vue` 的 `NAV`）+ 路由
  `{ path: '/traffic', name: 'traffic', component: () => import('@/pages/TrafficPage.vue'), meta: { title: '流量统计' } }`。
- `router.ts::afterEach` 的 `historyEnabled` 判定是否纳入本页：历史选项卡需要增量拉取，**纳入**。

### 4.2 实时选项卡

- 数据源 `/api/traffic/live`，轮询间隔与现有热路径一致（见 `status` store 的节奏），
  页面不可见 / 切后台时停轮询（与 `GlobePanel`、`status` store 同口径）。
- 列：域名 / 目标 IP / 地理信息 / 代理 / 活跃连接数 / 上下行速率。
- 空态：无活跃连接时显示占位文案，不显示空表。

### 4.3 历史选项卡

- 数据源 `/api/traffic/history`，进入选项卡时拉一次（条目持续累加更新，不做增量、只按需刷新）；
  列同 4.2，另加「流量总计」「最后连接时间」。
- 排序：后端已按流量总计降序下发；页头给出条目总数与"超出上限未列出"的提示。
- 地理信息列沿用现有归属列口径（`displayCountryName` + 省市拼接，见 `utils/format`）。

## 5. 边界与错误处理

| 场景 | 处理 |
|---|---|
| 解析不出 IP（DoH 失败/无 A 记录/未探测） | 历史行的 `ip` 记空串，实时行的 `ip/geo` 为 `null`；条目照常显示 |
| host 是 IP 字面量 | 不经 DoH，直接定位（沿用 `TargetGeoUseCase` 现有判定） |
| 内网/保留地址、单标签主机名、`.local` | 沿用现有跳过口径，不落历史行 |
| 同一 (bind, host) 并发多条连接 | 计数累加；归零即删（不保留余温） |
| `-1` 多于 `+1`（异常路径） | 钳到 0 并记 WARNING，不出现负计数 |
| pair 超容量 | 沿用 `_MAX_PAIRS` 批量淘汰口径；被淘汰 pair 的**历史行不受影响**（已落盘） |
| 采样/落盘期间进程退出 | 清理路径 flush 一次；丢最后不足 `flush_interval` 的增量可接受 |
| SQLite 写失败 | 记 exception，不中断采样（只丢本轮持久化） |
| `geoip.enabled=false` | 地图/地球相关路由不注册；流量页照常可用，geo 列显示 `-` |
| 历史表无限增长 | `prune_traffic_history` 按保留期清理（每轮采样内存节流触发） |

## 6. 前端契约（`frontend/src/types/api.ts`，与实现一致）

```ts
export interface TrafficLiveEntry {
  bind: string; host: string;
  ip: string | null; geo: GeoInfo | null;
  connections: number;                    // 此刻开着的连接数
  up_rate: number; down_rate: number;     // 聚合 EMA（B/s）
  first_seen: number; last_seen: number;  // 首连/最近会话建立时刻（秒）
}
export interface TrafficLiveResponse { entries: TrafficLiveEntry[]; }

export interface TrafficHistoryEntry {
  bind: string; host: string;
  ip: string | null; geo: GeoInfo | null;
  upload: number; download: number;       // 该条目累计字节
  last_seen: number;
}
export interface TrafficHistoryResponse {
  entries: TrafficHistoryEntry[];
  total_entries: number;                  // 库中总数（entries 受下发上限截断）
}
```

## 7. 测试要点

- `tests/use_cases/test_traffic_stats.py`（新增）：字节累计与速率/EMA 差分；连接计数增删
  （含异常路径钳零）；pair 容量淘汰；采样过期清理；IP 切换时旧行冻结、新行以当前累计为基线；
  落盘节流（未到 `flush_interval` 不写）。
- `tests/interfaces/test_sqlite_store.py`（增补）：`traffic_history` upsert / 按 (bind,host,ip)
  分区 / `prune_traffic_history`。
- `tests/interfaces/test_proxy_server.py`（改造）：`modstat` 的四条分支分别上报到用例
  （用 mock 用例断言调用参数），确认连接事件不再被丢弃。
- `tests/framework/test_web_api.py`（增补）：两个新路由的响应形状；`geoip` 关闭时 `geo=null`。
- 前端：`npm run test` + `npm run build`（vue-tsc）；流量页手动验收（实时/历史切换、增量、
  排序、空态）。

## 8. 分阶段计划

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | `TrafficStatsUseCase` + store 端口 + `traffic_history` 表；`PproxyServer` 钩子改接；引用点全量更新 | ✅ |
| 2 | 连接级统计 + 两个 API + 旧访问器删除 | ✅ |
| 3 | 前端流量页（两个选项卡、导航/路由） | ✅ |
| 4 | 文档同步（README / docs/architecture / docs/cli / docs/filestree / src/GUIDE / 本 LLD） | ✅ |
| 5 | 去掉流量页页头 KPI 仪表盘（连带删除 `/api/traffic/summary` 与 `connection_count()`） | ✅ |

## 8.1 实现注记（已落地）

- **host 归一化收进用例**：`normalize_host` 从 `proxy_server.py` 移到 `traffic_stats.py`，
  并在 `note_pair` / `record_bytes` / `record_conn` 入口统一调用。`PproxyServer` 只透传 pproxy
  给的原始 `host_name`，不做业务判断（`i ∈ {4,5}` 的连接事件同样原样转发）。代价是每块数据多
  一次字符串归一化，相对 socket 转发可忽略；换来的是用例 API 自洽、不依赖调用方纪律。
- **增量累加落盘**：`SqliteProxyStore.add_traffic_history` 用 `ON CONFLICT DO UPDATE` +
  SQL 侧 `upload = upload + excluded.upload`。`TrafficStatsUseCase.flush(now, force=False)`
  按 `flush_interval` 节流，`serve` 退出路径调用 `force=True` 落掉最后一段；写失败保留 pending
  下次重试（只记 exception，不中断采样）。
- **连接计数的钳零**：`record_conn` 在计数跌破 0 时钳到 0 并记 WARNING；归零即删 key 与
  `first_seen`（不保留余温）。
- **`live_connections()` 顺带查 IP**：活跃连接数量级小，方法内直接批量查一次探测缓存拿
  `(bind, host) → ip`，免去 API 层再拼一次；地理信息仍由 API 层经 `IpGeoUseCase.resolve` 补。
- **现有测试迁移**：统计相关的三个测试类从 `tests/interfaces/test_proxy_server.py` 迁到
  `tests/use_cases/test_traffic_stats.py`（改为直测用例）；原文件保留 `TestModstatForwarding`
  验证四条分支的转发契约。`test_web_api.py` 的 `make_ctx` 把统计 mock 挂到 `ctx.traffic`。
- **去掉页头 KPI 仪表盘**：流量页顶部原有的 KPI 卡片（活跃连接数 / 历史条目数 / 累计上行 /
  累计下行）只服务于那张卡，与下面的实时/历史两张列表信息重复，故整块去掉，页面只留列表
  （两个选项卡合并进同一张卡的卡头）。它专属的 `/api/traffic/summary` 与
  `TrafficStatsUseCase.connection_count()` 随之成为死代码，一并删除、不留兼容接口；
  历史条目总数改由列表卡头的提示文案给出（复用 `/api/traffic/history` 自带的
  `total_entries`，不新增请求）。仪表盘（`DashboardPage`）的流量 KPI 走 `/api/status` 的
  `traffic_total`，与本页无关，未受影响。

## 9. 实现取值说明

原第 9 节的三点按建议值实现（用户答复"执行"，未另行指定）：

1. **保留期与条数上限**：进配置 `config/panel.toml` 的 `[traffic]`（`retention` / `history_limit`），
   与 `[globe]` 同风格；改配置即生效，不需要改代码。
2. **实时页先只做全量列表**（不提供按代理/域名筛选），列内自带连接数与速率，按连接数降序。
3. **落盘节流**：`[traffic].flush_interval`；`serve` 退出路径的 `flush(force=True)`
   不受节流约束，最后一段增量不丢。
