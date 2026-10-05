# LLD：3D 地球（globe.gl）

> 状态：阶段 0~3 已实现（前端地球 + 只读后端接口）；阶段 4 文档已同步，真实环境验收待人工完成
> 关联：`src/framework/web/globe_data.py`、`src/framework/web/api.py`、`src/framework/serve.py`、
> `frontend/src/pages/DashboardPage.vue`
> **探测侧**：目标落点由「DNS 探测队列」（流量统计子模块）产出，设计见
> **`LLD-DnsProbe.md`**——本文只描述地球如何**只读**这些最终数据。
> **后续变更**：流量统计已从 `PproxyServer` 整体迁到
> `src/use_cases/traffic_stats.py::TrafficStatsUseCase`（见 `LLD-TrafficStats.md`）——
> 本文中 `PproxyServer.sample_traffic` / `PproxyServer.active_pairs` / `_pair_*` 等描述
> 均为迁移前的形态，现由 `TrafficStatsUseCase` 提供同名能力。

## 0. 决策记录（已与用户对齐）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 飞线画法 | **两段弧分别成线**：`本机→代理`（按代理聚合速率）、`代理→目标`（按 (代理,目标) 流量），各自编码粗细与流速 |
| D2 | 目标归属缓存键 | **(代理 bind, 目标域名)**——不同出口的 CDN 解析结果不同，弧线指向该代理视角的真实落点 |
| D3 | 本机（"我们"）位置 | **仅手填坐标**（`panel.toml [globe] self_lat/self_lng`）；不发起任何自动探测；未配置时不画本机节点与第一段弧 |
| ~~D4~~ | ~~探测激活时机~~ | **已废弃**（原结论：面板打开时激活）。新模型：探测改由**流量统计**事件驱动（有 (代理,域名) 会话即探测），与地球是否被查看无关；地球**不再驱动探测**，只读结果。见 `LLD-DnsProbe.md` |
| D5 | 球面渲染方式 | **three-globe 官方示例贴图（`globeImageUrl`）**，不用 ECharts 的 `world.json` 做矢量地球：贴图随 `three-globe` npm 包本地可得（`example/img/`），复制 `earth-blue-marble.jpg` 到 `frontend/public/globe/` 入库即完全离线；见 §4.5 |

默认口径（未特别反对即按此实现）：

- 线宽 ∝ 该弧聚合速率的 **EMA**（平滑，避免抖动）；流速 ∝ **瞬时速率**（最近一个采样窗口的差分）。
- 代理节点只画**代理服务队列成员（含锁定代理）**——只有它们承载 serve 流量。
- `代理→目标` 弧按 **(代理, 目标城市)** 聚合后下发（hosts 列表进 tooltip），弧数封顶 `max_flows`。
- 本机位置仅手填；若地理库关闭（`geoip.enabled=false`）整个地球功能不可用（不注册端点、前端隐藏）。

## 1. 背景、目标与范围

### 1.1 需求

1. 仪表盘最底部展示 globe.gl 地球：每个代理一个亮点；实时流量飞线（流动光点，流量越大流动越快）；流量飞线（`本机→代理→目标`，流量越大线越粗）。
2. 目标服务器地区探测：把浏览目标记下来、穿对应代理做 DNS 查询、结果缓存（**已重构为独立的 DNS 探测队列，见 `LLD-DnsProbe.md`；本文不再展开**）。
3. 地球用上述数据绘制——**只消费最终数据，不驱动探测**。

### 1.2 现状与调研结论

| 项 | 结论 |
|---|---|
| 请求级钩子 | vendor 内化的 pproxy `stream_handler` 在选定上游后调 `modstat(user, remote_ip, host_name, roption)`——`host_name`=目标域名/IP，`roption.bind`=命中上游地址。`PproxyServer` 只转发，统计在 `TrafficStatsUseCase`。 |
| 流量数据（本机视角） | 逐字节计数经 modstat 按方向分发；`traffic_sample_loop` 每 2s 采样。`(bind, host)` 维度只做 O(1) dict 写入，协议层零改动。 |
| 目标地区数据 | 由 DNS 探测队列产出并落 `dns_probe_cache`（键 `(bind, host)`）；地球**读**该表。探测定点、DoH 端点、跳过规则见 `LLD-DnsProbe.md`。 |
| 地理数据管道 | `GeoLite2Locator`（`interfaces/geoip_client.py`）+ `ip_geo_cache` 表 + `IpGeoUseCase`（三步编排：query_cache/locate/save_cache、TTL、国家中心坐标兜底）——地球节点归属与目标归属都复用。 |
| globe.gl | npm 最新 **2.46.2**，与 `reference/globe.gl` 检出一致；`three` 是直接依赖（>=0.179 <1）。所需 API 全部现成：`globeImageUrl`、`pointsData`、`arcsData`（`arcStroke`/`arcDashLength`/`arcDashGap`/`arcDashAnimateTime` 均为逐弧 accessor）、`showGraticules`/`showAtmosphere`、`pauseAnimation()/resumeAnimation()`、`getScreenCoords` 等。**不需要改引擎源码**。 |
| 离线纹理 | 贴图文件随 `three-globe` npm 包分发（`node_modules/three-globe/example/img/`）；把 4 张官方贴图降采样到 ≤2048 宽入库 `frontend/public/globe/`（含设置页缩略图 `thumb-earth-*.jpg`，合计 ~1.1MB），设置页「地球样式」任选。**默认真实地球（earth-blue-marble）**：本卡片底色是浅色，官方点位/飞线示例常用的 `earth-night`/`earth-dark` 依赖深色星空底衬，浅色卡片上会呈现为一颗暗球。**不用 `world.json` 做矢量地球**（见 §4.5）。 |
| 测试基建 | 前端 vitest 可测纯映射函数（无 WebGL）；`globe_data.aggregate_flows` 为 Python 纯函数可单测；web e2e 用临时配置目录 + serve 子进程。 |
| 前端构建 | Vite 产物输出到 `src/framework/web/static/`；globe 组件用动态 import 拆独立 chunk（three 约 1MB，gzip ~250KB），桌面打包随之带上。 |

### 1.3 目标 / 非目标

目标：仪表盘底部可交互 3D 地球，实时反映各代理的流量与「代理→目标地区」分布；目标地区数据**由流量统计侧的探测队列随时产出**（不依赖面板是否打开），地球只读。

非目标（v1 不做，记录备查）：UDP（池不支持）；每会话一个飞行事件（连接级事件流）；目标端口粒度；自动探测本机公网 IP；历史回放。

## 2. 总体数据流

```
客户端请求 → pproxy stream_handler → modstat(bind, host)
    ├─ 既有：逐方向字节计数 → 2s 采样 → 队列页/总流量
    └─ note_pair(bind, host)：记 last_seen + per-pair 累计字节，并把 pair 交给 DNS 探测队列
                 │
                 ├──（流量统计侧，随时）DNS 探测队列穿该代理 DoH → 落 dns_probe_cache
                 │                                    （详见 LLD-DnsProbe.md）
                 ▼
        per-pair EMA 速率 + 瞬时速率 + 过期清理（METRICS_WINDOW）
                 │
                 ▼
GET /api/globe（5s，前端；只读，不触发探测）
    ← self 坐标 + 队列节点(geo) + aggregate_flows(活跃 pair, dns_probe_cache 行, max_flows)
                 │
                 ▼
GlobePanel.vue（懒加载）→ globe.gl：点 + 两层弧（细/粗、快/慢）
```

## 3. 后端设计

### 3.1 流量维度的归属（interfaces + use_cases 层）

per-pair 流量维度已从 `PproxyServer` 迁到 `TrafficStatsUseCase`（见 `LLD-TrafficStats.md`）：`_pair_last_seen` / `_pair_upload` / `_pair_download` / `_pair_rate_ema` / `_pair_rate_inst` / `_pair_prev`，超 `_MAX_PAIRS=2000` 按 `last_seen` 批量淘汰、按 `METRICS_WINDOW` 清理。地球经 `ctx.traffic.active_pairs(since_ts)` 读取：`[{bind, host, up, down, up_rate, down_rate, up_inst, down_inst, last_seen}]`——EMA 供线宽、瞬时速率供流速。

> 迁移前 `PproxyServer` 的同名结构与 `sample_traffic()` 描述见文档头「后续变更」注记，现由 `TrafficStatsUseCase` 提供。

### 3.2 目标归属缓存（只读）

地球读 `dns_probe_cache` 表（键 `(bind, host)`，含 ip/国家/省/市/经纬度/`cached_at`）：

- 通过 `ctx.traffic.dns_probe_rows(pairs)` 批量读（内部走 `DnsProbeStore.get_dns_probe_batch`，读失败返回空 dict、不把地球请求打成 500）。
- 表结构、写入方（探测队列结果经流量统计 `save_dns_probe_batch` 落盘）、TTL/清理都在探测侧，见 `LLD-DnsProbe.md`。地球**只读不写**。
- geo 为空的"已探测无结果"行照常存在（TTL 内不重复探测）；地球只使用 `lat/lng` 非空的行画弧。

### 3.3 端口（domain/ports.py）

- `HostResolver`（穿代理域名 → A 记录）与 `DnsProbeStore`（探测结果缓存读写）是**探测侧**端口，定义与用法见 `LLD-DnsProbe.md`；地球不直接依赖它们。
- 地球仅依赖 `TrafficStatsUseCase` 的 `active_pairs` / `dns_probe_rows` 两个查询方法。

### 3.4 聚合函数：`aggregate_flows`（framework/web/globe_data.py，纯函数）

从原 `TargetGeoUseCase` 的静态方法平移过来，签名 `aggregate_flows(pairs, target_rows, max_flows)`：

- `pairs` = `active_pairs()` 输出；`target_rows` = `get_dns_probe_batch()` 输出。
- 按 `(bind, country_code, city)` 分组求和速率（EMA 与瞬时），`hosts` 每组按速率降序取前 `_MAX_HOSTS_PER_GROUP=8` 个。
- 无坐标（`lat`/`lng` 缺失）的行**不产出弧**；输出按总速率降序截断到 `max_flows`。
- 返回 `[{bind, country, country_code, city, lat, lng, up_rate, down_rate, up_inst, down_inst, hosts}]`。

### 3.5 配置（config/panel.toml 的 `[globe]`，精简后）

```toml
[globe]
enabled = true                 # 地球开关（依赖 geoip.enabled；false 时不注册 /api/globe）
# self_lat = 39.9              # 手填本机坐标（决定 self 节点与第一段弧）；两者缺省 = 不画本机
# self_lng = 116.4
max_flows = 60                 # /api/globe 下发的最大弧分组数（按速率取 top N）
```

- `config.py` 常量只保留 `GLOBE_ENABLED` / `GLOBE_SELF_LAT` + `GLOBE_SELF_LNG`（须成对、范围合法）/ `GLOBE_MAX_FLOWS`。
- 原 `[globe]` 里的探测参数（`probe_concurrency` / `probe_timeout` / `target_cache_ttl` / `activation_window` / `probe_window` / `prune_after` / `doh_endpoints`）**已删除**，全部迁到 `[dns_probe]`（见 `LLD-DnsProbe.md`）。

### 3.6 serve 接线（framework/serve.py）

- `serve._run` 内构造 `DnsProbeQueue`（仅 `config.DNS_PROBE_ENABLED`）+ `TrafficStatsUseCase`（注入 `dns_probe_store=c.store`、`dns_probe=...`），再 `dns_probe.set_sink(traffic.note_probe_result)`；起常驻消费者任务 `asyncio.create_task(dns_probe.run())`。详见 `LLD-DnsProbe.md`。
- `PanelContext` **去掉 `target_geo` 字段**；`/api/globe` 的注册条件与 `status.globe_enabled` 都改为 `config.GLOBE_ENABLED and ctx.geo is not None`。
- 删除 `target_probe_loop` / `TARGET_PROBE_INTERVAL`（探测改由流量统计侧驱动，地球不再有独立周期任务）。

### 3.7 API（framework/web/api.py::`_globe`）

- `build_app`：`config.GLOBE_ENABLED and ctx.geo is not None` 时注册 `GET /api/globe`。
- handler **不调 `touch()`、不下发 `stats`**，只组装：
  - `self`: `{lat, lng}` 或 `null`（来自 `[globe].self_lat/lng`）。
  - `nodes`: `ctx.standby.queue_rows()` 每行 → geo（含经纬度，`ctx.geo.resolve`，缓存毫秒级）+ `latency/pinned/locked/consecutive_fails` + 该 bind 的聚合速率（`ctx.traffic.active_pairs` 按 bind 求和）。
  - `flows`: `ctx.traffic.active_pairs(now - METRICS_WINDOW)` ∩ 当前队列 bind，批量读 `ctx.traffic.dns_probe_rows(...)`，`aggregate_flows(...)` top N。
- `/api/status` 增加 `globe_enabled`（`config.GLOBE_ENABLED and ctx.geo is not None`，前端据此显示/隐藏卡片）。
- 响应体量控制：nodes ≤ 队列上限（~30），flows ≤ `max_flows`（60），单次 < 20KB。

> 已删除：原 `stats`（探测观测计数）字段与 `touch()` 调用——探测已与地球解耦，不再有"激活/观测"概念。

## 4. 前端设计

### 4.1 依赖与构建

- `cd frontend && npm install globe.gl`（引入 three / three-globe / three-render-objects；npm 最新 2.46.2 与 reference 一致）；`package-lock.json` 同步提交。
- `GlobePanel.vue` 经 `defineAsyncComponent(() => import(...))` 动态加载 → three 独立 chunk；主 chunk 不膨胀。
- 产物仍输出 `src/framework/web/static/`（`npm run build` 必须通过 vue-tsc），桌面打包自动携带。

### 4.2 组件与渲染（frontend/src/components/globe/）

- `GlobePanel.vue`：
  - 卡片标题「3D 地球」；`self` 未配置时显示一行提示（说明 `panel.toml [globe] self_lat/self_lng`）。
  - `IntersectionObserver`：滚到可见才初始化 globe（懒加载+省 CPU）；不可见时 `pauseAnimation()`，可见 `resumeAnimation()`；`document.hidden` 同样暂停并停轮询。**轮询与否只影响前端自身 CPU 与请求量，不再驱动后端探测**（探测由流量统计随时进行）。
  - 数据轮询 `/api/globe` 固定 5s（不进 2s status 热路径）；组件卸载停止并 `dispose`（globe.gl `_destructor()`）+ 断开 RO。
  - `ResizeObserver` 跟随容器尺寸；主题切换重建配色（读 CSS 变量，同 `GeoMap.vue` 口径）。
  - WebGL 不可用/初始化异常：捕获并渲染降级文案（不弹全局错误）。
  - 地球：球面样式（贴图/经纬网/大气光晕）由设置页「地球样式」决定（`stores/globeStyle`，纯前端 localStorage），默认 `globeImageUrl('/globe/earth-blue-marble.jpg')` + `showAtmosphere(false)`（浅色卡片上 alpha 光晕会显成蓝雾）+ `showGraticules(false)`（官方带贴图示例均不画网格）+ `backgroundColor('rgba(0,0,0,0)')`；`pointOfView({lat: 30, lng: 110, altitude: 1.8})`（≈ 球体占卡片可视高度 3/4）。
- `globeData.ts`（纯函数，vitest 单测）：
  - `buildPoints(self, nodes)` → 代理点数据（含颜色/大小/标签；self 单独色）。
  - `buildProxyArcs(self, nodes)` → 第一段弧（本机→代理）。
  - `buildTargetArcs(flows)` → 第二段弧（代理→目标城市）。
  - `rateToStroke(rate, maxRate)`：`0.15 ~ 0.9`（对数归一，单调）；
  - `rateToDashTime(rate)`：`800 ~ 12000` ms（速率越大越小=越快）；
  - `arcDotStyle = { dashLength: 0.02, dashGap: 0.96 }`（短 dash + 长 gap = 流动光点）。
  - 全部函数空数据/0 速率/缺坐标时安全返回（不产出 NaN）。
- `types/api.ts` + `api/client.ts`：`GlobeData` / `GlobeNode` / `GlobeFlow` 类型与 `api.globe()`（**已删除 `GlobeStats` 类型与探测统计脚注相关逻辑**）。
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
- **懒加载与暂停**：滚动进入视口才 `import('globe.gl')`（球面贴图由 `globeImageUrl` 自行拉取，不再拉 `world.json`），
  同时开始 5s 轮询 `/api/globe`；不可见或 `document.hidden` 时 `pauseAnimation()` + 停轮询。
  该节流**只省前端 CPU 与请求**，与后端探测无关（探测由流量统计随时进行，见 `LLD-DnsProbe.md`）。
- **实测构建体积**：`globe.gl` chunk 1,931.90 kB（gzip 547.56）独立 chunk，
  DashboardPage 621.38 kB，主 chunk 187.08 kB——`WebGLRenderer` 只在 globe chunk 中，主 chunk 不含 three。

### 4.5 球面贴图与「只剩中国可见」缺陷修复（D5）

初版把 ECharts 的 `world.json`（~1083 个 polygon）喂 `polygonsData` 画矢量地球，上线后仪表盘地球**只显示中国及数据顺序上更早的 41 个国家，其余大洲全黑**。根因：

1. `frontend/public/world.json` 的 `China` 是 MultiPolygon，第 13 个条目为 `[]`（空环，非法 GeoJSON；全文件仅此一处）；
2. three-globe 逐 polygon 构造 `ConicPolygonGeometry` → 空环在 `d3-geo` 的 `geoBounds` 抛 `TypeError: Cannot set properties of undefined (setting '0')`；
3. 多边形层用 `data-bind-mapper` 的 digest（先建全部对象、再按数据顺序逐个建几何体），异常使 update 中断——**其后的 821 个 polygon 永远拿不到几何体**；
4. kapsule 的渲染更新是 `debounce(fn, 1)`（下一事件循环执行），异常逃出组件的 `try/catch` 只记 console，因此地球仍"初始化成功"、不显示降级文案。

修复：改用官方贴图渲染球面（D5），**删除 `polygonsData` 与 `world.json` 拉取**——既回到官方示例口径，也彻底移除该崩溃面；`world.json` 仍由 2D 的 `GeoMap.vue` 使用。配套改动：

- `frontend/public/globe/`：4 张官方贴图（≤2048 宽，合计 ~1.1MB）+ 4 张设置页缩略图（`thumb-earth-*.jpg`，各 4~11KB）——卡片球体仅约 320px，4K 贴图要多占 ~33MB 显存且无视觉收益；
- `api.py` 新增白名单路由 `GET /globe/{name}`（与 `/world.json` 同模式，面板无静态目录通配；白名单外的文件名与目录穿越一律 404），缓存 `max-age=86400`（文件名无内容 hash，不能 immutable）；
- `stores/globeStyle.ts` + `components/GlobeStyleCard.vue`：设置页「地球样式」（贴图 4 选 1 + 经纬网/大气光晕开关，localStorage 持久化，`GlobePanel` 监听后即时刷新球面）；
- 单测：`tests/framework/test_web_api.py::test_globe_texture_served`。

## 5. 错误处理与边界

| 场景 | 处理 |
|---|---|
| `dns_probe_cache` 读失败 | 只丢本帧目标弧（`dns_probe_rows` 读失败返回空 dict），不把地球请求打成 500 |
| 目标缓存行 lat/lng 为空 | 不产出弧。地球只画有坐标的行 |
| bind 已不在队列（代理淘汰/锁定变化） | 不在 nodes；flows 已按队列 bind 过滤 |
| `geoip.enabled=false` 或 `[globe].enabled=false` | 不注册路由、`globe_enabled=false`，前端隐藏（不影响探测本身） |
| 自填坐标只填一个 | 配置校验直接报错（启动期暴露） |
| WebGL 不可用 | 组件降级文案；不影响面板其余功能 |
| 浏览器时钟偏差 | 前端不参与时间判定（采样全在服务端） |

## 6. 性能与资源估算

| 项 | 估算 |
|---|---|
| `/api/globe` | ≤ 30 nodes + ≤ 60 flows，< 20KB/次，5s 一次（仅仪表盘可见时）；只读，无外网、无写库 |
| 前端渲染 | 60 弧 + ~60 点；数据 5s 刷新、动画客户端连续；不可见/后台暂停；globe.gl 官方能力范围内 |
| Bundle | three chunk ~1MB（gzip ~250KB），懒加载；主 chunk 不变 |
| 内存 | 地球自身不新增后端常驻结构（复用流量统计的 pair 表与 `dns_probe_cache`） |

> 探测侧的性能（DoH 并发/退避/磁盘体量）见 `LLD-DnsProbe.md`。

## 7. 测试要点

### 7.1 后端

| 文件 | 用例 |
|---|---|
| `tests/framework/test_globe_data.py`（新增） | `aggregate_flows` 纯函数：分组求和、hosts 上限与降序、无坐标不产弧、按总速率截断 |
| `tests/framework/test_web_api.py`（增补） | `/api/globe` 响应形状（含无 `stats`）；`globe_enabled`；geo/globe 关闭时 404；`/globe/{name}` 贴图路由（白名单内 200 + image/jpeg + 一天缓存，白名单外/穿越尝试 404） |
| `tests/framework/test_config.py`（增补） | `[globe]` 键精简后的校验（缺省、非法坐标、节归属） |

> 探测队列、DoH、`dns_probe_cache`、`[dns_probe]` 的测试见 `LLD-DnsProbe.md`。

### 7.2 e2e（tests/e2e/）

- `test_dns_probe_e2e.py`（见 `LLD-DnsProbe.md`）：**不发任何 `/api/globe` 请求**，仅发一次经代理的目标请求 → `dns_probe_cache` 落盘；随后 `/api/globe` 读到该目标弧（证明地球只读最终数据）。
- web e2e（可选）：`test_panel_e2e.py` 增加地球卡片存在性断言（canvas 或降级文案；Playwright WebGL 不可用时不误报失败）。

### 7.3 前端

- `frontend/tests/unit/globe-data.test.ts`：映射单调性/上下界/空数据安全/坐标缺失容错。
- `frontend/tests/unit/globe-style.test.ts`：地球样式偏好的默认值、非法/坏 JSON 回退、持久化与恢复默认。
- `npm run test`、`npm run build`（vue-tsc）必须通过。
- 浏览器实测：点/弧渲染、动画速度与粗细随流量变化、切页暂停、主题切换、设置页改样式即时生效、无 console 错误。

> 实现注记（已落地）：`globe-data.test.ts` 覆盖速率映射单调性/上下界/非法值、`withAlpha` 解析与
> 回退、空数据与缺坐标容错、两段弧组装（含“只为会画的弧归一”）、dash 样式；`globe-style.test.ts`
> 覆盖地球样式偏好的默认值/脏数据回退/持久化；`npm run test` 与 `npm run build` 均通过。
> **浏览器实测（真实代理 + 实际流量）尚未执行**，见阶段 4。

## 8. 分阶段计划

### 阶段 0：调研与定稿（本文档）✅

- 产出：本 LLD + 决策记录。
- 验收：决策已由用户确认；globe.gl/DoH/sqlite/tests 集成点全部核对完毕。

### 阶段 1：流量维度与存储（后端基座）✅

- 任务：per-pair 计数/速率/清理（现属 `TrafficStatsUseCase`）；探测缓存表与 store 方法；探测端口；config `[globe]` 解析与校验。
- 验收：新增单测全绿；既有 `pytest` 全绿（per-proxy 流量/队列行为无回归）。✅

### 阶段 2：探测链路与 API ✅

- 任务：`AsyncHttpGetter.get_json` 抽取；`DohResolver`；目标探测用例；`/api/globe` + `globe_enabled`；e2e mock DoH。
- 验收：单测 + e2e 绿。✅

### 阶段 3：前端地球 ✅（浏览器实测待阶段 4）

- 任务：安装 globe.gl；`globeData.ts` 纯函数 + 单测；`GlobePanel.vue`（懒加载/可见性暂停/RO/降级）；types + client；DashboardPage 挂载；`npm run build`。
- 验收：`npm run test`/`npm run build` 通过 ✅；主 chunk 不含 three ✅（§4.4 实测体积）；浏览器实测点、两层弧、动画与粗细随流量变化、切页与后台暂停 ⏳。

### 阶段 4：探测解耦重构 + 验收与文档 ✅

- 任务：把探测从地球后端重构为流量统计的「DNS 探测队列」（事件驱动、延后写、改名/表名，见 `LLD-DnsProbe.md`）；真实环境验收（真实代理 + 实际流量 + 手填坐标）；文档同步（含本文与 `LLD-DnsProbe.md`、测试树）。
- 验收：文档检查单全勾 ✅；全量 `pytest` + 前端 test/build 通过 ✅；真实链路观察到目标弧 ⏳（需人工，见 §10 头号风险）。
- 验证：`python -m pytest -v`、`cd frontend && npm run test && npm run build`。

未去做的可选顶：Playwright 地球卡片断言（§7.2 标注为可选；当前本地约定不跑浏览器 e2e）。

## 9. 文档与产物同步清单

- [x] `README.md`（功能段落 + 依赖/配置说明）
- [x] `docs/architecture.md`（「3D 地球」只读 + 「DNS 探测队列」一节）
- [x] `docs/cli.md`（serve/配置说明同步）
- [x] `docs/filestree.md`（`use_cases/dns_probe.py`、`framework/web/globe_data.py`、前端 globe 组件等）
- [x] `src/GUIDE.md`（测试树补新测试文件）
- [x] `docs/design/LLD-Globe3D.md`（本文档）
- [x] `docs/design/LLD-DnsProbe.md`（探测队列设计）
- [x] `frontend/` 构建产物（`npm run build` 后 `static/` 更新；不入库）

## 10. 风险与待实测项

| 风险 | 影响 | 缓解/验证 |
|---|---|---|
| 免费代理 DoH 成功率低（死代理/被墙端点） | 目标弧出现慢或缺 | 三端点回退（含国内 alidns）；失败退避；不影响其余功能（探测侧细节见 `LLD-DnsProbe.md`） |
| ~~`world.json` polygons 渲染性能~~（已去除 polygons 图层） | — | 已消除（D5/§4.5）：球面改贴图后不再拉 ~1MB 地图、无 1083 个 polygon 的三角化开销；残留风险仅贴图加载失败（断面不渲染，点位/弧仍在） |
| three 与 Vite/vue-tsc 构建兼容 | 构建失败 | 阶段 3 第一步先 `npm install + build` 空跑验证 |
| Playwright headless WebGL 不可用 | e2e 断言方式 | e2e 只断言卡片/canvas 或降级文案存在，不依赖渲染结果 |
| 目标域名落盘隐私 | 本机浏览目标暴露给能读 `.cache.db` 的进程 | TTL + 30 天清理；面板监听址可在 `[web].host` 收紧（默认 `0.0.0.0`）；关闭与清理方式见 `LLD-DnsProbe.md`（`[dns_probe].enabled=false` 停探测；清 `dns_probe_cache` 表） |
| 真实代理的 DoH 成功率与渲染观感 | 目标弧数量/可读性 | 待人工验收；失败退避保证不拖慢其余功能 |
| 大量新 pair 突发 | 探测排队 | 队列积压由"消费前取信号量"形成背压，不无界并发（见 `LLD-DnsProbe.md`） |