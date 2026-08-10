# CLI 命令文档

根目录 `cli.py` 是薄入口（引导 sys.path 后委托 `src.framework.cli.main()`）。

## serve — 本地代理服务器

```bash
python cli.py serve [--port 8080] [--host 127.0.0.1] [--queue-size 10]
                   [--interval 2] [--probe-url URL] [--type http|https|socks5]
                   [--web-host 127.0.0.1] [--web-port 8081]
```

### 参数说明

- `--port`：监听端口（默认 8080）
- `--host`：监听地址（默认 127.0.0.1）
- `--queue-size`：代理服务队列容量（默认配置中的 STANDBY_QUEUE_CAPACITY）
- `--interval`：心跳间隔秒（默认配置中的 HEARTBEAT_INTERVAL）
- `--probe-url`：心跳探测目标 URL（默认配置中的 HEARTBEAT_PROBE_URL）
- `--type`：允许的代理类型，可多次指定（默认配置中的 SERVE_PROXY_TYPES）
- `--web-host`：Web 控制面板监听地址（默认 127.0.0.1）
- `--web-port`：Web 控制面板监听端口（默认 8081）

### 运行机制

- 心跳成功 → 代理保留在代理服务队列（不晋级 trusted）；失败 → 连续失败计数 +1 进入退避补测（独立复核，成功清零恢复），达到阈值（`MAX_CONSECUTIVE_FAILS`/`TRUSTED_MAX_CONSECUTIVE_FAILS`）拉黑移出；缺口自动探测补入
- 代理服务队列按响应时间（latency）升序维护，队列满时新代理更快则踢队尾 move trusted、较慢则自身 move trusted（探测成果不浪费）
- **本地网络门控**：fill/心跳/补测前直连多个探测目标（`network_probe_urls`，任一通即正常）判定本地网络可用性。异常时（WiFi 断/ISP 故障）跳过本轮操作（不计数/不拉黑/不补测），队列保持现状，网络恢复后自动恢复调度——避免好代理被误拉黑。正常时缓存复用（`network_check_cache_ok` 秒），异常时退避重检（`network_check_backoff_initial` → ×2 → `network_check_backoff_max`）

### Web 控制面板

`--web-host`/`--web-port`：启用 Web 控制面板（默认 `127.0.0.1:8081`，仅本机，无鉴权）。浏览器打开面板地址进行监控与控制：

#### 监控功能

- **总览**：各代理组存量/活跃数/TTL、可用代理数、代理服务队列（latency 排序）
- **组明细**：选择任一组查看每个代理的连续失败数、响应时间、过期倒计时（支持关键字过滤、列排序）；**双向虚拟滚动**——一次渲染 50 行，滚动到底部自动懒加载下一批，超过 150 行时销毁远离视口的行（top/bottom spacer 保持滚动条高度），拖滚动条跳到远处自动定位到对应行；右下角悬浮回到顶部按钮
- **代理源**：启用状态、暂停剩余时间；代理服务队列待补测代理一览
- **单代理 hover 图表**：代理服务队列页面鼠标悬浮代理 IP（表格行或锁定卡片）时弹出 tooltip，展示该代理的延时趋势 + 流量速率图表（Chart.js 独立实例），数据来自 `/api/proxy/history?bind=`，2s 定时刷新；tooltip 可交互（鼠标可移入查看图表），离开后 100ms 延迟销毁
- **单代理测速**：tooltip 底部「测速 (CF 10s)」按钮，点击后调 `/api/proxy/speedtest`（POST `{"bind": "..."}`），后端用 vendor/pspeed 对该代理做 Cloudflare 下载+上传 10s 测速（`asyncio.to_thread` 避免阻塞），返回 `{download: {best_mbps, ...}, upload: {best_mbps, ...}}` 展示在按钮下方；测速开始后 tooltip **pin**（鼠标移出不自动消失，点击外部才消失），解除 pin 后恢复正常 mouseleave 行为
- **右键上下文菜单**：代理服务队列页面右键代理行弹出菜单——「移出」（hover 展开子菜单，列出所有组及其存量，点击调 `/api/proxy/move` 将代理移到目标组并同步 pproxy 上游）、「拉黑」（调 `/api/blacklist`，从所有组移除并加入黑名单）、「测速」（显示 tooltip 并立即触发测速）
- **代理组右键菜单**：代理组页面右键代理行弹出菜单——非黑名单组：「移动」（子菜单选目标组，排除当前组和黑名单）+「拉黑」+「删除」；黑名单组：「还原」（移到代理队列）+「删除」
- **点击 IP pin tooltip**：代理服务队列页面点击代理 IP 固定悬浮面板（与测速 pin 一致，鼠标移出不消失，点击外部才消失）；已有 pin 面板时悬浮不切换，需先关闭
- **添加代理**：代理组页面「添加」按钮（导出 CSV 左边），弹出模态框输入代理地址（每行一个，裸地址默认 http），调 `POST /api/group/{name}/add` 批量添加到当前组

#### 控制功能

写端点均复用现有用例，即时生效并落盘：

- 手动拉取 `fetch`
- 拉黑 / 加入信任池（`/api/blacklist`、`/api/trust`）：拉黑时从所有组移除并加入黑名单，同步 pproxy 上游
- 移出代理到指定组（`/api/proxy/move`，POST `{"proxy", "target_group"}`）：将代理从当前组移到目标组，从 standby 队列移出时同步 pproxy 上游
- 删除代理（`/api/proxy/remove`，POST `{"proxy", "group"}`）：从指定组移除代理（不拉黑），从 standby 队列移除时同步 pproxy 上游
- 添加代理到组（`POST /api/group/{name}/add`，`{"proxies": [...]}`）：批量添加代理到指定组，裸地址自动补 http://，standby 组添加后同步 pproxy 上游
- 源暂停 / 恢复（可带自定义暂停时长，`/api/source/pause|resume`）
- 固定 / 解固代理服务队列代理（`/api/standby/pin|unpin`）：固定代理排队列最前（按固定时间升序），不被踢出/拉黑/补测，超出 `capacity` 的固定代理忽略但保留固定状态
- 锁定 / 解锁单一上游（`/api/standby/lock|unlock`）：锁定队列内代理为 pproxy 唯一上游（move 到内建隐藏组），fill 跳过、心跳仍探测锁定代理测速（延迟数据加趋势图，失败只计数不拉黑）、无论心跳是否成功都只用该代理；锁定时健康率趋势卡片隐藏、调度参数卡片隐藏、队列统计只显示延迟；其他代理保留不动，解锁后恢复原调度。与 pin 区别：pin 只是不被踢出（rc 仍可能选其他），lock 是上游只有这一个
- 调度策略运行时调整（`/api/settings`）：
  - 心跳间隔、并发、超时、探测 URL
  - 填充倍率/增量、允许的代理类型
  - 调度算法（`salgorithm`：fa/rr/rc/lc）切换会重启 pproxy 调度器（保留现有上游），其余参数 serve 下一轮即生效

仪表盘的「代理服务队列延迟趋势」「健康率趋势」图表由**后端采样**驱动：每轮 heartbeat 结束采一次队列指标，存内存按 `METRICS_WINDOW` 时间窗口保留（超出自动清理），`/api/status` 的 `standby_history` 字段下发完整历史，前端无状态重绘——**刷新页面不丢失趋势**（多标签一致），重启清空。心跳动态调整时窗口始终不变。

「数据流量」图表通过 pproxy 原生 `modstat` 钩子统计上行/下行字节，每轮 heartbeat 采累计值存历史（同窗口），前端算速率曲线；旁边 KPI 显示实时上下行速率、累计流量、当前平均延迟。`/api/status` 另返回 `per_proxy_traffic`（按上游代理地址的累计上下行字节，`{bind: {"upload": int, "download": int}}`），通过 monkey-patch `pproxy.schedule` + `contextvars` 记录选中上游代理实现，仅内存实时计数不持久化。`/api/proxy/history?bind=` 按需查单代理的延时历史（`ProxyLatencySample`）+ 流量历史（`TrafficSample`），两种历史统一用 bind（地址，不含 scheme）作 key。

面板为单页原生 JS，定时轮询 `/api/status`（2s），无前端构建链；数据通过 REST API 交互。

## fetch — 拉取代理

```bash
python cli.py fetch [--json]
```

### 参数说明

- `--json`：以 JSON 格式输出拉取结果

### 功能说明

从启用的代理源拉取代理，按组写入，并检查源去重率决定是否暂停。

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
- e2e：`tests/e2e/` 用本地 mock 上游（模拟 HTTP CONNECT 隧道 + SOCKS5 握手）验证 serve 全链路：fill 补入 → pproxy 启动 → 请求经上游转发 → 心跳移出死代理 → 动态 sync 增删 → 无缝切换 → Ctrl+C 优雅退出，全程不依赖外网
