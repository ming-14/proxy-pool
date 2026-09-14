# proxy-pool 项目文件树

```
proxy-pool/
├── cli.py                         # 命令行薄入口：引导 sys.path 后委托 src.framework.cli.main()
├── view.py                        # .cache.db 只读查看器（tkinter GUI，独立于业务逻辑）
├── AGENTS.md                      # 工程规范（干净架构、测试规范、用户交互规则）
├── requirements.txt               # 项目依赖（requests / PySocks / fake-useragent / aiohttp；pproxy 已 vendor 化）
├── pytest.ini                     # pytest 配置（testpaths=tests）
├── config/                        # 配置文件目录（按领域拆分，节固定归属单文件，见 src/framework/config.py）
│   ├── config.toml                # [paths] 路径 + [web] 面板开关/端口
│   ├── pool.toml                  # [pool] 阈值/语义组/可请求组 + [groups] 分组定义
│   ├── sources.toml               # [fetch] 拉取调度（含 freeproxy_world）+ [[sources]] 源列表
│   ├── serve.toml                 # [server] serve 端口/队列/心跳/网络门控/metrics
│   ├── panel.toml                 # [purity] 纯洁度 + [geoip] IP 归属地图
│   ├── logging.toml               # [logging] 控制台/文件级别分档 + 逐 logger 覆盖
│   └── apikey.env                 # 密钥（ABUSEIPDB_API_KEY 等，git 忽略）
├── src/                           # 主包（干净架构四层）
│   ├── __init__.py
│   ├── GUIDE.md                   # 测试目录结构树（tests/ 全量说明，测试增删时同步）
│   ├── domain/                    # 实体层：纯业务规则，零第三方依赖、无 IO
│   │   ├── __init__.py
│   │   ├── proxy_pool.py          # ProxyPool（分组/TTL/晋级/淘汰/源暂停/序列化）
│   │   ├── models.py              # 值对象：SourceSpec/FpwConfig、normalize_proxy/to_proxy_dict
│   │   └── ports.py               # 抽象端口：ProxySource/ProxyStore/HttpGetter/TaskScheduler
│   ├── use_cases/                 # 用例层：应用业务逻辑，构造注入
│   │   ├── __init__.py
│   │   ├── fetch_proxies.py       # 拉取用例（三阶段：拉取/去重入组/去重率暂停）
│   │   ├── get_proxies.py         # 获取用例（trusted 优先，池空自动拉取）
│   │   ├── record_proxy.py        # 成功/失败记录、拉黑、信任
│   │   ├── request_with_retry.py  # 带重试换代理的请求
│   │   ├── check_proxies.py       # 并发测试代理
│   │   ├── network_gate.py        # 本地网络门控（直连多目标+缓存+退避，异常时跳过心跳/补测/fill 避免误拉黑）
│   │   └── standby.py             # 代理服务队列维护（fill 补入/心跳/退避补测，含本地网络门控 + per-proxy 延时历史）
│   ├── interfaces/                # 接口适配器层：实现 domain 端口
│   │   ├── __init__.py
│   │   ├── sqlite_store.py         # SqliteProxyStore SQLite 存储（.cache.db WAL 模式，组级增量 upsert）
│   │   ├── http_client.py         # HttpGetter（requests，函数级惰性导入，不校验证书 + 多 IP 重试）+ AsyncHttpGetter（aiohttp，心跳/门控高频路径）
│   │   ├── scheduler.py           # ProxyScheduler 异步调度器（asyncio）
│   │   ├── proxy_server.py        # PproxyServer（rserver 动态同步 + rc 调度 + 全局/per-proxy 流量计数 + per-proxy 流量历史）
│   │   ├── ua_generator.py         # 确定性 UA 生成器（基于 fake-useragent）
│   │   └── sources/               # 代理源适配器
│   │       ├── __init__.py
│   │       ├── base.py            # HttpSourceFetcher 基类（URL 构建 + 重试骨架）
│   │       ├── ip89.py            # 89IP 源（纯文本响应，正则提取）
│   │       ├── goodips.py         # Goodips 源（JSON 接口）
│   │       ├── proxy_scdn.py      # ProxyScdn 源（JSON 接口）
│   │       ├── geonode.py         # Geonode 源（JSON 分页接口）
│   │       └── freeproxy_world.py # FreeProxy.World 源（多阶段爬取，异步实现）
│   └── framework/                 # 框架与驱动层：具体技术实现
│       ├── __init__.py
│       ├── config.py              # 配置加载（config/ 领域拆分多文件，固定节归属）
│       ├── container.py           # 组合根（依赖组装；purity/geo 按 WEB_ENABLED 门控构造、重依赖分支内惰性导入）
│       ├── cli.py                 # argparse 命令处理
│       ├── serve.py               # serve 主循环（调度 fill/heartbeat/补测 + 独立流量采样循环 + 上游同步）
│       ├── logging.py             # 日志模块（双通道分档 + 逐 logger 覆盖）
│       ├── logbuffer.py           # 面板日志缓冲（root 镜像 → 有界环形缓冲 + seq 游标增量拉取，供 /api/logs）
│       └── web/                   # Web 控制面板（serve 模式）
│           ├── __init__.py
│           ├── api.py             # REST API（读状态快照/组明细 + 写控制动作/调度策略调整 + /api/proxy/history 单代理历史 + /api/proxy/speedtest 单代理测速 + /api/proxy/move 批量移组 + /api/proxy/remove 批量删除 + /api/group/{name}/add 批量添加 + /api/logs 面板日志增量拉取/清空）+ Vite 产物静态服务
│           ├── control.py         # ServeControl（serve 运行时控制：fill/heartbeat/补测）
│           ├── panel_state.py     # PanelTasks（面板任务状态：测速/纯洁度检查的进行中与终态，/api/status 下发供前端刷新后恢复）
│           ├── server.py          # aiohttp 服务器启动
│           └── static/            # 前端构建产物（frontend/ npm run build 输出，提交入库，勿手改）
│               ├── index.html     # Vite 入口（hash 路由单页）
│               ├── world.json     # 世界地图 GeoJSON（ECharts registerMap，懒加载）
│               └── assets/        # 带内容 hash 的 JS/CSS（immutable 缓存）
├── frontend/                      # Web 面板前端工程（Vue 3 + Vite + TypeScript + Tailwind CSS 4 + ECharts + Pinia）
│   ├── package.json               # 依赖与脚本（dev/build/test）
│   ├── vite.config.ts             # 构建（outDir=src/framework/web/static）+ dev 代理 /api→8083
│   ├── index.html                 # 入口模板（主题防闪屏内联脚本）
│   ├── public/world.json          # 地图数据源文件
│   ├── tests/unit/                # Vitest 单测（format/merge/csv/series/status-store/standby-series/useLogPanel/chart-options）
│   └── src/
│       ├── main.ts                # 应用装配（pinia/router/轮询启动）
│       ├── App.vue                # 壳：侧边栏（含底部控制台日志按钮）+ 顶栏（暂停/连接态/主题）+ 路由视图 + Toast
│       ├── router.ts              # hash 路由（dashboard/groups/standby/sources/settings）+ 历史拉取开关
│       ├── types/                 # API DTO（与 api.py 契约对应）与 UI 类型
│       ├── api/client.ts          # 类型化 API 客户端（fetch 封装 + ApiError）
│       ├── stores/                # Pinia：status（轮询+历史合并）/ theme / toast
│       ├── utils/                 # 纯函数：format / csv / merge（顺序冻结）/ series（降采样·速率差分·共享时间窗口·sparkline 坐标）
│       ├── composables/           # useRowSelection（框选）/ usePurity（纯洁度）/ useSpeedTest（按 bind 的测速结果）/ useStandbySeries（逐代理序列增量缓冲）/ useStandbyGeo（归属地节流拉取）/ useLogPanel（控制台日志源：增量轮询+可见性节流+主题联动）
│       ├── vendor/logpanel/       # 日志视窗组件（vendored 零依赖，Shadow DOM + TS 声明 + --lp-* 主题变量化）
│       ├── components/            # ui（ToastHost/ContextMenu）、charts（echarts 注册/option 工厂/ChartBox/GeoMap）、Sparkline（行内 SVG 迷你趋势图）、PurityTags、ScheduleSettingsCard、AddProxyModal
│       ├── pages/                 # DashboardPage / GroupsPage / StandbyPage / SourcesPage / SettingsPage
│       └── styles/                # theme.css（Tailwind 4 @theme 令牌）+ components.css（组件类）
├── tests/                         # 测试目录（复刻 src 结构；tests/e2e/web/ 为面板 Playwright e2e）
├── vendor/
│   ├── pproxy/                    # pproxy 代理库（fork 自 pproxy 2.7.9，本地维护: sslwrap 静默 / sslcontexts 弱引用 / modstat 传 roption / SOCKS5 认证应答与 UDP ASSOCIATE / HTTP 404 / WebSocket 握手修复 / 通道空闲回收改会话双向静默（修复流式长连接被单向 60s 读超时误杀） 等内化进源码；proxy_server.py 通过 sys.path 导入）
│   └── pspeed/                    # 代理测速工具（TCP/IP/下载/上传带宽，JSON 输出；api.py 通过 sys.path 导入 probe_one）
├── logs/                          # 日志目录（毫秒时间戳命名：proxy-pool-{YYYYMMDD-HHMMSS-mmm}.log）
└── .cache.db                      # 代理池状态持久化 SQLite 数据库（WAL 模式，proxies + source_paused + group_meta）
```

> 说明：项目采用干净架构四层（洋葱模型），依赖只能从外层指向内层。组合根 `src/framework/container.py` 是唯一组装点，构造 `ProxyPool` → 从 `.cache.db` 恢复状态 → 组装各用例 → 暴露给 CLI。
