# proxy-pool

代理池管理工具。自动从多个免费代理源拉取代理，支持分组管理、TTL 过期、成功/失败追踪、黑名单淘汰、多线程并发调度。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`fake-useragent`

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
```

## 命令

### fetch — 拉取代理

从所有启用的代理源拉取代理并写入缓存。

```bash
python cli.py fetch [--json]
```

### list / ls — 列出可用代理

```bash
python cli.py list [--group GROUP] [--json]
```

- `--group`：按组筛选，可选 `default`、`freeproxy-world`、`trusted`
- `--json`：JSON 格式输出

### get — 随机获取代理

池空时自动拉取。

```bash
python cli.py get [-n N] [--json]
```

### status — 查看代理池状态

显示各组代理数量、TTL、暂停的代理源。

```bash
python cli.py status
```

### blacklist — 加入黑名单

```bash
python cli.py blacklist 1.2.3.4:8080 5.6.7.8:3128
```

### trust — 加入信任池

```bash
python cli.py trust 1.2.3.4:8080
```

### request — 用代理访问 URL

自动从池中选取代理，失败时换代理重试。成功的代理自动加入信任池，失败过多的自动拉黑。

```bash
python cli.py request URL [--timeout 10] [--retries 5] [--max-length N] [--headers]
```

- `--retries`：最大重试次数（默认 5），每次换不同代理
- `--max-length`：响应内容截断长度
- `--headers`：仅输出响应头

### test — 测试代理

**单代理模式：**

```bash
python cli.py test 1.2.3.4:8080 https://httpbin.org/ip
```

**并发模式：** 多线程同时测试池中多个代理，输出表格化结果。

```bash
python cli.py test https://httpbin.org/ip -c 20
```

- `-c / --concurrent`：并发测试 N 个代理，省略 proxy 参数时自动进入并发模式

## 代理分组

| 组 | TTL | 说明 |
|---|---|---|
| `default` | 240s | 普通代理，来自快速 API 源 |
| `freeproxy-world` | 18000s | FreeProxy.World 爬取的代理，与源暂停时长一致 |
| `trusted` | 永不过期 | 成功使用过的代理 |
| `blacklist` | 10800s | 失败过多的代理，过期后可重新入池 |

## 代理源

| 模块 | 说明 |
|---|---|
| `89ip-cn` | 89IP 免费代理 |
| `freeproxy-world` | FreeProxy.World（5 阶段爬取：直连→多线程爬 HTTPS→代理获取 HTTP→多线程爬 HTTP→写缓存） |
| `geonode-com` | Geonode 代理 API |
| `goodips-com` | Goodips 代理 |
| `proxy-scdn-io` | ProxyScdn 代理 |

在 `config.py` 的 `SOURCES` 列表中配置启用/禁用。

## 项目结构

```
proxy-pool/
├── cli.py                 # CLI 命令行工具
├── config.py              # 配置（分组、源、超时等）
├── manager.py             # ProxyManager 代理池管理器
├── scheduler.py           # ProxyScheduler 多线程调度器
├── cache.py               # CacheManager 缓存管理
├── ua_generator.py        # User-Agent 生成器
├── log.py                 # 日志模块
├── requirements.txt       # 依赖
├── pytest.ini             # pytest 配置
├── proxy_pool/            # 代理源模块
│   ├── __init__.py
│   ├── 89ip-cn.py
│   ├── freeproxy-world.py
│   ├── geonode-com.py
│   ├── goodips-com.py
│   └── proxy-scdn-io.py
└── test/                  # 测试（208 个用例）
    ├── test_cache.py
    ├── test_cli.py
    ├── test_proxy_fetchers.py
    ├── test_proxy_manager.py
    ├── test_proxy_scheduler.py
    └── test_ua_generator.py
```

## 作为库使用

```python
from manager import ProxyManager
from scheduler import request_with_retry, check_proxies_concurrent

pm = ProxyManager()

# 用代理请求 URL，自动重试
proxy, resp = request_with_retry(pm, "https://example.com", max_retries=5)
print(f"使用 {proxy}，状态码 {resp.status_code}")

# 并发测试代理
results = check_proxies_concurrent(["1.1.1.1:8080", "2.2.2.2:3128"], "https://example.com")
for proxy, info in results.items():
    print(f"{proxy}: {'成功' if info['success'] else '失败'}")

pm.save_cache()
```

## 测试

```bash
python -m pytest test/ -v
```
