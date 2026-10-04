# CLI 命令文档

根目录 `cli.py` 是薄入口（引导 sys.path 后委托 `src.framework.cli.main()`）。

## serve — 本地代理服务器

```bash
python cli.py serve [--port 8082] [--host 127.0.0.1] [--queue-size 10]
                   [--interval 2] [--probe-url URL] [--type http|https|socks5]
                   [--web-host 127.0.0.1] [--web-port 8083]
```

### 参数说明

- `--port`：监听端口（默认 8082）
- `--host`：监听地址（默认 127.0.0.1）
- 监听**协议**不在此处指定：取配置 `[server].listen_protocols`（入站协议，默认 `http+socks4+socks5` 单端口）；与 `--type`（出站上游类型过滤）是两套词汇
- `--queue-size`：代理服务队列容量（默认配置中的 STANDBY_QUEUE_CAPACITY）
- `--interval`：心跳间隔秒（默认配置中的 HEARTBEAT_INTERVAL）
- `--probe-url`：心跳探测目标 URL（默认配置中的 HEARTBEAT_PROBE_URL）
- `--type`：允许进入代理服务队列的**上游代理类型**，可多次指定（不填默认取配置 `[server].proxy_types`）；不影响本地监听协议（后者由 `[server].listen_protocols` 决定）
- `--web-host`：Web 控制面板监听地址（默认 127.0.0.1）
- `--web-port`：Web 控制面板监听端口（默认 8083）

### 运行机制

- 心跳成功 → 代理保留在代理服务队列（不晋级 trusted）；失败 → 连续失败计数 +1 进入退避补测（独立复核，成功清零恢复），达到阈值（`MAX_CONSECUTIVE_FAILS`/`TRUSTED_MAX_CONSECUTIVE_FAILS`）拉黑移出；缺口自动探测补入
- **绝不直连**：无可用上游（队列空/全部不可用）时请求被拒绝——HTTP 回 `502`、SOCKS5 回 REP `0x01`、SOCKS4 回 CD `0x5B`，**不会以本机真实 IP 直连目标**（pproxy 原语义是回退直连并返回成功响应，等于静默泄漏真实 IP）；上游不可用↔恢复会在日志里记警告/恢复信息
- **失败代理立即退出调度**：心跳判失败、进入退避补测的队列成员，在上游侧被标记不可用，rc/rr/lc 不再选中它（补测成功即恢复）——不必等它攒够失败次数被拉黑；存活判定以代理池心跳为唯一来源，不额外做 TCP 探测
- **协议能力（TCP only）**：SOCKS4/SOCKS4a、SOCKS5 CONNECT、HTTP/HTTPS 隧道均支持；**UDP 不中继**——SOCKS5 `UDP ASSOCIATE` 回 REP `0x07`（command not supported），客户端据此退回 TCP；`BIND(0x02)` 等未实现 CMD 同样回 `0x07`（不再裸断连）。原因：上游是 HTTP/HTTPS/SOCKS5 免费代理，HTTP(S) 无法承载 UDP，SOCKS5 上游也需 UDP ASSOCIATE 客户端（几乎没有免费代理支持）；唯一"能跑通"的形态是 UDP 直连=泄漏真实 IP，故不做
- **上游协议**：与池支持集合一致（`http`/`https`/`socks5`，见 `SUPPORTED_SCHEMES`）；**socks4 上游已移除**——入选集合与入池校验同源，面板添加 / autoload / `blacklist`/`trust` 传入 socks4 都会被拒（响应回传 `rejected` 并记日志）。原因：免费源不产出 socks4，保留只会形成"能被选中却无供给"的半支持状态；pproxy 与 `aiohttp_socks` 的 socks4 机制能力（含入站 `socks4://` 监听）不受影响
- 代理服务队列按响应时间（latency）升序维护，队列满时新代理更快则踢队尾 move trusted、较慢则自身 move trusted（探测成果不浪费）
- **入队筛选**：候选入队前按 `[[server.enqueue_rules]]` 的 `priority` 升序逐条判定（延时/国家/纯净度/测速，默认全部不启用），未通过即短路后续规则并移入 `enqueue-rejected` 组（`ttl="never"`、不可请求，不再参与填充）；测量惰性（前序不过则后续指标不测）、未测到值一律放过；启停与阈值可在设置页调整（优先级只读，改配置重启生效）
- **本地链路门控**：fill/心跳/补测在探测前和探测后各直连多个探测目标（`network_probe_urls`，任一返回状态码 <400 即正常）判定链路可用性——**两次都必须正常才允许执行正常拉黑操作**：探测前不可用（WiFi 断/ISP 故障）跳过本轮操作（不计数/不拉黑/不补测），探测后复检不可用则本轮失败不归因到代理（探测期间链路劣化），队列保持现状、恢复后自动续跑；结论缓存单向——只缓存"不可用"（`network_unhealthy_cache_ttl` 秒内复用、不再探测），"可用"每次真实探测（心跳 2s 一轮，链路抖动 10~20 秒即可让失败代理攒满拉黑阈值，陈旧的"可用"会授权误拉黑；失败结论只会让本轮跳过，复用只花时间）

### Web 控制面板

`--web-host`/`--web-port`：启用 Web 控制面板（默认 `127.0.0.1:8083`，仅本机，无鉴权）。浏览器打开面板地址进行监控与控制：

#### 监控功能

- **总览**：各代理组存量/活跃数/TTL、可用代理数、代理服务队列（latency 排序）
- **组明细**：选择任一组查看每个代理的连续失败数、响应时间、过期倒计时（支持关键字过滤、列排序）；**虚拟滚动**（@tanstack/vue-virtual，动态测高 + overscan）——只渲染视口附近行，上下 spacer 保持滚动条比例，拖滚动条跳到远处自动定位；右下角悬浮回到顶部按钮
- **Windows 资源管理器式框选**：代理组页与代理服务队列页的代理列表支持鼠标框选/多选（锁定单一上游期间队列不参与框选/右键批量/全选——含键盘 Ctrl+A，仅锁定行可操作）——空白处、表头或行上按住拖拽出现蓝色矩形框选，矩形覆盖的行被选中（从表头向下拖拽也可框选）；单击行单选、Ctrl+单击切换、Shift+单击锚点范围选择、Ctrl+拖拽切换框内行、Ctrl+A 全选当前列表、Esc 清空、点击列表外部空白清空选中、拖到视口边缘自动滚动（方向随鼠标所在边缘即时切换；框选可跨未渲染区域）；选中态行高亮（左缘蓝色指示条），列表重绘后选中保持
- **右键批量操作**：选中多行后右键弹出上下文菜单，菜单项作用于**整个选中集**——代理组页：移动（子菜单选目标组，批量）/ 拉黑（批量）/ 删除（批量）/ 还原（黑名单组批量）；代理服务队列页：移出（批量）/ 拉黑（批量）/ 复制（**仅单选**，复制菜单所在行地址）/ 测速（**多选时菜单不展示测速与复制项**，仅单选可用）。右键目标行不在选中集内时先单选该行（对齐资源管理器）
- **代理源**：启用状态、暂停剩余时间；每源「暂停 / 恢复」，未暂停的源另给「拉取」按钮（只拉该源，暂停态下不显示，恢复后立即出现），卡片右上为全量「拉取代理」
- **代理服务队列**：队列内代理（latency 升序，列出延迟/连续失败/纯净度与纯净度·测速·固定·锁定操作）；**锁定单一上游**时锁定代理作为列表首行的「已锁定」行（操作列只剩纯净度/测速/解锁），其余队列行灰显且不接收交互；队列外**待补测代理一览**（心跳失败后进入退避复核、尚未判定结果的代理）仅在此页展示；**固定/解固/锁定/解锁成功后列表立即按新顺序回正**（不等下一次 2s 轮询），其余时间行顺序在选中/拖拽/悬浮期间冻结，避免行从光标下挪走
- **队列逐代理趋势数据**：`/api/standby/series?since=<ts>` 增量下发队列每条的延时与累计流量序列（缺省全量，`cursor` 为下次游标，`window` 为服务端采样窗口秒数供前端裁剪本地缓冲）；延时序列的值为 `null` 表示该轮探测失败，前端据此断线绘制并算成功率
- **行下质量带（单代理图表内联）**：代理服务队列页每个代理占一块——摘要行 + 行下质量带。摘要行列出地址、协议、归属地（`/api/standby/geo`，5 分钟刷新）、入队时长、纯净度 tag、延迟、连续失败与操作按钮；质量带**无逐项显示开关、四块全部常显**：延时趋势与流量速率图（自绘 SVG 行内曲线，**按时间戳映射 x 轴、探测失败处断线**，全部行共用同一时间窗口——新入队行左端留白、采样停止的行不贴右边缘；孤立的单次成功探测仍画出点；图左侧一列标出该图的数据值区间（上=最大/下=最小，与坐标映射同源），迷你图没有坐标轴也能读出量级；**流量速率为 0 的间隔不绘制**（只留真实有速率的段，累计值看"累计 ↑x ↓y"））、纯净度与测速结果，没有数据的块显示"暂无采样/无速率/未检查/未测速"占位（唯一个密度开关是右上角「收起图表」：收起回到单行、跨会话记住）；代理质量由后端心跳自动管理（失败→补测→拉黑），前端不主动做质量标记。纯净度检查源与代理规则在 `config/ippure.toml` 声明（插件式检查源，密钥见 `config/.env`），未启用时不展示检查按钮、质量带显示「纯净度未启用」。**测速/检查状态由后端保存**（`PanelTasks`，经 `/api/status` 每行 `speedtest`/`purity_state` 下发）：刷新页面不丢失"测速中/检查中/结果"；进行中重复触发幂等（不重复起任务、不重复调有日限额的检查源）。
- **单代理测速**：摘要行操作列「测速」按钮（或右键菜单「测速」）调 `/api/proxy/speedtest`（POST `{"bind": "..."}`），后端用 vendor/pspeed 对该代理做 Cloudflare 下载+上传 10s 测速（`asyncio.to_thread` 避免阻塞），返回 `{download: {...}, upload: {...}}`（含 `ok`/`err`）**就地展示在行下质量带**（无需悬浮）；失败腿显示「失败」并挂原因 title，不渲染成 `0.0`。不做后台轮询，30 个代理串行测速约 5 分钟且吃带宽。进行中重复请求后端返回 `{"status":"running"}`（幂等），完成结果写后端状态，刷新页面后仍可见。
- **右键上下文菜单**：代理服务队列页面右键代理行弹出菜单——「复制」（复制该代理地址，剪贴板 API 不可用/非安全上下文时回退 execCommand）、「移出」（hover 展开子菜单，列出所有组及其存量，点击调 `/api/proxy/move` 将代理移到目标组并同步 pproxy 上游；触屏上子菜单点击开合）、「拉黑」（调 `/api/blacklist`，从所有组移除并加入黑名单）、「测速」（立即对该代理触发测速，结果落在行下质量带）。**触屏（手机/平板）**：长按代理行弹同一菜单（触屏禁文本选区，长按不再触发系统选择手柄；iOS 由前端长按计时兜底）；行顺序冻结仅桌面悬浮生效，触屏抬手即解除
- **代理组右键菜单**：代理组页面右键代理行弹出菜单——非黑名单组：「移动」（子菜单选目标组，排除当前组和黑名单）+「拉黑」+「删除」；黑名单组：「还原」（移到代理队列）+「删除」
- **代理归属地**：队列行展示国家·省·市（`/api/standby/geo`，bind → geo，缺失的 IP 首次请求时批量定位并写入 90 天缓存；`geoip.enabled=false` 时该路由不注册、面板不展示该列）
- **3D 地球**（仪表盘底部）：globe.gl 渲染代理节点与「本机 → 代理 → 目标服务器」两段流量飞线（线宽 ∝ 聚合速率、光点流速 ∝ 瞬时速率），数据 5s 轮询 `/api/globe`；目标落点由**穿对应代理的 DoH 解析**得到（CDN 按出口就近调度）。`geoip` 或 `[globe].enabled` 任一关闭时后端不注册该路由、卡片隐藏。**进入视口才加载渲染**（three 链动态 import，主包不含），不可见/页面切后台暂停动画并停轮询——没有人看地球时后端不做探测；主题切换重建配色；WebGL 不可用时卡片显示降级文案。本机坐标在 `config/panel.toml` 的 `[globe]` 手填（`self_lat`/`self_lng`），未配置时不画「本机 → 代理」段。目标域名与归属缓存到 `.cache.db` 的 `target_geo_cache` 表（30 天自动清理）；只想停探测设 `[globe].enabled=false`，要清掉已缓存的目标域名则停 serve 后清该表内容（见 `docs/architecture.md` 隐私边界节）
- **添加代理**：代理组页面「添加」按钮（导出 CSV 左边），弹出模态框输入代理地址（每行一个，裸地址默认 http），调 `POST /api/group/{name}/add` 批量添加到当前组
- **设置页**（侧边栏「设置」，仅运行时可变项）：编辑 6 项运行时调度参数（心跳间隔/并发/超时/探测 URL/允许类型/调度算法，`/api/settings` 即刻生效）；**入队筛选规则卡片**逐指标开关 + 阈值（延时秒数/国家白名单/纯净度上限/测速下限），优先级以「执行顺序」一行只读展示（改顺序需改 `config/serve.toml` 重启）；填充候选倍率与最小增量不提供面板入口，取值见 `config/serve.toml` 的 `standby_fill_probe_multiplier` / `standby_fill_probe_min_extra`

#### 控制功能

写端点均复用现有用例，即时生效并落盘：

- 手动拉取（`POST /api/fetch`）：`{"source": "<源模块路径>"}` 只拉该源，省略 `source` 时拉取全部启用源。单源拉取复用自动拉取同一条用例路径——已暂停的源跳过、去重率过高按默认时长暂停、源 `pause_ttl` 照常生效。**同一时刻只跑一个拉取**：已有拉取在跑返回 `{"status": "running"}` 不重复起任务；进行中状态随 `/api/status` 下发（`fetching_all` 全量 / 每源 `sources[].fetching`），面板**刷新后仍显示「处理中」**；**全部源拉取异常**返回 HTTP 500 `{"error": "拉取失败: 全部代理源拉取失败"}`，面板弹「拉取失败」；`source` 不在配置里或未启用返回 **400**（不再返回 200「拉取完成」而实际一个源都没拉）
- 拉黑 / 加入信任池（`/api/blacklist`、`/api/trust`）：拉黑时从所有组移除并加入黑名单，同步 pproxy 上游
- 移出代理到指定组（`/api/proxy/move`，POST `{"proxies": [...], "target_group"}`，**批量**，proxies 为数组）：将代理从当前组移到目标组，从 standby 队列移出时同步 pproxy 上游
- 删除代理（`/api/proxy/remove`，POST `{"proxies": [...], "group"}`，**批量**，proxies 为数组）：从指定组移除代理（不拉黑），从 standby 队列移除时同步 pproxy 上游
- 添加代理到组（`POST /api/group/{name}/add`，`{"proxies": [...]}`）：批量添加代理到指定组，裸地址自动补 http://，standby 组添加后同步 pproxy 上游
- 源暂停 / 恢复（可带自定义暂停时长，`/api/source/pause|resume`）：源名必须在本配置的代理源列表里，否则 400（不落库成面板永远看不到、也解不掉的幽灵暂停记录）；恢复未处于暂停的源返回 `resumed=false`（幂等，不报错）
- 固定 / 解固代理服务队列代理（`/api/standby/pin|unpin`）：固定代理排队列最前（按固定时间升序），不被踢出/拉黑/补测，超出 `capacity` 的固定代理不进上游但保留固定状态（仍受心跳探测，失败只计数）
- 锁定 / 解锁单一上游（`/api/standby/lock|unlock`）：锁定队列内代理为 pproxy 唯一上游（move 到内建隐藏组），fill 跳过、心跳仍探测锁定代理测速（延迟数据加趋势图，失败只计数不拉黑）、无论心跳是否成功都只用该代理；锁定时仪表盘健康率卡/健康率趋势卡隐藏（队列长度与健康率无意义），延迟趋势图连续显示测速；其他代理保留不动，解锁后恢复原调度。与 pin 区别：pin 只是不被踢出（rc 仍可能选其他），lock 是上游只有这一个
- 调度策略运行时调整（`/api/settings`）：
  - 心跳间隔、并发、超时、探测 URL
  - 填充倍率/增量、允许的代理类型
  - 入队筛选规则（`enqueue_rules`：[{metric, enabled, ...阈值}]，校验复用领域层 `METRIC_SPECS`，非法值 400）
  - 调度算法（`salgorithm`：fa/rr/rc/lc）切换会重启 pproxy 调度器（保留现有上游），其余参数 serve 下一轮即生效

仪表盘的「代理服务队列延迟趋势」「健康率趋势」图表由**后端采样**驱动：每轮 heartbeat 结束采一次队列指标，存内存按 `METRICS_WINDOW`（全局留存时长，与流量共用）保留，超出自动清理；`/api/status/history` 增量下发（`?since=<ts>` 只回新点，并带 `retention_window` 供前端按时间裁剪本地缓冲），图表**按拿到的真实数据点绘制、不做窗口裁剪**——**刷新页面不丢失趋势**（多标签一致），重启清空。`standby_chart_window` 只决定**队列页**行内趋势图显示多长，不影响留存与仪表盘。

「数据流量」图表通过 pproxy 原生 `modstat` 钩子统计上行/下行字节，**独立采样循环固定 2s 采累计值存历史**（与队列指标同按 `METRICS_WINDOW` 留存）——采样与心跳节奏解耦：锁定单一上游时心跳放宽到 30s 但流量照常 2s 采样（锁定代理正被使用，流量图不跟着变稀疏），前端算速率曲线；旁边 KPI 显示实时上下行速率、累计流量、当前平均延迟。按上游代理的累计/历史流量（`per_proxy_traffic`，`{bind: {"upload": int, "download": int}}`）通过 vendor/pproxy 内化的 `modstat(user, remote_ip, host_name, roption)` 钩子实现，仅内存实时计数不持久化。历史数据统一用 bind（地址，不含 scheme）作 key：面板走 `/api/standby/series` 批量增量下发（见上），单代理的 `/api/proxy/history?bind=` 保留作诊断查询（延时序列的值为 `null` 表示该轮探测失败）。

面板为 Vue 3 + Vite + TypeScript + Tailwind CSS 4 单页应用（源码 `frontend/`，构建产物输出到 `src/framework/web/static/`、由 aiohttp 直接 serve；**产物不入库**，克隆后需在 `frontend/` 执行 `npm install && npm run build`；图表统一 ECharts）。定时轮询 `/api/status`（固定 2s），数据全部通过 REST API 交互。

## fetch — 拉取代理

```bash
python cli.py fetch [--json]
```

### 参数说明

- `--json`：以 JSON 格式输出拉取结果

### 功能说明

从启用的代理源拉取代理，按组写入，并检查源去重率决定是否暂停。

入池前先过 `config/sources.toml` 的 `[[fetch_filter]]` 声明式筛选规则——命中即拉黑（进黑名单组，ttl 内不再入池）：

```toml
[[fetch_filter]]
pattern = ":80$"
reason = "端口 80 为 Cloudflare 假代理（CONNECT 400）"
```

对完整代理 URL（含 scheme）做 `re.search`，IP / 端口 / scheme 均由正则自行表达；多条规则按书写顺序取首个命中，`match` 可限定生效的源（glob，省略 = 全部源）。

## list / ls — 列出可用代理

```bash
python cli.py list [--group GROUP] [--json]
```

### 参数说明

- `--group`：指定组名，仅列出该组的代理
- `--json`：以 JSON 格式输出

### 功能说明

列出当前可用的代理，默认从所有可请求组中列出。

## get — 随机获取代理

```bash
python cli.py get [-n N] [--json]
```

### 参数说明

- `-n N`：获取 N 个代理（默认 1）
- `--json`：以 JSON 格式输出

### 功能说明

随机获取代理（trusted 优先，池空自动拉取）。按配置的填充组优先级从可用集合收集代理。

## status — 查看代理池状态

```bash
python cli.py status
```

### 功能说明

查看代理池状态，包括各组数量、TTL、暂停源等信息。

## blacklist / trust

```bash
python cli.py blacklist 1.2.3.4:8080 5.6.7.8:3128
python cli.py trust 1.2.3.4:8080
```

### 功能说明

- `blacklist`：将指定代理加入黑名单
- `trust`：将指定代理加入信任池

## request — 用代理访问 URL

```bash
python cli.py request URL [--timeout 10] [--retries 5] [--max-length N] [--headers]
```

### 参数说明

- `URL`：目标 URL
- `--timeout`：请求超时秒数（默认 10）
- `--retries`：最大重试次数（默认 5）
- `--max-length N`：响应体最大长度
- `--headers`：显示响应头

### 功能说明

用代理访问 URL，自动重试换代理。成功代理自动晋级 trusted（连续失败计数清零），连续失败过多的自动拉黑（代理服务队列成员复制进 trusted 并清零队列副本连续失败计数、保留在队列）。

## test — 测试代理

```bash
# 单代理模式
python cli.py test 1.2.3.4:8080 URL

# 并发模式
python cli.py test https://httpbin.org/ip -c 20
```

### 参数说明

- `proxy`：指定代理地址（单代理模式）
- `URL`：测试目标 URL
- `-c N`：并发测试 N 个代理（并发模式）

### 功能说明

- 单代理模式：走 `http_client.get`，不校验证书
- 并发模式：从池中取 N 个代理并发测试，表格化输出（按 成功优先/耗时 排序）

## 日志

`--debug` 输出 DEBUG 级；默认 `setup_logging()`：统一格式（毫秒时间戳）输出到 **stderr + logs/proxy-pool-{YYYYMMDD-HHMMSS-mmm}.log**（毫秒精度天然唯一不覆盖）；`urllib3`/`requests` 日志降为 WARNING。

### 日志文件位置

日志文件位于 `logs/` 目录，文件名格式：`proxy-pool-{YYYYMMDD-HHMMSS-mmm}.log`

### 日志级别

- 默认：INFO 级别
- `--debug`：DEBUG 级别
- `urllib3`/`requests`：WARNING 级别（避免过于冗余）

## 测试

```bash
python -m pytest -v
```

### 测试覆盖

- 单测：领域规则（TTL/晋级/淘汰/暂停）、用例（拉取三阶段、重试、晋级）、接口（存储/调度器/UA/代理服务器/各源解析）、CLI（fake container 注入）
- e2e：`tests/e2e/` 用本地 mock 上游（模拟 HTTP CONNECT 隧道 + SOCKS5 握手）验证 serve 全链路：fill 补入 → pproxy 启动 → 请求经上游转发 → 心跳移出死代理 → 动态 sync 增删 → 无缝切换 → Ctrl+C 优雅退出，目标地区探测链路（mock DoH → `/api/globe` 出弧、未激活不探测），全程不依赖外网
- 前端：`frontend/` 的 Vitest（`cd frontend && npm run test`）覆盖工具函数与 3D 地球数据映射等纯逻辑；`cd frontend && npm run build`（vue-tsc + Vite）必须通过；组件行为由 `tests/e2e/web/` 的 Playwright 覆盖
