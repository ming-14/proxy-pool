# LLD：3D 地球（globe.gl）与目标服务器地区探测

> 状态：阶段 0~3 已实现（后端探测链路 + 前端地球）；阶段 4 文档已同步，真实环境验收待人工完成
> 关联：`src/interfaces/proxy_server.py`、`src/framework/serve.py`、`src/framework/web/api.py`、`frontend/src/pages/DashboardPage.vue`

## 0. 决策记录（已与用户对齐）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 飞线画法 | **两段弧分别成线**：`本机→代理`（按代理聚合速率）、`代理→目标`（按 (代理,目标) 流量），各自编码粗细与流速 |
| D2 | 目标归属缓存键 | **(代理 bind, 目标域名)**——不同出口的 CDN 解析结果不同，弧线指向该代理视角的真实落点 |
| D3 | 本机（"我们"）位置 | **仅手填坐标**（`panel.toml [globe] self_lat/self_lng`）；不发起任何自动探测；未配置时不画本机节点与第一段弧 |
| D4 | 探测激活时机 | **面板打开时激活**：最近 `activation_window` 秒内有 `/api/globe` 请求才消耗代理做 DoH 探测（仍限并发）；平时只记录，缓存持久 |

默认口径（未特别反对即按此实现）：

- 线宽 ∝ 该弧聚合速率的 **EMA**（平滑，避免抖动）；流速 ∝ **瞬时速率**（最近一个采样窗口的差分）。
- 代理节点只画**代理服务队列成员（含锁定代理）**——只有它们承载 serve 流量。
- `代理→目标` 弧按 **(代理, 目标城市)** 聚合后下发（hosts 列表进 tooltip），弧数封顶 `max_flows`。
- 本机位置经 geoLite2 得到；若地理库关闭（`geoip.enabled=false`）整个地球功能不可用（不注册端点、前端隐藏）。

## 1. 背景、目标与范围

### 1.1 需求

1. 仪表盘最底部展示 globe.gl 地球：每个代理一个亮点；实时流量飞线（流动光点，流量越大流动越快）；流量飞线（`本机→代理→目标`，流量越大线越粗）。
2. 新增目标服务器地区探测队列：程序把请求记下来，扔进队列处理，限制并发，**用对应的代理做 DNS 查询**，结果缓存。
3. 地球用上述数据绘制。

### 1.2 现状与调研结论

| 项 | 结论 |
|---|---|
| 请求级钩子 | **已存在**：vendor 内化的 pproxy `stream_handler` 在选定上游后调 `modstat(user, remote_ip, host_name, roption)`——`host_name`=目标域名/IP，`roption.bind`=命中上游地址（vendor 已内化 roption 参数）。目前 `PproxyServer._make_modstat` 只按 bind 累计字节。 |
| 流量数据 | 逐字节计数经 modstat 按方向分发；`traffic_sample_loop` 每 2s 采样。加 `(bind, host)` 维度只需 O(1) dict 写入，协议层零改动。 |
| 地理数据管道 | `GeoLite2Locator`（`interfaces/geoip_client.py`）+ `ip_geo_cache` 表 + `IpGeoUseCase`（三步编排：query_cache/locate/save_cache、TTL、国家中心坐标兜底）直接复用。 |
| 目标地区解析 | HTTP/SOCKS 代理**无法回读远端解析出的 IP**（SOCKS5 `rdns=True` 只有远端解析语义、HTTP 代理在隧道内）。唯一现实路径：**通过该代理请求 DoH JSON**（DNS 请求从代理出口发出），再对返回的 A 记录做 GeoLite2 定位。 |
| DoH 端点 | Cloudflare `https://cloudflare-dns.com/dns-query?name=<host>&type=A`（`Accept: application/dns-json`）；Google `https://dns.google/resolve`；阿里 `https://223.5.5.5/resolve`（国内出口可用，缓解被墙）。响应为 JSON，`Answer[]` 中 `type==1` 的 `data` 即 A 记录（已查官方文档确认存在）。 |
| globe.gl | npm 最新 **2.46.2**，与 `reference/globe.gl` 检出一致；`three` 是直接依赖（>=0.179 <1）。所需 API 全部现成：`pointsData`、`arcsData`（`arcStroke`/`arcDashLength`/`arcDashGap`/`arcDashAnimateTime` 均为逐弧 accessor）、`polygonsData`、`pauseAnimation()/resumeAnimation()`、`getScreenCoords` 等。**不需要改引擎源码**。 |
| 离线纹理 | 官方示例贴图走 CDN，桌面/离线环境不可用。改用现有 `frontend/public/world.json`（标准 GeoJSON FeatureCollection，~250 features）喂 `polygonsData` 做**矢量地球**，零额外资源且与主题一致。 |
| 测试基建 | `tests/e2e/mock_upstream.py` 支持自定义响应体（可模拟 DoH JSON）；web e2e 用临时配置目录 + serve 子进程；前端 vitest 可测纯映射函数（无 WebGL）。 |
| 前端构建 | Vite 产物输出到 `src/framework/web/static/`；globe 组件用动态 import 拆独立 chunk（three 约 1MB，gzip ~250KB），桌面打包随之带上。 |

### 1.3 目标 / 非目标

目标：仪表盘底部可交互 3D 地球，实时反映各代理的流量与「代理→目标地区」分布；目标地区经对应代理 DoH 解析并缓存（`.cache.db`），面板打开时才探测。

非目标（v1 不做，记录备查）：UDP（池不支持）；每会话一个飞行事件（连接级事件流）；目标端口粒度；自动探测本机公网 IP；历史回放。

## 2. 总体数据流

```
客户端请求 → pproxy stream_handler → modstat(bind, host)
    ├─ 既有：逐方向字节计数 → 2s 采样 → 队列页/总流量
    └─ 新增：_note_target(bind, host)（last_seen）+ per-pair 累计字节
                 │
                 ▼ 2s 采样（PproxyServer.sample_traffic 内）
        per-pair EMA 速率 + 瞬时速率 + 过期清理（METRICS_WINDOW）
                 │
                 ▼ 探测循环（serve，仅激活时）
        TargetGeoUseCase.probe_cycle：候选 = 窗口内活跃 pair ∩ 队列 bind
            → 缓存有效性判定（target_geo_cache）
            → DoH 穿该代理（并发信号量 probe_concurrency）
            → A 记录 → IpGeoUseCase.resolve → 写 target_geo_cache
                 │
                 ▼
GET /api/globe（5s，前端）← self 坐标 + 队列节点(geo) + flows(pair→城市聚合, top N)
                 │
                 ▼
GlobePanel.vue（懒加载）→ globe.gl：点 + 两层弧（细/粗、快/慢）
```

## 3. 后端设计

### 3.1 PproxyServer：per-pair 流量与活跃目标（interfaces 层）

新增内存结构（全部在事件循环线程，无锁）：

```python
_MAX_PAIRS = 2000                                     # pair 容量上限，防浏览产生无限 key
_pair_last_seen: dict[tuple[str, str], float]         # (bind, host) → 最近一次会话建立时间（modstat 时写入）
_pair_upload / _pair_download: dict[tuple[str, str], int]   # 累计字节
_pair_rate_ema: dict[tuple[str, str], tuple[float, float]]  # (up_rate, down_rate) EMA，线宽用
_pair_rate_inst: dict[tuple[str, str], tuple[float, float]] # 最近一个采样窗口的瞬时速率，流速用
_pair_prev: dict[tuple[str, str], tuple[int, int]]    # 上次采样累计值（算瞬时差分）
```

- `_make_modstat` 内：每次会话开始 `_note_target(bind, host)`；按方向的 lambda 改为 `_add_upload(bind, host, n)` / `_add_download(bind, host, n)`。
- `sample_traffic()`（每 2s 已存在）扩展：对每个 pair 差分 → 瞬时速率与 EMA（α=0.3）；清理 `last_seen` 超过 `metrics_window` 的 pair（连带三个 dict）。
- host 归一化：小写、去首尾空白/结尾点；空串跳过。
- 新增访问器 `active_pairs(since_ts)` → `[{bind, host, up, down, up_rate, down_rate, up_inst, down_inst, last_seen}]`（供探测候选与 /api/globe；EMA 用于线宽，瞬时用于流速）。
- 容量控制：超 `_MAX_PAIRS` 时按 `last_seen` 最旧淘汰（三个 dict 同步删）。

### 3.2 SQLite：`target_geo_cache` 表（interfaces/sqlite_store.py）

```sql
CREATE TABLE IF NOT EXISTS target_geo_cache (
    bind         TEXT NOT NULL,     -- 上游代理地址（bind_of，非完整 URL）
    host         TEXT NOT NULL,     -- 归一化目标域名（IP 字面量原样）
    ip           TEXT,              -- DoH 解析出的 A 记录（取首个可用）
    country      TEXT,
    country_code TEXT,
    province     TEXT,
    city         TEXT,
    lat          REAL,
    lng          REAL,
    cached_at    REAL NOT NULL,     -- 解析完成时间（TTL 判定在用例层）
    PRIMARY KEY (bind, host)
);
```

方法：`get_target_geo_batch(pairs)`（批量读，返回 `{(bind,host): row}`）、`save_target_geo_batch(rows)`、`prune_target_geo(before_ts)`（删除长期未刷新的行，返回删除数）。geo 为空的"已探测无结果"行**照常落盘**（TTL 内不重复探测），地球只使用 `lat/lng` 非空的行。表随 `_SCHEMA` 脚本自动创建，无迁移。

### 3.3 端口（domain/ports.py）

```python
class HostResolver(Protocol):
    """域名 → A 记录（穿代理的远端解析；失败抛异常，空列表=有效但无结果）"""

    async def resolve(self, host: str, *, proxy: str) -> list[str]: ...


class TargetGeoStore(Protocol):
    """(代理, host) 目标归属缓存端口：TTL 判断在用例层，store 只做纯读写"""

    def get_target_geo_batch(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]: ...
    def save_target_geo_batch(self, rows: dict[tuple[str, str], dict]) -> None: ...
    def prune_target_geo(self, before_ts: float) -> int: ...
```

### 3.4 DohResolver（interfaces/doh_resolver.py，新增）

- 复用 `AsyncHttpGetter` 的代理链路（http/https 用共享 session `proxy=`；socks 用 `ProxyConnector(rdns=True)` 临时 session），证书策略复用 `proxy_scheme.verify_target(proxy)`。
- 实现方式：把 `AsyncHttpGetter` 内部请求抽成可返回状态的公共路径，**新增 `get_json(url, *, timeout, proxy, verify)`**（现有 `get` 行为不变，check_proxies 不受影响）。
- `DohResolver.resolve(host, proxy)`：按配置的 `doh_endpoints` 顺序尝试 GET `?name=<urlencoded>&type=A`（`Accept: application/dns-json`），取响应 JSON `Answer[]` 中 `type==1 && data` 的地址列表（去重，最多 3 个）；全部端点失败抛最后一个异常；HTTP 非 200 / 非 JSON 视为失败。
- 时区/编码无关；IDN 域名先 `idna` 编码（标准库 `encode("idna")`）。
- 日志：DEBUG 逐 pair 结果（端点数/耗时）；连续失败由用例层退避，不在适配器内重试。

### 3.5 TargetGeoUseCase（use_cases/target_geo.py，新增）

```python
class TargetGeoUseCase:
    def __init__(self, *, server, store, resolver, geo, proxy_lookup,
                 probe_concurrency, cache_ttl,
                 activation_window, probe_window, prune_after): ...

    # 激活
    def touch(self) -> None                     # /api/globe 每次请求调用
    def is_active(self) -> bool                 # now - last_touch <= activation_window

    # 探测
    async def probe_cycle(self) -> int          # 返回本轮发起的探测数；未激活直接 0

    # 供 API 组装
    @staticmethod
    def aggregate_flows(pairs, target_rows, max_flows) -> list[dict]
```

> 实现注记：`probe_timeout` 由 DohResolver 构造持有（HostResolver 接口不含超时参数）；
> `max_flows` 是 `aggregate_flows` 的参数（API 从 `config.GLOBE_MAX_FLOWS` 传入），
> 均不进用例构造器。`probe_cycle` 的跳过判定放在候选过滤阶段——跳过项不占每轮
> `probe_concurrency * 2` 的探测名额。

`probe_cycle` 流程：

1. `is_active()` 为假 → 返回 0（只记录，不探测）。
2. 候选 = `server.active_pairs(now - probe_window)` 中 `proxy_lookup(bind)` 非空的 pair（代理已离队即跳过）。
3. 批量读缓存 → 过滤出**缺失或超出 `cache_ttl`** 的候选。
4. 过滤内存退避表（`pair → next_attempt_ts`；失败退避 60s 起指数翻倍，上限 15min，成功清零）。
5. 按 `last_seen` 降序取前 `probe_concurrency * 2` 个，`asyncio.Semaphore(probe_concurrency)` 并发处理：
   - host 是 IP 字面量 → 跳过 DoH，直接定位；内网/保留地址（`ipaddress` 判定）/单标签主机名（无点、非 IP）/`.local` 后缀 → 跳过且不落缓存（记 DEBUG）。
   - 否则 `await resolver.resolve(host, proxy=proxy)`；空结果 → 落"无结果"行（`ip/geo` 全 NULL，`cached_at=now`）。
   - 对 A 记录逐个（最多 2 个）调 `await geo.resolve([ip], label="target")`，取首个定位成功的结果写行。
6. 每小时一次 `store.prune_target_geo(now - prune_after)`（内存节流，不额外配置循环）。

`aggregate_flows`（纯函数，可单测）：pair 速率按 `(bind, country_code|city)` 分组求和，`hosts` 收集（每个分组最多 8 个，按速率降序），输出按总速率降序截断到 `max_flows`；无坐标的缓存行不产出弧。

### 3.6 配置（config/panel.toml 新增 `[globe]`）

```toml
[globe]
enabled = true                 # 依赖 geoip.enabled；false 时不注册 /api/globe、不探测
self_lat = 0.0                 # 手填本机坐标（决定 self 节点与第一段弧）；两者缺省=不画本机
self_lng = 0.0
max_flows = 60                 # /api/globe 下发的最大弧分组数（按速率取 top N）
probe_concurrency = 3          # DoH 探测并发上限
probe_timeout = 8              # 单次 DoH 超时秒
target_cache_ttl = 86400       # (代理,目标) 缓存有效期秒（过期且仍在流量中 → 重探）
activation_window = 60         # 最近多少秒内有 /api/globe 请求才探测
probe_window = 600             # 窗口内有流量的 pair 才作为探测候选
prune_after = 2592000          # 缓存行清理阈值秒（30 天未刷新即删）
doh_endpoints = ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve", "https://223.5.5.5/resolve"]
```

`config.py`：`_SECTION_OWNERSHIP["panel.toml"]` 增加 `globe`；解析并校验（`enabled` 布尔；`self_lat/lng` 必须成对且范围合法；`doh_endpoints` 非空且 http(s)；数值键为正）。缺省值按上表（键全部可选，与 `[geoip]` 风格一致）。

### 3.7 serve 接线（framework/serve.py）

- `_run` 内在构造 `PproxyServer` 后组装（仅 `config.GLOBE_ENABLED and c.geo is not None`）：
  `target_geo = TargetGeoUseCase(server=server, store=c.store, resolver=..., geo=c.geo, proxy_lookup=..., ...)`
  `proxy_lookup(bind)` 复用面板队列行口径（含锁定代理，`bind_of` 匹配）。
- 新增第三个周期任务 `target_probe_loop`（间隔 2s，与心跳/采样并列）：未激活时直接休眠；异常只告警不中断；退出时 cancel。
- `PanelContext` 增加 `target_geo`（None = 功能关闭）。

> 实现注记（已落地）：`DohResolver` 随 `TargetGeoUseCase` 在 `serve._run` 内构造
> （serve 专属依赖，不进容器——所有 CLI 命令都组装容器，挂上去只增面）；
> `proxy_lookup` 复用 `StandbyQueueUseCase.queue_rows()`——本次把"锁定代理置首 +
> 按地址去重"的队列行口径从 web/api 下沉到用例，api 与 serve 共用同一实现
> （原 `_standby_rows(ctx)` 已删除）。

### 3.8 API（framework/web/api.py）

- `build_app`：`ctx.target_geo is not None` 时注册 `GET /api/globe`（与 `geo_enabled` 路由同模式）。
- `/api/globe` handler：`ctx.target_geo.touch()` → 组装响应：
  - `self`: `{lat, lng}` 或 `null`（来自配置）。
  - `nodes`: 队列行每行 → geo（含经纬度，`ctx.geo.resolve`，缓存毫秒级）+ `latency/pinned/locked/consecutive_fails` + 该 bind 的聚合速率（`server.active_pairs` 按 bind 求和）。
  - `flows`: `ctx.server.active_pairs(now - METRICS_WINDOW)` ∩ 当前队列 bind，批量读 `target_geo_cache`，`aggregate_flows(...)` top N。
  - `probed_pairs` / `pending_pairs`: 观测用计数（可选，便于排查）。
- `/api/status` 增加 `globe_enabled`（前端据此显示/隐藏卡片，复用 `geo_enabled` 模式）。
- 响应体量控制：nodes ≤ 队列上限（~30），flows ≤ `max_flows`（60），单次 < 20KB。

> 实现注记（已落地）：flows 组装经 `TargetGeoUseCase.build_flows(pairs, max_flows)`
> （批量读缓存 + 读失败降级：读失败只丢本帧目标弧，不把整个地球请求打成 500）；
> nodes 的归属字段带经纬度（`_geo_point`，地图/归属路由的 `_geo_payload` 故意不带）；
> `probed_pairs` / `pending_pairs` 为可选项，暂未实现。

## 4. 前端设计

### 4.1 依赖与构建

- `cd frontend && npm install globe.gl`（引入 three / three-globe / three-render-objects；npm 最新 2.46.2 与 reference 一致）；`package-lock.json` 同步提交。
- `GlobePanel.vue` 经 `defineAsyncComponent(() => import(...))` 动态加载 → three 独立 chunk；主 chunk 不膨胀。
- 产物仍输出 `src/framework/web/static/`（`npm run build` 必须通过 vue-tsc），桌面打包自动携带。

### 4.2 组件与渲染（frontend/src/components/globe/）

- `GlobePanel.vue`：
  - 卡片标题「3D 地球」；`self` 未配置时显示一行提示（说明 `panel.toml [globe] self_lat/self_lng`）。
  - `IntersectionObserver`：滚到可见才初始化 globe（懒加载+省 CPU）；不可见时 `pauseAnimation()`，可见 `resumeAnimation()`；`document.hidden` 同样暂停并停轮询。
  - 数据轮询 `/api/globe` 固定 5s（不进 2s status 热路径）；组件卸载停止并 `dispose`（globe.gl `_destructor()`）+ 断开 RO。
  - `ResizeObserver` 跟随容器尺寸；主题切换重建配色（读 CSS 变量，同 `GeoMap.vue` 口径）。
  - WebGL 不可用/初始化异常：捕获并渲染降级文案（不弹全局错误）。
  - 地球：`polygonsData(world.json.features)` 矢量国家（深色 cap + 无侧面）+ `showGraticules(true)` + `backgroundColor('rgba(0,0,0,0)')`；`pointOfView({lat: 30, lng: 110, altitude: 2.2})`。
- `globeData.ts`（纯函数，vitest 单测）：
  - `buildPoints(self, nodes)` → 代理点数据（含颜色/大小/标签；self 单独色）。
  - `buildProxyArcs(self, nodes)` → 第一段弧（本机→代理）。
  - `buildTargetArcs(flows)` → 第二段弧（代理→目标城市）。
  - `rateToStroke(rate, maxRate)`：`0.15 ~ 0.9`（对数归一，单调）；
  - `rateToDashTime(rate)`：`800 ~ 12000` ms（速率越大越小=越快）；
  - `arcDotStyle = { dashLength: 0.02, dashGap: 0.96 }`（短 dash + 长 gap = 流动光点）。
  - 全部函数空数据/0 速率/缺坐标时安全返回（不产出 NaN）。
- `types/api.ts` + `api/client.ts`：`GlobeData` / `GlobeNode` / `GlobeFlow` 类型与 `api.globe()`。
- `DashboardPage.vue`：`<GlobePanel v-if="status.globe_enabled" />` 挂到最底部（GeoMap 之后）。

### 4.3 视觉映射

| 元素 | 数据 | 映射 |
|---|---|---|
| 代理亮点 | 队列成员 geo | 固定半径；颜色区分 锁定/健康(consecutive_fails=0)/异常；hover 标签=bind+归属+延迟 |
| 本机节点 | 手填坐标 | 独立颜色（如青色），标签「本机」 |
| 弧 1：本机→代理 | 每节点聚合速率 | 线宽 ∝ 聚合 EMA；dash 流速 ∝ 瞬时；无本机坐标时整层隐藏 |
| 弧 2：代理→目标 | flows 分组 | 线宽 ∝ 分组 EMA；dash 流速 ∝ 瞬时；tooltip=城市+hosts+速率 |
| 目标点 | flows 坐标 | 小圆点（可并入弧终点渲染；不额外画标签层） |

### 4.4 实现注记（已落地）

- **文件落点**：`frontend/src/components/globe/globeData.ts`（纯函数 + 视觉常量，Vitest 直测）、
  `frontend/src/components/globe/GlobePanel.vue`（渲染/可见性/主题/降级）、
  `frontend/src/types/api.ts`（`GlobeData`/`GlobeNode`/`GlobeFlow`/`GlobeGeo`，`StatusSnapshot.globe_enabled`）、
  `frontend/src/api/client.ts`（`api.globe()`）、`frontend/src/pages/DashboardPage.vue`（`defineAsyncComponent` + `v-if="status.globe_enabled"` 挂到 GeoMap 之后）。
- **映射常量**：`MIN_STROKE=0.15`/`MAX_STROKE=0.9`（对数归一分母取画面内最大速率）、
  `DASH_TIME_SLOW_MS=12000`/`DASH_TIME_FAST_MS=800`、`DASH_RATE_REF=1_000_000`（流速参考固定，
  不随画面最大值漂移）、`ARC_DOT_STYLE={dashLength:0.02, dashGap:0.96}`。
- **弧组装**：`buildProxyArcs` 无 `self` 返回空（整层隐藏）；`buildTargetArcs` 以节点 `bind → 坐标`
  关联起点，bind 已离队/坐标缺失不产弧——即第二段弧只画「队列内代理 → 其目标城市」。
- **激活链路**：滚动进入视口才 `import('globe.gl')` + 拉 `world.json`（模块级 Promise 缓存），
  同时开始 5s 轮询 `/api/globe`；不可见或 `document.hidden` 时 `pauseAnimation()` + 停轮询，
  于是停止 touch、后端探测随之休眠（D4 的整条链路闭合）。
- **实测构建体积**：`globe.gl` chunk 1,931.90 kB（gzip 547.56）独立 chunk，
  DashboardPage 621.38 kB，主 chunk 187.08 kB——`WebGLRenderer` 只在 globe chunk 中，主 chunk 不含 three。

## 5. 错误处理与边界

| 场景 | 处理 |
|---|---|
| DoH 超时/非 200/非 JSON/无 Answer | 该 pair 记失败，内存退避（60s→15min 指数），不落缓存行 |
| DoH 成功但无 A 记录 | 落 `ip/geo=NULL` 行，TTL 内不再探测 |
| A 记录存在但 GeoLite2 未命中（保留段等） | 落 `ip` 非空、geo NULL 行；地球不使用 |
| host 是 IP 字面量 | 跳过 DoH，直接定位；内网/保留地址跳过不落盘 |
| 单标签主机名 / `.local` 后缀 | 跳过（通常是内网名），不落盘 |
| IDN | `idna` 编码后查询；失败跳过 |
| bind 已不在队列（代理淘汰/锁定变化） | 候选过滤跳过；已有缓存行保留（同一 bind 回归可复用） |
| `geoip.enabled=false` 或 `[globe].enabled=false` | 不构造用例、不注册路由、`globe_enabled=false`，前端隐藏 |
| `web.enabled=false` | 无面板 → 无人 touch → 永不探测（只记录内存，无落盘） |
| 缓存写失败 | 只丢本次持久化、不中断探测循环（同 `IpGeoUseCase` 降级口径），记 exception |
| 自填坐标只填一个 | 配置校验直接报错（启动期暴露） |
| WebGL 不可用 | 组件降级文案；不影响面板其余功能 |
| 浏览器时钟偏差 | 前端不参与时间判定（激活窗口/采样全在服务端） |

## 6. 性能与资源估算

| 项 | 估算 |
|---|---|
| modstat 热路径增量 | 每会话 1 次 dict 写（last_seen）+ 每次转发 O(1) 计数；峰值 pair 2000 → 三个 dict 常驻 < 1MB |
| 采样循环增量 | 每 2s 遍历活跃 pair（≤2000）做差分/EMA/清理，< 1ms |
| DoH 探测 | 并发 ≤ probe_concurrency（3）；每 pair 每 TTL（24h）一次；失败退避封顶 15min；激活窗口外零流量 |
| `/api/globe` | ≤ 30 nodes + ≤ 60 flows，< 20KB/次，5s 一次（仅仪表盘可见时） |
| 前端渲染 | 60 弧 + ~60 点；数据 5s 刷新、动画客户端连续；不可见/后台暂停；globe.gl 官方能力范围内 |
| Bundle | three chunk ~1MB（gzip ~250KB），懒加载；主 chunk 不变 |
| 磁盘 | 每行 ~200B；上千 pair + 30 天清理 → 亚 MB 级 |

## 7. 测试要点

### 7.1 后端单测

| 文件 | 用例 |
|---|---|
| `tests/interfaces/test_proxy_server.py`（增补） | modstat 写 pair last_seen/字节；采样后 EMA/瞬时速率正确；容量淘汰；过期清理；`active_pairs` 字段 |
| `tests/interfaces/test_sqlite_store.py`（增补） | `target_geo_cache` upsert/批量读/主键分区/`prune_target_geo` |
| `tests/interfaces/test_doh_resolver.py`（新增） | mock AsyncHttpGetter：端点顺序/首端点失败换下一个/JSON 解析（A 与 CNAME 混排）/非 200/坏 JSON/IDN |
| `tests/use_cases/test_target_geo.py`（新增） | 激活窗口；TTL 命中不探测；失败退避与上限；并发信号量；IP 字面量/内网/单标签跳过；无 A 结果落盘；bind 缺失跳过；`aggregate_flows` 纯函数 |
| `tests/framework/test_config.py`（增补） | `[globe]` 键校验（缺省、非法坐标、空端点表、节归属） |
| `tests/framework/test_web_api.py`（增补） | `/api/globe` 响应形状；`globe_enabled`；touch 更新；geo/globe 关闭时 404 |
| `tests/framework/test_serve.py`（增补） | probe 循环任务取消路径（如可拆） |

### 7.2 e2e（tests/e2e/）

- `test_target_geo_e2e.py`（已落地，独立于 `test_serve_loop_e2e.py`）：mock 上游配置为返回 DoH JSON 响应体 → 经本地代理发一次请求 → 轮询 `/api/globe`（≤10s）出现目标 flow；无激活请求时不探测（`target_geo_cache` 无新行）。
- web e2e（可选）：`test_panel_e2e.py` 增加地球卡片存在性断言（canvas 或降级文案；Playwright WebGL 不可用时不误报失败）。

### 7.3 前端

- `frontend/tests/unit/globe-data.test.ts`：映射单调性/上下界/空数据安全/坐标缺失容错。
- `npm run test`、`npm run build`（vue-tsc）必须通过。
- 浏览器实测：点/弧渲染、动画速度与粗细随流量变化、切页暂停、主题切换、无 console 错误。

> 实现注记（已落地）：`globe-data.test.ts` 覆盖速率映射单调性/上下界/非法值、`withAlpha` 解析与
> 回退、空数据与缺坐标容错、两段弧组装（含“只为会画的弧归一”）、dash 样式；`npm run test`
> （226 项）与 `npm run build` 均通过。**浏览器实测（真实代理 + 实际流量）尚未执行**，见阶段 4。

## 8. 分阶段计划

### 阶段 0：调研与定稿（本文档）✅

- 产出：本 LLD + 决策记录。
- 验收：四项决策已由用户确认；globe.gl/DoH/sqlite/tests 集成点全部核对完毕。

### 阶段 1：流量维度与存储（后端基座）✅

- 任务：PproxyServer per-pair 计数/速率/清理；`target_geo_cache` 表与 store 方法；`TargetGeoStore` 端口；config `[globe]` 解析与校验。
- 验收：新增单测全绿；既有 `pytest` 全绿（per-proxy 流量/队列行为无回归）。✅
- 验证：`python -m pytest tests/interfaces/test_proxy_server.py tests/interfaces/test_sqlite_store.py tests/framework/test_config.py`。✅

### 阶段 2：探测链路与 API ✅

- 任务：`AsyncHttpGetter.get_json` 抽取；`DohResolver`；`TargetGeoUseCase`；serve `target_probe_loop` 接线；`PanelContext.target_geo`；`/api/globe` + `globe_enabled`；e2e mock DoH。
- 验收：单测 + e2e 绿；未激活时不产生探测；激活后 mock DoH 结果出现在 `/api/globe`。✅
- 验证：`python -m pytest tests/` 全量（主链 923 passed / 1 skipped，浏览器 e2e 跳过）。✅

### 阶段 3：前端地球 ✅（浏览器实测待阶段 4）

- 任务：安装 globe.gl；`globeData.ts` 纯函数 + 单测；`GlobePanel.vue`（懒加载/可见性暂停/RO/降级）；types + client；DashboardPage 挂载；`npm run build`。
- 验收：`npm run test`/`npm run build` 通过（226 项）✅；主 chunk 不含 three ✅（§4.4 实测体积）；浏览器实测点、两层弧、动画与粗细随流量变化、切页与后台暂停 ⏳。
- 验证：`cd frontend && npm run test && npm run build` + 手动浏览器检查（真实 serve 或 mock）。

### 阶段 4：验收与文档 ⏳

- 任务：真实环境验收（真实代理 + 实际流量 + 手填坐标）；README/docs/architecture/docs/filestree/src/GUIDE.md 同步（含测试树）；清理隐私说明（缓存清理方式）。
- 验收：文档检查单全勾 ✅；全量 `pytest` + 前端 test/build 通过 ✅；真实链路观察到目标弧 ⏳（需人工，见 §10 头号风险）。
- 验证：`python -m pytest -v`、`cd frontend && npm run test && npm run build`。

未去做的可选顶：Playwright 地球卡片断言（§7.2 标注为可选；当前本地约定不跑浏览器 e2e）。

工作量粗估：阶段 1 ≈ 0.5~1 人日，阶段 2 ≈ 1.5~2 人日，阶段 3 ≈ 1.5~2 人日，阶段 4 ≈ 0.5~1 人日。

## 9. 文档与产物同步清单

- [x] `README.md`（功能段落 + 依赖/配置说明）
- [x] `docs/architecture.md`（新增「3D 地球与目标地区探测」一节：modstat pair 维度、探测队列、缓存表 + 隐私边界与清理方式）
- [x] `docs/cli.md`（若 serve/配置说明受影响则同步）
- [x] `docs/filestree.md`（新增文件：`use_cases/target_geo.py`、`interfaces/doh_resolver.py`、前端 globe 组件等）
- [x] `src/GUIDE.md`（测试树补新测试文件；前端单测不在 Python 树内，故不列入）
- [x] `docs/design/LLD-Globe3D.md`（本文档，实现过程中按需修订）
- [x] `frontend/` 构建产物（`npm run build` 后 `static/` 更新；不入库）

## 10. 风险与待实测项

| 风险 | 影响 | 缓解/验证 |
|---|---|---|
| 免费代理 DoH 成功率低（死代理/被墙端点） | 目标弧出现慢或缺 | 三端点回退（含国内 alidns）；失败退避；不影响其余功能；实现后统计成功率 |
| `world.json` polygons 渲染性能（~250 多边形） | 地球初始化/帧率 | 实现时实测；备选（hexPolygons / 仅经纬网 + 点弧）；必要时裁剪分辨率 |
| three 与 Vite/vue-tsc 构建兼容 | 构建失败 | 阶段 3 第一步先 `npm install + build` 空跑验证 |
| Playwright headless WebGL 不可用 | e2e 断言方式 | e2e 只断言卡片/canvas 或降级文案存在，不依赖渲染结果 |
| 目标域名落盘隐私 | 本机浏览目标暴露给能读 `.cache.db` 的进程 | TTL + 30 天清理；面板监听址可在 `[web].host` 收紧（默认 `0.0.0.0`）；README/architecture 写清关闭与清理方式（`[globe].enabled=false` 停探测；删除 `target_geo_cache` 表内容） |
| 真实代理的 DoH 成功率与渲染观感 | 目标弧数量/可读性 | 待阶段 4 人工验收；失败退避保证不拖慢其余功能 |
| 激活窗口内大量新 pair 突发 | 探测排队 | 每轮限 `concurrency*2`、按最近活跃优先、容量封顶 |
