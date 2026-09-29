项目更新时，**必须同步更新此文档**、`docs/`（cli / architecture / filestree）、`src/GUIDE.md`（测试目录树）和 web 端（`frontend/`）
请检查：
- [ ] `README.md`更新了吗
- [ ] `docs/cli.md` / `docs/architecture.md` / `docs/filestree.md` 更新了吗
- [ ] `src/GUIDE.md`（测试树）更新了吗
- [ ] web 端（`frontend/` 源码 + `npm run build` 产物）更新了吗


---

# proxy-pool

代理池管理工具。自动从多个免费代理源拉取代理，支持分组管理、TTL 过期、成功/失败追踪、黑名单淘汰、源暂停、多线程并发调度，并提供本地 HTTP/SOCKS4（含 4a）/SOCKS5 正向代理服务器（TCP only：SOCKS5 UDP ASSOCIATE 回 REP `0x07`，不做 UDP 直连；自动心跳 + 无缝切换；**上游不可用时拒绝请求而不回退直连**，避免泄漏本机真实 IP）。

Web 控制面板支持 Windows 资源管理器式代理列表框选/多选（蓝色矩形框选、Ctrl/Shift 多选、Ctrl+A 全选、批量拉黑/移动/删除），提供 **IP 分布世界地图**（ECharts，IP 归属 GeoLite2 离线解析，城市级散点颜色深浅表示密度），侧边栏底部可启停**控制台日志面板**（悬浮视窗实时查看 serve 日志，支持级别/模块筛选、搜索、导出；面板未开启时不产生日志接口请求），详见 [docs/cli.md](docs/cli.md)。

代理服务队列页把每个代理的**延时趋势与流量速率内联在行下**（自绘 SVG 迷你曲线，按时间戳映射、探测失败处断线，全部行共用同一时间窗口：新入队行左端留白、采样已停止的行不贴右边缘；左侧一列标出该图的数据值区间，流量为 0 的间隔不绘制），连同协议/归属地/入队时长/纯净度与手动测速结果，让代理信息与质量在**同一页面滚动可见**（无逐项显示开关，四块质量信息直接常显；只需时用「收起图表」切回单行密度）；代理质量由后端心跳自动管理（失败计数→退避补测→拉黑淘汰），前端不主动做质量标记。

IP 纯净度检查与代理配置见 `config/ippure.toml`：检查源为插件（`src/interfaces/ippure/`，内置 ipok 与 AbuseIPDB），新增源只需加一个模块并在配置里声明；密钥统一放在 `config/.env`（加载时注入环境变量，不入 TOML）。

代理源清单与拉取调度见 `config/sources.toml`。其中 `github_lists` 源把多个仓库的静态列表合并为一个源（按各列表协议补 scheme，由 `group_by_protocol` 分拣入 `github-http` / `github-https` / `github-socks5` 三组）；`raw.githubusercontent.com` 直连在部分网络被中断，其镜像前缀取环境变量 `GITHUB_LISTS_MIRROR`（未设置或为空即直连，代码不含任何镜像地址）——需要时在 `config/.env` 里配。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`PySocks`、`fake-useragent`、`aiohttp`、`aiohttp-socks`、`geoip2`（`pproxy`、`pspeed` 已 vendor 化，见 `vendor/`）

### IP 归属数据库（地图功能需要）

Web 面板「IP 分布」页面依赖 MaxMind **GeoLite2-City** 离线数据库（自带经纬度）。下载后放到 `vendor/geoip/GeoLite2-City.mmdb`：

- 官方：https://www.maxmind.com/en/accounts/current/geoip/downloads（免费注册）
- 或社区构建版（如 `P3TERX/GeoLite.mmdb` 的 GitHub Releases）

未放置时启动仅 WARNING 并禁用地图（serve 正常运行；`[web].enabled=false` 或 `[geoip].enabled=false` 时连 geoip2 依赖链都不加载）；放置后首次打开地图页面自动全量解析并缓存到 `.cache.db` 的 `ip_geo_cache` 表（后续毫秒级返回）。路径与缓存 TTL 见 `config/panel.toml` 的 `[geoip]` 节。

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
python cli.py serve --port 8082

# 查看 .cache.db 的可视化只读查看器（tkinter 界面）
python view.py
```

## 详细文档

- **架构文档**：[docs/architecture.md](docs/architecture.md) - 架构设计、核心机制、serve 机制、代理源、数据持久化
- **前端重构设计**：[docs/design/FE-Refactor-Vue3.md](docs/design/FE-Refactor-Vue3.md) - Web 面板前端架构（Vue 3 + Vite + TS + Tailwind 4）
- **CLI 文档**：[docs/cli.md](docs/cli.md) - 所有命令的详细使用说明
- **文件树**：[docs/filestree.md](docs/filestree.md) - 项目文件结构说明

## 前端开发（Web 面板）

面板源码在 `frontend/`（Vue 3 + Vite + TypeScript + Tailwind CSS 4 + ECharts + Pinia）：

```bash
cd frontend
npm install
npm run build     # 类型检查 + 构建，产物输出到 src/framework/web/static/（构建产物不入库，见 .gitignore）
npm run test      # Vitest 单测（纯逻辑）
npm run dev       # 开发模式（HMR，/api 代理到 127.0.0.1:8083，需先 serve）
```

## 测试

```bash
python -m pytest -v
```

Web 面板浏览器 e2e（`tests/e2e/web/`，Playwright + Chromium，自动起 serve（端口默认取空闲端口，可用
`PP_E2E_SERVER_PORT` / `PP_E2E_WEB_PORT` 固定）、mock 上游代理、独立临时 cache_db，不依赖外网）需先安装：

```bash
pip install playwright
python -m playwright install chromium
```

未安装 playwright 时相关用例自动跳过（不影响其余测试）。前端单测：`cd frontend && npm run test`。

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
