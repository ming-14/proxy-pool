项目更新时，**必须同步更新此文档**、`docs/`（cli / architecture / filestree）、`src/GUIDE.md`（测试目录树）和 web 端（`frontend/`）
请检查：
- [ ] `README.md`更新了吗
- [ ] `docs/cli.md` / `docs/architecture.md` / `docs/filestree.md` 更新了吗
- [ ] `src/GUIDE.md`（测试树）更新了吗
- [ ] web 端（`frontend/` 源码 + `npm run build` 产物）更新了吗


---

# proxy-pool

代理池管理工具。自动从多个免费代理源拉取代理，支持分组管理、TTL 过期、成功/失败追踪、黑名单淘汰、源暂停、多线程并发调度，并提供本地 HTTP/SOCKS4（含 4a）/SOCKS5 正向代理服务器（TCP only：SOCKS5 UDP ASSOCIATE 回 REP `0x07`，不做 UDP 直连；自动心跳 + 无缝切换；**上游不可用时拒绝请求而不回退直连**，避免泄漏本机真实 IP）。

Web 控制面板支持 Windows 资源管理器式代理列表框选/多选（蓝色矩形框选、Ctrl/Shift 多选、Ctrl+A 全选、批量拉黑/移动/删除），提供 **IP 分布世界地图**（ECharts，IP 归属 GeoLite2 离线解析，城市级散点颜色深浅表示密度）与仪表盘底部的 **3D 地球**（globe.gl：代理节点 + 本机→代理→目标城市的实时流量飞线，目标落点由穿对应代理的 DoH 解析得到，仅面板查看时探测），侧边栏底部可启停**控制台日志面板**（悬浮视窗实时查看 serve 日志，支持级别/模块筛选、搜索、导出；面板未开启时不产生日志接口请求），详见 [docs/cli.md](docs/cli.md)。

代理服务队列页把每个代理的**延时趋势与流量速率内联在行下**（自绘 SVG 迷你曲线，按时间戳映射、探测失败处断线，全部行共用同一时间窗口：新入队行左端留白、采样已停止的行不贴右边缘；左侧一列标出该图的数据值区间，流量为 0 的间隔不绘制），连同协议/归属地/入队时长/纯净度与手动测速结果，让代理信息与质量在**同一页面滚动可见**（无逐项显示开关，四块质量信息直接常显；只需时用「收起图表」切回单行密度）；代理质量由后端心跳自动管理（失败计数→退避补测→拉黑淘汰），前端不主动做质量标记。

IP 纯净度检查与代理配置见 `config/ippure.toml`：检查源为插件（`src/interfaces/ippure/`，内置 ipok 与 AbuseIPDB），新增源只需加一个模块并在配置里声明；密钥统一放在 `config/.env`（加载时注入环境变量，不入 TOML）。

代理源清单与拉取调度见 `config/sources.toml`。其中 `github_lists` 源把多个仓库的静态列表合并为一个源（按各列表协议补 scheme，由 `group_by_protocol` 分拣入 `github-http` / `github-https` / `github-socks5` 三组）；`raw.githubusercontent.com` 直连在部分网络被中断，其镜像前缀取环境变量 `GITHUB_LISTS_MIRROR`（未设置或为空即直连，代码不含任何镜像地址）——需要时在 `config/.env` 里配。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`PySocks`、`fake-useragent`、`aiohttp`、`aiohttp-socks`、`geoip2`（`pproxy`、`pspeed` 已 vendor 化，见 `vendor/`）

### IP 归属数据库（地图功能需要）

Web 面板「IP 分布」页面与 3D 地球的代理节点定位依赖 MaxMind **GeoLite2-City** 离线数据库（自带经纬度）。下载后放到 `vendor/geoip/GeoLite2-City.mmdb`：

- 官方：https://www.maxmind.com/en/accounts/current/geoip/downloads（免费注册）
- 或社区构建版（如 `P3TERX/GeoLite.mmdb` 的 GitHub Releases）

未放置时启动仅 WARNING 并禁用地图（serve 正常运行；`[web].enabled=false` 或 `[geoip].enabled=false` 时连 geoip2 依赖链都不加载）；放置后首次打开地图页面自动全量解析并缓存到 `.cache.db` 的 `ip_geo_cache` 表（后续毫秒级返回）。路径与缓存 TTL 见 `config/panel.toml` 的 `[geoip]` 节；3D 地球的目标地区探测另见同文件 `[globe]` 节（含手填本机坐标）。目标地区探测会把「目标域名 + 归属」缓存在 `.cache.db` 的 `target_geo_cache` 表：只想停探测设 `[globe].enabled=false`，要清掉已缓存的目标域名则停掉 serve 后清该表内容（任意 SQLite 客户端，如 `sqlite3 .cache.db "DELETE FROM target_geo_cache"`）；行也会按 `[globe].prune_after`（默认 30 天）自动老化。

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

## 桌面版（Electron）

`desktop/` 把项目打包成**免安装的 Windows 桌面程序**：双击文件夹里的 `proxy-pool.exe` 即用，机器上无需安装 Python。

- 启动后弹出**启动器**：只填「代理端口」，点「启动」→ 自动跑 `serve`（网页面板端口由程序挑一个空闲端口，无需填写），就绪后在**内嵌窗口**打开控制面板网页；
- 内置 **Python 3.11 运行时与全部依赖**（官方 embeddable + 预编译 wheel），无需目标机器装 Python；
- **托盘常驻**：关闭任一窗口只隐藏到托盘（serve 继续运行），托盘菜单可打开面板 / 在浏览器中打开 / 打开设置 / 停止 / 重启 / 退出；
- 运行数据 `.cache.db`、`logs/` 写在 **exe 同级目录**；`resources/pyapp/`（代码、config、geoip）与 `resources/python/`（运行时）为随包只读资源。

### 构建

```bash
# 1) 构建前端产物（web/static 不入库，必须先构建）
cd frontend && npm run build && cd ..

# 2) 安装 Electron 依赖
cd desktop && npm install

# 3) 下载内嵌 Python、组装 payload、安装依赖并自检（首次较慢）
npm run prepare:runtime

# 4) 开发态运行 / 打包
npm start          # 直接跑 Electron（用 desktop/staging 的运行时）
npm run dist       # 产出桌面文件夹 desktop/dist/win-unpacked（可整体重命名为 proxy-pool）
```

说明：

- 构建机需要 Node 18+ 与 Python 3.11+（含 pip，用于把依赖装进内嵌运行时）；**目标机器不需要安装 Python**。
- `npm run dist` 产出的是**免安装文件夹**（electron-builder `dir` 目标），不是安装包；把该文件夹整体拷走即可分发，内部无硬编码路径。
- `desktop/node_modules`、`staging/`、`dist/` 均为构建产物（见 `desktop/.gitignore`）。
- 依赖、运行时与 Electron 二进制走国内镜像（npmmirror / 清华 pypi），无需额外配置。
- `config/` 随包只读：代理端口在启动器里改，其余配置（`.env` 密钥、代理源等）需改仓库后重新打包。`make-payload` 会把 `config/.env` 一并打进包，**分发该文件夹等于分发其中的密钥**。
- 桌面端单测：`cd desktop && npm test`。

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
