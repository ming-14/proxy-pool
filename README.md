项目更新时，**必须同步更新此文档**和`GUIDE.md`，`tests\GUIDE.md`和web端
请检查：
- [ ] `README.md`更新了吗
- [ ] `GUIDE.md`更新了吗
- [ ] `tests\GUIDE.md`
- [ ] web端


---

# proxy-pool

代理池管理工具。自动从多个免费代理源拉取代理，支持分组管理、TTL 过期、成功/失败追踪、黑名单淘汰、源暂停、多线程并发调度，并提供本地 HTTP/SOCKS4/SOCKS5 正向代理服务器（自动心跳 + 无缝切换）。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`PySocks`、`fake-useragent`、`pproxy`、`aiohttp`

## 快速开始

```bash
# 拉取代理
python cli.py fetch

# 查看池状态
python cli.py status

# 用代理访问 URL（自动重试换代理）
python cli.py request https://httpbin.org/ip

# 并发测试池中代理
python cli.py test https://httpbin.org/ip -c 20

# 启动本地代理服务器（HTTP/SOCKS4/SOCKS5），自动心跳 + 无缝切换
python cli.py serve --port 8080

# 查看 .cache.db 的可视化只读查看器（tkinter 界面）
python view.py
```

## 详细文档

- **架构文档**：[docs/architecture.md](docs/architecture.md) - 架构设计、核心机制、serve 机制、代理源、数据持久化
- **CLI 文档**：[docs/cli.md](docs/cli.md) - 所有命令的详细使用说明
- **文件树**：[GUIDE.md](GUIDE.md) - 项目文件结构说明

## 测试

```bash
python -m pytest -v
```

## 作为库使用

```python
from src.framework.container import build_container

c = build_container()

# 用代理请求 URL，自动重试
proxy, resp = c.request_with_retry.execute("https://example.com", max_retries=5)
print(f"使用 {proxy}，状态码 {resp.status_code}")

# 并发测试代理
results = c.check_proxies.execute(["1.1.1.1:8080", "2.2.2.2:3128"], "https://example.com")
for proxy, info in results.items():
    print(f"{proxy}: {'成功' if info['success'] else '失败'}")

c.save_full()  # 持久化到 .cache.db
```
