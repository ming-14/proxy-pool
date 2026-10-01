# 设计方案：通用化入队筛选规则（优先级 + 惰性测量）

## 1. 背景

### 1.1 fill 现在只有一道隐式闸门

`StandbyQueueUseCase.fill()`（`src/use_cases/standby.py`）从候选组收集一批代理，并发探测，
**凡探测成功（HTTP 2xx）即 move 入代理服务队列**，`elapsed` 仅被记录为 latency 用于排序与
队列满时踢尾。也就是说当前所谓「延时达标」等于「活着」——**不存在任何数值阈值闸门**。

### 1.2 四个指标的现状差异（核心约束）

| 指标 | 测量方式 | 现在住在哪 | 相对成本 | 能否被 fill 直接调用 |
|---|---|---|---|---|
| 延时 | fill 探测本身的 `elapsed` | `src/use_cases/standby.py` | 免费（已有） | 天然可用 |
| 国家 | mmdb 定位 + SQLite 缓存 | `IpGeoUseCase`；三步线程编排在 `api.py::_resolve_geo` | 低（本地读） | 编排在 web 层 |
| 纯净度 | 外部 API + 缓存 | `CheckPurityUseCase`；编排内联在 `api.py::_do_purity` | 高（外网 + 日限额） | 编排在 web 层 |
| 测速 | pspeed 打 Cloudflare（~10s，100MB 下载） | **框架层** `api.py::_run_speedtest`，结果存框架层 `PanelTasks` | 极高 | 完全不可复用 |

两处硬约束：

- `src/framework/container.py` 中 purity 与 geo **仅在 `WEB_ENABLED` 时构造**（注释：仅 Web 面板消费）。
  入队筛选属 serve 核心路径，该前提必须解除。
- 测速实现留在框架层 web api，且与面板 UI 任务状态（`PanelTasks`）耦合，用例层拿不到。

### 1.3 探测失败候选的现状处理（新规则的语义基准）

fill 一轮内先收集失败候选，**整轮结束后**统一处理：

1. 链路门控复检（`_gate.check()`）：不通过 → 本轮失败**不计分**（防链路抖动误杀）；
2. 通过 → 逐个 `record_fail(proxy, blacklist=True)`：
   - `consecutive_fails += 1`；
   - 达阈值（普通组 `max_consecutive_fails=5`、trusted `10`）→ `remove()`：清掉所有组副本 +
     加入黑名单组（`ttl=10800`，3 小时后过期可重新入池）；
   - 未达阈值 → 留在原组、计数累加。
3. **没有冷却期概念**：`get_available_proxies()` 只排除 `consecutive_fails >= 阈值` 的代理，
   因此未达阈值的失败候选下一轮 fill 会被再次选中再探。fill 每轮在 heartbeat 前后各跑一次，
   ≈3 轮就攒满 5 次失败被拉黑。

即既有语义是「**计数 → 达阈值淘汰**」，不是「冷却」。新规则不复用这套计数，改用一次性移出
到专用组（§3.8）——规则拒绝是确定性判定，不需要多轮观察。

## 2. 目标

- 把 fill 的单一隐式闸门泛化为**可配置、可扩展、有优先级、惰性求值**的规则链；
- 首期支持：延时、国家、纯净度、测速四个指标，留出新增指标的扩展点；
- 规则与优先级写在配置文件；网页设置提供开关与阈值（**不提供优先级**）；
- 日志完整输出筛选工作流（每一步通过/不通过、被跳过的规则）。

### 2.1 顺序语义（已确认）

每条规则带 `priority`，**数字小的先执行**（升序），未通过则**短路**后续规则。默认：

| 指标 | priority | 为什么这个位置 |
|---|---|---|
| 延时 | 1 | 测量零额外成本（复用 fill 探测），最便宜，放最前 |
| 国家 | 2 | 本地 mmdb + 缓存，便宜 |
| 纯净度 | 3 | 外网 API + 日限额，较贵 |
| 测速 | 4 | ~10s + 大流量，最贵，必须放到最后（前三条先过滤掉绝大多数） |

示例：同时启用延时(1) 与测速(4) 时，先测延时；延时通过才测速，延时不过则测速**完全不执行**。

## 3. 方案

### 3.1 规则模型与顺序（`src/domain/enqueue_filter.py`，新增）

沿用 `src/domain/source_filter.py`（`FilterRule` + `match_rule`）与 `src/domain/purity.py`
（`ProxyRule` + `resolve_proxy`）确立的「声明式规则 + 有序求值」惯例。领域层零 IO、无第三方依赖。

```python
@dataclass(frozen=True)
class EnqueueRule:
    metric: str                     # latency | country | purity | speed
    priority: int                   # 升序执行
    params: Mapping[str, object]    # max_latency / allow / max_score / min_download_mbps
    enabled: bool = False           # 默认不启用，网页设置可运行时开启

    @property
    def label(self) -> str:         # 展示名取自 METRIC_SPECS，不随规则落配置
        ...


@dataclass(frozen=True)
class Verdict:
    passed: bool
    detail: str                     # "0.42s ≤ 1.0s" / "US ∉ {US,JP}" / "未测到，放过"
    measured: bool = True           # False = 没取到值，按放过处理


@dataclass(frozen=True)
class MetricSpec:
    label: str                      # 中文展示名（日志/面板）
    required: frozenset[str]        # 必填参数键
    optional: frozenset[str]
    coerce: Callable[[Mapping], dict]                    # 参数校验 + 归一（未知/缺键报错）
    judge: Callable[[Mapping, object], Verdict]          # 纯判定


METRIC_SPECS: dict[str, MetricSpec] = { ... }             # 扩展点：新增指标只加一条


def order_rules(rules: Iterable[EnqueueRule]) -> tuple[EnqueueRule, ...]:
    """按 priority 升序（稳定排序，priority 唯一性在配置层强制）"""
```

**扩展方式**：新增一个指标 = `METRIC_SPECS` 加一条（参数校验 coerce + 纯判定 judge）+ 在组合根
注册该指标的测量源（providers，见 3.2）；若该指标来自外部系统，再在 `interfaces/` 加一个实现（见重构 A）。
参数校验与判定都在领域层，配置层不再为每个指标写一份解析分支。

### 3.2 惰性测量上下文（`src/use_cases/enqueue_filter.py`）

「数字小的先测、未通过就跳过后续」本质是**惰性求值**：只有当规则链走到某指标时才去测量它。
不预先算四个指标，而是给规则链一个按需取值的上下文，内部记忆化（同一代理同一指标只测一次）：

```python
# 测量源：proxy -> 测量值（None = 测不到，判定时按放过处理）
MetricProvider = Callable[[str], Awaitable[object | None]]


class EnqueueReadings:
    """按需测量 + 记忆化；未启用/不可用时返回 None

    latency 直接取 fill 探测结果（不占 provider），其余指标按注册表懒调 provider。
    同一候选同一指标只测一次，未走到的指标零成本。
    """
```

**缺数据策略：放过（fail-open）**，并在 `Verdict.detail` 与日志中标注「未测到值(放过)」——
geoip 关闭 / 纯净度未启用 / 测速不可用时，不应因"我方无数据"而拒绝一个可用代理。

### 3.3 用例层 `EnqueueFilterUseCase`（新增）

```python
class EnqueueFilterUseCase:
    def __init__(self, *, rules, providers: Mapping[str, MetricProvider] | None = None): ...

    async def evaluate(self, proxy: str, *, latency: float | None) -> FilterOutcome:
        """按 order_rules 顺序逐条判定，未通过即短路；本方法不外抛（异常放行）"""

    def update_rules(self, rules) -> None:
        """替换规则集合（已按 priority 升序）；无测量源的启用规则告警并始终放过"""


@dataclass(frozen=True)
class FilterOutcome:
    passed: bool
    rejected_by: EnqueueRule | None
    trace: tuple[tuple[EnqueueRule, Verdict], ...]   # 实际执行路径（被短路的规则不在其中）

    def workflow(self) -> str:      # 供日志输出完整工作流
        ...
```

规则评估**不写进 `standby.py`**（它已 560 行，含 fill/heartbeat/retry/lock/metrics/series）。

### 3.4 与 fill 接入：回调链需支持协程

`ProxyScheduler.run` 现在**同步**调用 `on_success(proxy, task_data, result)`
（`src/interfaces/scheduler.py`），`CheckProxiesUseCase._on_success` 同样同步转发给 `on_result`。
筛选是 async（懒测量要 await），故需让这两处回调支持协程：

```python
ret = on_success(proxy, task_data, result)
if inspect.isawaitable(ret):
    await ret
```

这是**契约放宽**（同时支持同步/异步回调），不是兼容垫片：`freeproxy_world` 的同步 `on_page`
保持不变。改动约 4 行，两处。

接入点在 `fill()` 的 `on_result` 成功分支：先 `await self._filter.evaluate(...)`，通过才走
既有的入队/踢尾逻辑；未通过则按 `on_reject` 处理（见 3.8），不入队、不 sync 上游。

### 3.5 配置格式（`config/serve.toml`）

`serve.toml` 的 `[server]` 是已登记的顶层节，`[[server.enqueue_rules]]` 嵌套数组表**无需改动
`_SECTION_OWNERSHIP`**。注意 TOML 规则：数组表之后不能再写 `[server]` 的标量键，
故这批规则块统一放在文件**末尾**。

`[server]` 另加一个 `enqueue_reject_group`（拒绝候选的归属组名，见 §3.8）。

```toml
# 入队筛选规则：按 priority 升序执行（小号先测），未通过即短路。默认全部不启用。
# 数组表须置于本文件末尾（其后不能再写 [server] 的标量键）。
[[server.enqueue_rules]]
metric = "latency"
priority = 1
max_latency = 1.0                 # 秒
enabled = false

[[server.enqueue_rules]]
metric = "country"
priority = 2
allow = ["US", "JP", "SG"]       # 空 = 不限制
enabled = false

[[server.enqueue_rules]]
metric = "purity"
priority = 3
max_score = 50                    # 0-100，越高越脏
enabled = false

[[server.enqueue_rules]]
metric = "speed"
priority = 4
min_download_mbps = 10
enabled = false
```

校验（照抄 `_parse_filter_rule` 的口径，错误在启动时暴露，防"拼错静默失效"）：

| 校验 | 提示 |
|---|---|
| `metric` 不在 `METRIC_SPECS` | 未知指标 + 可选值列表 |
| 未知键 | 列出该 metric 允许的键 |
| 缺必填参数 | 指明缺哪个 |
| `priority` 非正整数 / 重复 | 顺序不确定，直接报错 |
| `enqueue_reject_group` 未在 `[groups]` 定义 | 报错 |
| `enqueue_reject_group` 出现在 `requestable_groups` | 报错（拒绝候领会立即重新成为候选，规则形同虚设） |

导出 `config.ENQUEUE_RULES: tuple[EnqueueRule, ...]`，由组合根注入。

### 3.6 运行时与网页设置

优先级来自配置、只读；运行时只可启停与改阈值（与现有「运行时可变项无落盘语义」一致）。

- 新增 `EnqueueRuleSettings`（`src/framework/web/control.py`，与 `ServeControl` 同层同文件），
  持有 `enabled: set[str]` + 阈值覆盖，`to_dict()` 输出 `{metric, label, priority, enabled, params}`；
- `ServeControl.to_dict()` 带上它 → `/api/status.settings.enqueue_rules`；
- `/api/settings` 的 patch 增加 `enqueue_rules` 分支（校验后调 `standby.update_enqueue_rules()`），
  与既有 `fill_probe_*` 的落地方式完全一致；
- 前端新增 `EnqueueRulesCard.vue`（与 `ScheduleSettingsCard.vue` 同款脏检测 + 保存），
  挂在 `SettingsPage.vue`：每指标一个开关（对应 `enabled`）+ 阈值输入，**优先级以一行的只读执行顺序展示**
  （如「执行顺序：延时 → 国家 → 纯净度 → 测速」）；默认四项全关。

### 3.7 日志（工作流）

- **每候选 DEBUG**（fill 每 2s 一轮、候选可达 `缺口×2`，逐条 INFO 会刷屏）：

  ```
  筛选 1.2.3.4:8080 → 延时[1] 0.42s ≤ 1.0s 通过 → 国家[2] US ∈ {US,JP} 通过
                   → 纯净度[3] score 88 > 50 不通过，跳过后续（测速[4] 未执行）
  ```

- **每轮 INFO 汇总**：`入队筛选: 候选 12, 延时淘汰 5, 国家淘汰 2, 纯净度淘汰 1, 测速淘汰 0, 通过 4`。
- 规则集合变更（网页改设置）记一条 INFO：`入队筛选规则更新: 延时(1) 开, 国家(2) 关, ...`。

### 3.8 拒绝候选的处理：移入专用组

候选被规则拒绝后**不留在原组**（否则下一轮 fill 又会选中它、反复测量），而是移入配置指定的
`enqueue_reject_group`——一个 `ttl="never"` 的普通组。

- 机制：`remove_all_copies(proxy, reject_group)`（清掉所有组副本后入组，与拉黑同一套做法），
  顺带记录 `source_group` 便于面板展示"从哪个组被拒"；
- 该组**不进 `requestable_groups`**，故不会被 `get_available_proxies()` 选中，也不会被 fill 采集；
- 拉取侧也天然不会复活：`fetch.apply()` 用 `find_proxy()` 去重，代理在 reject 组内即视为已存在；
- 与黑名单的区别：`ttl="never"` 永久留档、不自动过期，语义是"已判定不合格、留证"而非"暂时冷却"；
  需要重新放回池子时可从面板手动 move 到其它组。

`pool.toml` 新增组：`"enqueue-rejected" = { ttl = "never" }`。面板需给它一个标签
（`_GROUP_META` 加 `kind="rejected"`，前端 `GroupStat.kind` 联合类型补一项）。

### 3.9 测速指标的成本控制（必须）

测速是 10s + 100MB 级操作，**在入队筛选里必须带缓存与低并发**，否则每轮 fill 重测会打爆带宽：

- 结果缓存（TTL 1h，常量在接口实现内），避免同一候选每轮重测；测速为阈值判定，
  缓存过期自然触发重测；
- 候选内串行/低并发（并发 1），仅在通过前序规则后触发；
- 面板手动测速与筛选用**同一个测量实现**（重构 A），但不共享 `PanelTasks` 的 UI 任务状态
  （那是面板展示态，与测量本身无关）。

## 4. 架构重构点

| 编号 | 现状问题 | 重构 |
|---|---|---|
| **A** | 测速实现（pspeed/Cloudflare）住在框架层 `api.py`，且与 `PanelTasks` 耦合，用例层不可用 | 新增 `ProxySpeedTester` 端口（`src/domain/ports.py`）+ `src/interfaces/speedtest.py` 实现；web 端点与筛选共用 |
| **B** | 「query_cache → executor → save_cache」三步 SQLite 线程编排在 `api.py` 写了两遍（`_resolve_geo`、`_do_purity`） | 收敛为用例上的单一 `async resolve(...)` 入口（含既有降级语义），web 与筛选共用 |
| **C** | purity/geo 仅在 `WEB_ENABLED` 时构造 | 与 web 解耦：serve 需要（有对应规则）时也构造；保留"不需要就不 import"的懒加载初衷 |
| **D** | `standby.py` 已 560 行 | 规则评估独立为 `EnqueueFilterUseCase`，不并入 standby |
| **E** | 回调链只支持同步 `on_success`/`on_result` | 支持协程回调（§3.4），否则流式入队无法接入 async 规则 |
| **F** | 拒绝候选无处可去（§1.3） | 新增 `enqueue-rejected` 组（`ttl="never"`、不可请求）+ 域方法复用 `remove_all_copies`；面板标签与 `kind` 联合类型补项 |

## 5. 涉及文件

| 文件 | 改动 |
|---|---|
| `src/domain/enqueue_filter.py` | 新增：`EnqueueRule` / `Verdict` / `MetricSpec` / `METRIC_SPECS` / `order_rules` |
| `src/domain/ports.py` | 新增 `ProxySpeedTester` 端口 |
| `src/use_cases/enqueue_filter.py` | 新增：`EnqueueFilterUseCase` + `EnqueueReadings`（惰性测量） |
| `src/use_cases/ip_geo.py` / `check_purity.py` | 重构 B：新增 `async resolve(...)` 三步编排入口 |
| `src/interfaces/speedtest.py` | 重构 A：pspeed 测速实现（从 `api.py` 下沉）+ 结果缓存 |
| `src/interfaces/scheduler.py` / `check_proxies.py` | 重构 E：支持协程回调 |
| `src/use_cases/standby.py` | `fill()` 成功分支接入筛选；`update_enqueue_rules()` |
| `src/framework/config.py` | `_parse_enqueue_rule` + `ENQUEUE_RULES` + `ENQUEUE_REJECT_GROUP`（含两项组校验） |
| `config/serve.toml` | 新增 `[[server.enqueue_rules]]` 四条（默认 `enabled=false`）+ `enqueue_reject_group` |
| `config/pool.toml` | 新增 `"enqueue-rejected" = { ttl = "never" }` 组 |
| `src/framework/container.py` | 装配筛选用例；purity/geo 构造条件与 web 解耦（重构 C） |
| `src/framework/web/control.py` / `api.py` | 重构 B：调用用例入口；运行时规则设置 + `/api/settings` + `/api/status` |
| `frontend/src/components/EnqueueRulesCard.vue` / `pages/SettingsPage.vue` / `types/api.ts` | 设置选项卡 |
| `docs/architecture.md` / `docs/cli.md` | 同步 |
| `tests/**` | 见 §6 |

## 6. 测试计划

| 层级 | 用例 |
|---|---|
| domain | `order_rules` 升序 + 稳定；优先级重复在配置层报错 |
| domain | 各 metric 的 `judge` 边界（阈值相等、缺值放过、空 allow = 不限制） |
| config | 未知 metric / 未知键 / 缺必填 / priority 非法或重复 / on_reject 非法 → 报错 |
| use_cases | **惰性核心保证**：首条规则未通过时，后续指标的 provider **一次都没被调用**（用 mock 断言调用次数） |
| use_cases | 短路顺序与 `priority` 一致；拒绝候选被移出原组并入 reject 组，下一轮 fill 不再被采集 |
| use_cases | `enabled=false` 的规则完全不执行（provider 一次都没被调用） |
| use_cases | 缺数据 fail-open（geo 关闭 / purity 未启用时规则放过并标注） |
| interfaces | `ProxySpeedTester` 实现单测（mock pspeed）：成功/失败/超时 |
| interfaces | scheduler 协程回调：async `on_success` 被 await；sync `on_success` 行为不变 |
| use_cases/e2e | fill 端到端：被规则拒绝的候选不入队、不上游；通过的正常入队 |
| e2e(web) | 设置页开关/阈值往返 `/api/settings`；优先级只读展示；产物已构建 |
| 回归 | 无规则配置时行为与现状**完全等价**（存活即入队、失败计数语义不变） |

## 7. 分期

| 期 | 内容 | 依赖 | 现状 |
|---|---|---|---|
| 1 | 领域规则引擎 + 配置解析 + 日志汇总/明细 + **延时**指标 | 无（延时测量零依赖） | 完成（含配置/集成测试） |
| 2 | 重构 B/C（geo 编排下沉 + 构造解耦）+ **国家**指标 | 期 1 | 完成 |
| 3 | 重构 A + 测速缓存 + **测速**指标 | 期 1 | 完成（缓存常量在接口实现内，规则不再带 `cache_ttl`） |
| 4 | **纯净度**指标（复用期 2 的编排模式） | 期 2 | 完成 |
| 5 | 网页设置选项卡 + 运行时启停 + 文档同步 + 前端构建 | 期 1 | 完成（面板 e2e 新增用例待跑） |

每期结束跑：后端全量 pytest、前端 typecheck+test、`npm run build`（凡动前端）、面板 e2e。

## 8. 取舍

| 项 | 说明 |
|---|---|
| 优先级只读 | 网页设置不提供优先级编辑（用户要求）；改顺序需改配置重启 |
| 缺数据放过 | "我方查不到"不等于"代理不行"，宁可放行并标注，也不误杀 |
| 测速默认放最后 | 10s + 100MB 级成本，必须靠前序规则先削减候选；且必须缓存 |
| 拒绝即移出 | 不新增计数/冷却设施：一次性移入 `ttl="never"` 的 reject 组，彻底离开候选池 |
| 拒绝组永久保留 | `ttl="never"` 不自动回收，需人工从面板放回；换来的是"不重复测量"与"可追溯"
| 不预计算指标 | 惰性求值，未走到的指标零成本；代价是同一候选的测量分散在多轮（靠缓存兜住） |
| 规则评估不并入 standby | 避免 `standby.py` 继续膨胀；筛选是可独立测试的用例 |
| 阈值等待确认项 | `on_reject` 默认值与是否需要 skip 由用户定（§9） |

## 9. 已定决策

1. 拒绝候选 → 移入 `ttl="never"` 的专用组（§3.8）。
2. 四个规则默认**全部不启用**（`enabled = false`），阈值按本文档建议值预置。
3. 日志/注释从简：配置块只留必要说明，代码注释不写冗长解释。
