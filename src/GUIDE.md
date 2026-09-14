# proxy-pool 测试目录结构

```
tests/                                 # 测试目录（复刻 src 结构；pytest testpaths=tests）
├── __init__.py
├── domain/                            # 领域规则测试
│   ├── __init__.py
│   └── test_domain_proxy_pool.py      # ProxyPool（分组/TTL/晋级/淘汰/pin/锁定/序列化）
├── use_cases/                         # 用例测试
│   ├── __init__.py
│   ├── test_autoload.py               # autoload 清单入队
│   ├── test_check_purity.py           # 纯洁度多源合并与缓存
│   ├── test_ip_geo.py                 # IP 归属三步编排与聚合
│   ├── test_network_gate.py           # 本地网络门控（缓存/退避/恢复）
│   ├── test_standby.py                # 队列 fill/heartbeat/补测/pin/lock/指标
│   └── test_use_cases.py              # fetch/get/record/request_with_retry/check_proxies
├── interfaces/                        # 接口适配器测试
│   ├── __init__.py
│   ├── test_geoip_client.py           # GeoLite2Locator City/Country 自动识别（真实 mmdb）
│   ├── test_proxy_scheduler.py        # 异步调度器
│   ├── test_proxy_server.py           # PproxyServer（流量计数/历史/上游同步）
│   ├── test_sqlite_store.py           # SqliteProxyStore（增量 upsert/geo 缓存/purity 缓存）
│   ├── test_ua_generator.py           # 确定性 UA 生成
│   └── sources/                       # 代理源测试
│       ├── __init__.py
│       ├── test_freeproxy_world.py    # FPW 多阶段爬取（mock 页面/轮转/限流）
│       └── test_proxy_fetchers.py     # 四 API 源解析
├── framework/                         # 框架层测试
│   ├── __init__.py
│   ├── test_cli.py                    # argparse 全命令 + setup_logging(debug) 透传
│   ├── test_container.py              # 组合根降级（缺 key/缺 mmdb/web 门控）+ 惰性导入守卫（子进程）
│   ├── test_logging.py                # logging.toml 双通道分档/逐 logger 覆盖
│   ├── test_logbuffer.py              # 面板日志缓冲（环形裁剪/seq 游标/stack 采集/buffer_start）
│   ├── test_loop.py                   # run_coro 跨循环桥接
│   ├── test_view.py                   # view.py 数据层（缓存读取/行构建）
│   └── test_web_api.py                # REST API 全端点（读快照/逐代理序列/队列归属/组明细/控制动作/测速/纯洁度/geo/日志增量与清空）
├── e2e/                               # 端到端测试（本地 mock，不依赖外网）
│   ├── __init__.py
│   ├── mock_upstream.py               # mock 上游代理（HTTP CONNECT 隧道 + SOCKS5 握手）
│   ├── test_serve_e2e.py              # serve 全链路（pproxy 启动→转发→心跳移除→sync）
│   ├── test_serve_loop_e2e.py         # serve 主循环（fill→heartbeat→补测→优雅退出）
│   └── web/                           # Web 面板浏览器 e2e（Playwright + Chromium，需 pip install playwright）
│       ├── __init__.py
│       ├── conftest.py                # serve 子进程 + mock 上游 + 临时配置目录隔离 + 端口防呆（默认空闲端口，PP_E2E_* 可固定）
│       └── test_panel_e2e.py          # 四页导航/主题/框选（含边缘自动滚动）/右键（含二次定位夹取与移出）/行下质量带常显与低质量标记/固定置顶/锁定/调度参数/源控制/日志面板 32 项
└── vendor/                            # vendor 库回归测试
    └── test_pproxy_vendor.py          # pproxy 内化修复（SOCKS5 应答/modstat roption/sslcontexts 弱引用等）
```
