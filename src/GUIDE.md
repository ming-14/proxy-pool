# proxy-pool 测试目录结构

```
tests/                                 # 测试目录（复刻 src 结构；pytest testpaths=tests）
├── __init__.py
├── domain/                            # 领域规则测试
│   ├── __init__.py
│   ├── test_domain_proxy_pool.py      # ProxyPool（分组/TTL/晋级/淘汰/pin/锁定/序列化）
│   ├── test_purity.py                 # 纯净度契约（代理规则 glob 匹配 / 结果归一化）
│   └── test_source_filter.py          # 入池筛选规则（正则匹配 / 源 glob / 首个命中）
├── use_cases/                         # 用例测试
│   ├── __init__.py
│   ├── test_autoload.py               # autoload 清单入队
│   ├── test_check_purity.py           # 纯净度用例（IP 提取/缓存分区与过期/归一化/三步编排边界）
│   ├── test_ip_geo.py                 # IP 归属三步编排与聚合
│   ├── test_network_gate.py           # 本地链路门控（状态码/多目标/失败结论缓存/成功不缓存/并发共享）
│   ├── test_standby.py                # 队列 fill/heartbeat/补测/pin/lock/指标/链路劣化/超容量程
│   ├── test_dns_probe.py              # DNS 探测队列（入队即探测/在途去重/冷却 TTL 与失败退避/限并发/跳过规则/sink 结果交付/is_pending/prune，假端口不打网络）
│   ├── test_traffic_stats.py          # 流量统计（三维计数与采样速率/EMA、pair 容量淘汰与窗口清理、连接级计数含钳零与归零即删、历史条目增量累加与 IP 变动分条、落盘节流/写失败保留/保留期清理）
│   └── test_use_cases.py              # fetch/get/record/request_with_retry/check_proxies
├── interfaces/                        # 接口适配器测试
│   ├── __init__.py
│   ├── test_doh_resolver.py           # DohResolver（端点顺序回退/正负结果解析/IDN punycode/证书策略，假 getter 不打网络）
│   ├── test_geoip_client.py           # GeoLite2Locator City/Country 自动识别（真实 mmdb）
│   ├── test_http_client.py            # http_client 的 socks 路由（socks4/socks5 走 ProxyConnector 且 rdns=True，http/https 复用共享 session）+ get_json（解析/非 200/坏 JSON）
│   ├── test_proxy_scheduler.py        # 异步调度器
│   ├── test_proxy_scheme.py           # 出站协议描述表（engine_uri 上游 URI 映射 / verify_target 证书校验策略）
│   ├── test_proxy_server.py           # PproxyServer（上游同步/绝不直连 direct_fallback=False/上游存活标记/modstat 字节与连接事件的转发契约——统计口径已迁往 use_cases）
│   ├── test_sqlite_store.py           # SqliteProxyStore（增量 upsert/geo 缓存/ippure 缓存按源分区/dns_probe 缓存（bind,host）批量读写与清理/旧表丢弃）
│   ├── test_ua_generator.py           # 确定性 UA 生成
│   ├── ippure/                        # 纯净度插件测试（mock requests，不打真实网络）
│   │   ├── __init__.py
│   │   ├── test_base.py               # 公共骨架（compact 过滤 / 插件导入不拉起重依赖）
│   │   ├── test_abuseipdb.py          # AbuseIPDB（元数据/字段映射/usage 词表/参数校验/代理注入）
│   │   └── test_ipok.py               # ipok.io（同契约，无密钥）
│   └── sources/                       # 代理源测试
│       ├── __init__.py
│       ├── test_freeproxy_world.py    # FPW 多阶段爬取（mock 页面/轮转/限流）
│       └── test_proxy_fetchers.py     # 四 API 源解析 + GitHub 静态列表源（多列表合并/协议补全/镜像前缀/单列表失败隔离）
├── framework/                         # 框架层测试
│   ├── __init__.py
│   ├── test_cli.py                    # argparse 全命令 + setup_logging(debug) 透传
│   ├── test_config.py                 # 配置加载（.env 注入与优先级/节归属与跨文件重名/ippure.toml 与 panel.toml [globe]/[dns_probe] 校验，子进程）
│   ├── test_container.py              # 组合根降级（缺环境变量/未知源名/插件参数非法/缺 mmdb/web 门控）+ 链路门控配置校验（子进程）+ 惰性导入守卫（子进程）
│   ├── test_globe_data.py             # 地球目标弧聚合纯函数（(bind,城市) 分组/hosts 排序截断/无坐标不产弧/top N）
│   ├── test_logging.py                # logging.toml 双通道分档/逐 logger 覆盖
│   ├── test_logbuffer.py              # 面板日志缓冲（环形裁剪/seq 游标/stack 采集/buffer_start）
│   ├── test_loop.py                   # run_coro 跨循环桥接
│   ├── test_view.py                   # view.py 数据层（缓存读取/行构建）
│   ├── test_serve.py                  # serve 主循环辅助逻辑（补测任务表同步：先回收已结束任务再补启动；探测候选 bind→代理反查 _proxy_lookup）
│   └── test_web_api.py                # REST API 全端点（读快照/逐代理序列/队列归属/组明细/控制动作/测速/纯净度/geo/3D 地球（/api/globe 只读最终数据：节点/速率/按城市聚合的 flows，关闭态 404、globe_enabled）/流量统计（/api/traffic/live|history）/日志增量与清空/静态资源（/globe/{name} 贴图白名单））
├── e2e/                               # 端到端测试（本地 mock，不依赖外网）
│   ├── __init__.py
│   ├── mock_upstream.py               # mock 上游代理（HTTP CONNECT 隧道 + SOCKS5 握手，响应体可自定义以区分多上游）
│   ├── server_factory.py              # 测试用 PproxyServer 工厂（注入真实流量统计用例 + 空历史/DNS 探测缓存存储，serve e2e 共用）
│   ├── test_serve_e2e.py              # serve 全链路（pproxy 启动→转发→心跳移除→sync→上游空时拒绝且不直连→不可用上游不被选中）
│   ├── test_serve_loop_e2e.py         # serve 主循环（fill→heartbeat→补测→失败代理随 sync 下发为 unhealthy→优雅退出）
│   ├── test_dns_probe_e2e.py          # DNS 探测队列（含流量即入队探测：无 /api/globe 请求也穿代理 DoH 并落盘 dns_probe_cache；禁用时不探测；地球只读缓存出目标弧）
│   └── web/                           # Web 面板浏览器 e2e（Playwright + Chromium，需 pip install playwright）
│       ├── __init__.py
│       ├── conftest.py                # serve 子进程 + mock 上游 + 临时配置目录隔离 + 端口防呆（默认空闲端口，PP_E2E_* 可固定）+ 组件探针页 fixture
│       ├── logpanel_probe.py          # LogPanel 探针页共享设施（构建/托管探针页 + Probe 驱动器 + 空白断言）
│       ├── test_panel_e2e.py          # 四页导航/主题/框选（含边缘自动滚动）/右键（含二次定位夹取、子菜单翻边与移出）/行下质量带常显与低质量标记/固定置顶/锁定（含锁定态 Ctrl+A 不得选中）/调度参数/源控制/日志面板（含搜索过滤、复制与窄屏抽屉形态）/代理组虚拟滚动改尺寸后仍对齐 36 项
│       ├── test_logpanel_virtual.py   # LogPanel 悬浮形态 14 项（驱动前端 probe/ 构建出的探针页）：全程滚动无空白/滚动中总高恒定/淘汰补偿钉位/跟随贴底且虚拟化生效/手动关跟随不被顶回/暂停继续后补上日志/过滤与高亮/正则非法标红与清空归零/展开态跨行回收保持/隐藏显示跟随与位置/右键菜单开合/拖拽窗口并持久化几何/缩放后重测行高/拖高后仍贴底
│       └── test_logpanel_sheet.py     # LogPanel 抽屉形态 14 项（移动端模拟上下文 390×844 触屏）：形态随宽度切换且内联样式无残留/两形态日志字号一致/满宽贴底不溢出/按钮与标题栏点按切档后仍贴底/拖拽与快滑切档/拖动中底边不悬空/松手从手指位置继续滑向目标档位/长按开菜单不改档位/chrome 不吃掉日志区（390 与 360 宽）/安全区垫底/搜索框 16px 防 iOS 缩放/形态切换后按新宽度重测/视口变矮高度跟随/筛选条开合与横排/抽屉不写悬浮几何
└── vendor/                            # vendor 库回归测试
    ├── test_pproxy_vendor.py          # pproxy 内化修复（SOCKS5 应答与未实现 CMD=REP 0x07/UDP ASSOCIATE 未中继/SOCKS4 应答时序与 4a/socks4 上游远端 DNS/无上游不回退直连/sslcontexts 弱引用等）
    └── test_pspeed_vendor.py          # pspeed 测速口径（curl 传输窗口/真实 hard_limit）/multi 取峰/节点名校验/面板失败渲染
```
