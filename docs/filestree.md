# proxy-pool 项目文件树

```
proxy-pool/
├── cli.py                         # 命令行薄入口：引导 sys.path 后委托 src.framework.cli.main()
├── view.py                        # .cache.db 只读查看器（tkinter GUI，独立于业务逻辑）
├── AGENTS.md                      # 工程规范（干净架构、测试规范、用户交互规则）
├── requirements.txt               # 项目依赖（requests / PySocks / fake-useragent / pproxy / aiohttp）
├── pytest.ini                     # pytest 配置（testpaths=tests）
├── config/                        # 配置文件目录
│   └── config.toml                # 唯一配置数据源（分组/源/超时/路径等）
├── src/                           # 主包（干净架构四层）
│   ├── __init__.py
│   ├── domain/                    # 实体层：纯业务规则，零第三方依赖、无 IO
│   │   ├── __init__.py
│   │   ├── proxy_pool.py          # ProxyPool（分组/TTL/晋级/淘汰/源暂停/序列化）
│   │   ├── models.py              # SourceSpec 值对象、normalize_proxy/to_proxy_dict
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
│   │   ├── http_client.py         # RequestsHttpGetter（不校验证书 + 多 IP 重试）
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
│       ├── config.py              # 配置加载（唯一数据源 config/config.toml）
│       ├── container.py           # 组合根（依赖组装）
│       ├── cli.py                 # argparse 命令处理
│       ├── serve.py               # serve 主循环（调度 fill/heartbeat/补测 + 上游同步）
│       ├── logging.py             # 日志模块
│       └── web/                   # Web 控制面板（serve 模式）
│           ├── __init__.py
│           ├── api.py             # REST API（读状态快照/组明细 + 写控制动作/调度策略调整 + /api/proxy/history 单代理历史 + /api/proxy/speedtest 单代理测速 + /api/proxy/move 代理移组）
│           ├── control.py         # ServeControl（serve 运行时控制：fill/heartbeat/补测）
│           ├── server.py          # aiohttp 服务器启动
│           └── static/
│               ├── index.html     # 单页原生 JS 控制面板（定时轮询 /api/status）
│               ├── css/
│               │   ├── base.css       # 基础样式（reset / 变量 / 通用类）
│               │   ├── layout.css     # 布局样式（侧栏 / #main-content overflow-anchor:none / 页面切换）
│               │   └── components.css # 组件样式（card / table / btn / chart / tooltip / back-to-top-btn）
│               └── js/
│                   ├── app.js         # 主入口（路由 / 轮询 / 主题切换）
│                   ├── api.js         # API 封装（fetch 包装 / 各端点方法）
│                   ├── dashboard.js   # 仪表盘页（KPI / 图表）
│                   ├── groups.js      # 代理组页（双向虚拟滚动 / 懒加载 / 排序 / 过滤 / 导出 / 右键菜单）
│                   ├── standby.js     # 代理服务队列页（hover tooltip / 测速 pin / 右键菜单 / pin / lock）
│                   ├── sources.js     # 代理源页
│                   └── charts.js      # Chart.js 封装
├── tests/                         # 测试目录（复刻 src 结构）
├── vendor/
│   └── pspeed/                    # 代理测速工具（TCP/IP/下载/上传带宽，JSON 输出；api.py 通过 sys.path 导入 probe_one）
├── logs/                          # 日志目录（毫秒时间戳命名：proxy-pool-{YYYYMMDD-HHMMSS-mmm}.log）
└── .cache.db                      # 代理池状态持久化 SQLite 数据库（WAL 模式，proxies + source_paused + group_meta）
```

> 说明：项目采用干净架构四层（洋葱模型），依赖只能从外层指向内层。组合根 `src/framework/container.py` 是唯一组装点，构造 `ProxyPool` → 从 `.cache.db` 恢复状态 → 组装各用例 → 暴露给 CLI。
