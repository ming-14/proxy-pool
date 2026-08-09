# proxy-pool 项目文件树

```
proxy-pool/
└── tests/                         # 测试目录（复刻 src 结构）
    ├── __init__.py
    ├── domain/                    # 领域规则测试
    │   ├── __init__.py
    │   └── test_domain_proxy_pool.py
    ├── use_cases/                 # 用例测试
    │   ├── __init__.py
    │   ├── test_use_cases.py
    │   └── test_standby.py
    ├── interfaces/                # 接口测试
    │   ├── __init__.py
    │   ├── test_sqlite_store.py
    │   ├── test_proxy_scheduler.py
    │   ├── test_proxy_server.py
    │   ├── test_ua_generator.py
    │   └── sources/              # 代理源测试
    │       ├── __init__.py
    │       ├── test_proxy_fetchers.py
    │       └── test_freeproxy_world.py
    ├── framework/                 # 框架层测试
    │   ├── __init__.py
    │   ├── test_cli.py
    │   ├── test_view.py
    │   └── test_web_api.py
    └── e2e/                       # 端到端测试（mock 上游）
        ├── __init__.py
        ├── mock_upstream.py       # 本地 mock 上游（模拟 HTTP CONNECT 隧道 + SOCKS5 握手）
        ├── test_serve_e2e.py      # serve 全链路 e2e（pproxy 启动 → 请求转发 → 心跳移除 → sync）
        └── test_serve_loop_e2e.py # serve 主循环 e2e（fill → heartbeat → 补测 → Ctrl+C 优雅退出）
```
