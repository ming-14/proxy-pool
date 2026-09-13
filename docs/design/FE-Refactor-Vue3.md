# 前端重构设计方案：Vue 3 + Vite + TypeScript + Tailwind 4

> 决策已与用户对齐：统一 ECharts（含折线图，弃用 Chart.js）；顶层 `frontend/` 工程，
> Vite 构建输出到 `src/framework/web/static/` 由 aiohttp 直接 serve；后端 /api/* 允许为
> 前端体验微调；现有交互功能全部保留；Playwright E2E + Vitest 单测；默认端口改为
> 代理 8082 / 面板 8083。

## 1. 现状问题（重构动因）

旧前端为 10 个顺序加载的手写 JS（~136KB）+ 3 个 CSS + Tailwind Play CDN，核心问题：

1. **无构建工具链**：全局 `window.*` 命名空间互相污染（appState/api/utils/charts/geoMap/
   pageHandlers/createRowSelection），无类型、无模块边界、无 tree-shaking。
2. **Tailwind CDN 在浏览器运行时 JIT**（407KB，每次加载实时编译）。
3. **补丁堆积**：standby.js 934 行、15+ 模块级可变状态；为绕开"2s 轮询 innerHTML 全量重绘"
   打了 8+ 补丁（文本选区冻结、渲染顺序冻结、focus 跳过重绘、innerHTML 比对跳过、
   textContent 变化才写…）；3 个 MutationObserver 分别处理主题重绘与地图容器
   可见性销毁/重建（修 0 尺寸 canvas InvalidStateError 刷屏）。
4. **手工造轮子且无测试**：自研双向虚拟滚动（spacer/行高估算/滚动跳转检测）、增量图表
   状态机（needsRebuild/setBaseline/格式化缓存）、hash 路由、框选引擎。
5. **冗余**：`fmtBytes` 两处实现且格式不一致；groups.js 与 standby.js 右键菜单/定位/批量
   操作近乎复制；手工 `esc()` 防 XSS 散落 60+ 处；`pageHandlers` 三处防御性初始化。
6. **vendor 大文件入库**：echarts.min.js 1MB、world.json 1MB、chart.umd.min.js 208KB、
   tailwindcss.js 407KB——应为 npm 依赖 + 构建产物。
7. **双图表库并存**：Chart.js（折线）+ ECharts（地图），重复的渲染管线与主题同步逻辑。

## 2. 目标架构

### 2.1 分层（洋葱架构映射）

依赖只能由外向内；页面/组件不直接触碰 fetch 与 DOM 细节。

```
frontend/src/
  types/        # API DTO 类型（StatusSnapshot/GroupDetail/HistoryPoint/…）——最内层契约
  api/          # 类型化 API 客户端（fetch 封装 + 各端点函数）
  utils/        # 纯函数：format（字节/时长/延迟/时间）、csv、geo 文本映射 —— Vitest 直测
  stores/       # Pinia：status（轮询+历史合并+连接态+暂停）、theme、toast
  composables/  # 复用交互逻辑：useRowSelection（框选/多选）、useContextMenu、
                # useProxyTooltip、useProxyHistory（hover 数据）、usePurity
  components/   # 展示组件：ui/（Card/Badge/Button/Modal/EmptyState/Skeleton）、
                # charts/（TrendChart 折线工厂、ProxyMiniCharts、GeoMap）、
                # layout/（AppSidebar/AppHeader）、tables/（VirtualTable 行渲染）
  pages/        # 路由视图：DashboardPage / GroupsPage / StandbyPage / SourcesPage / SettingsPage
  router.ts     # vue-router（hash 模式，保持 #dashboard 等 URL 不变）
  App.vue       # 壳：侧边栏 + 顶栏 + <RouterView> + Toast 容器
  main.ts
```

### 2.2 技术选型

| 关注点 | 选型 | 替代旧实现 |
|---|---|---|
| 框架 | Vue 3 `<script setup>` + TS | 手写 DOM 模板字符串 |
| 构建 | Vite 7（`base:'/'`，outDir=`../src/framework/web/static`，emptyOutDir） | `<script>` 顺序加载 |
| 样式 | Tailwind CSS 4（`@tailwindcss/vite`，`@theme` 令牌 + `@custom-variant dark`）；组件类（btn/card/badge/table/toast/modal/context-menu/marquee/proxy-tooltip）用 CSS 重写，度量与旧值一致 | Play CDN + 3 个 CSS |
| 状态 | Pinia（status/theme/toast）+ 页面局部 composable | `window.appState` |
| 路由 | vue-router 4 hash 模式 | 手写 hashchange |
| 图表 | **ECharts 6 单库**（LineChart+ScatterChart+GeoComponent+VisualMap+Tooltip+Legend，按需引入 tree-shake，canvas 渲染器） | Chart.js + ECharts |
| 世界地图数据 | `frontend/public/world.json`（懒加载 fetch + registerMap 全局一次） | static/vendor/world.json |
| 虚拟滚动 | @tanstack/vue-virtual（双向动态测量） | 手工 spacer/estRowH |
| 单测 | Vitest（utils/stores/composables 纯逻辑） | 无 |
| E2E | Python playwright（`tests/e2e/web/`，pytest 集成，真实 serve 端口运行时挑空闲端口） | 无 |

### 2.3 数据流

- **status 轮询**：`useStatusStore.tick()` 每 2s `GET /api/status`；仅 dashboard/standby
  页激活时增量 `GET /api/status/history?since=cursor`，store 内合并裁剪（MAX 1000 点）。
  暂停按钮只停轮询（保留最后数据），连接失败置 offline 态。
- **图表响应式**：`TrendChart.vue` 接收数据数组 prop，`watch` → `setOption`（趋势图
  `animation:false`，避免每 2s 动画重放；ECharts 全量 setOption 900 点/2s 无压力，
  旧版增量状态机整体废除）。主题切换 watch theme store → 重建 option（颜色取自
  CSS 变量单一来源），不再需要 MutationObserver。
- **地图生命周期**：`GeoMap.vue` 组件挂载时 init、卸载时 dispose（Vue 生命周期天然解决
  旧版"切页 0 尺寸 canvas"问题）；数据 60s 节流刷新；roam 状态在 option 内保留。
- **代理组明细**：组选择/关键字/排序/滚动位置为 GroupsPage 局部状态；数据
  `GET /api/group/{name}` 一次拉全量，前端过滤排序 + vue-virtual 渲染。
- **框选/多选**：`useRowSelection` composable 保留资源管理器语义（拖拽阈值 4px、
  Ctrl 切换、Shift 锚点范围、Ctrl+A/Esc、边缘自动滚动 rAF、坐标→索引经 virtualizer
  `offsetToIndex`），选中集为 `Set<string>`（proxy key），行高亮由 class 绑定实现。
- **hover 统计浮窗**：`useProxyTooltip`（悬停 300ms 显示、离开 100ms 销毁、点击 pin、
  测速期间 pin、外部点击关闭、滚动关闭、拖拽抑制），内部 2s 拉
  `GET /api/proxy/history?bind=`。文本选择冻结补丁**全部删除**——Vue 只更新绑定节点，
  重渲染不再重建 `<code>` 文本节点，选区天然不丢。

## 3. 视觉还原基线（对照旧面板参考截图）

折线图统一由共享 option 工厂产出，关键参数对齐 Chart.js 旧渲染效果：

- 网格/坐标轴：`splitLine` 色 `--border-color`，刻度文字 `--text-secondary`，x 轴时间
  `HH:MM:SS`（maxTicksLimit≈8 用 `interval` 自适应），轴标题（字节/s、延迟 s、健康率 %、数量）。
- 对数轴（延迟、流量）：`type:'log'`，`axisLabel.formatter` 输出千分位整数（10/100/500/
  1,000/50,000…），log base 10 + 1-2-5 刻度对齐 Chart.js 观感。
- 线型：`smooth: 0.4`（≈tension 0.35）、`symbol:'none'`、`lineStyle.width:2`、
  平均/健康率/队列长度/上下行带 `areaStyle`（颜色 alpha 0x20/0x15），最大/最小延迟、
  平均延迟(流量图) 无填充。
- 颜色：primary `#3b82f6`、success `#10b981`、warning `#f59e0b`（深浅主题同值）。
- 图例：顶部居中，圆形 icon（`itemStyle` 描边风格：白底/透明底+彩边，对齐 Chart.js
  usePointStyle），文字 `--text-secondary`。
- tooltip：`trigger:'axis'`，背景 `--bg-card`、边框 `--border-color`、文字色对齐，
  圆角阴影由 ECharts 内建。
- 锁定态：健康率卡隐藏、延迟卡占满整行（grid 布局响应 `locked_proxy`）。
- 地图：geo roam + 城市散点（`symbolSize=3+sqrt(count)*4`）、visualMap calculable
  左下"密集/稀疏"、深浅两套 areaColor/borderColor、波纹默认关。
- 表格/卡片/按钮/badge/toast/modal/右键菜单/框选矩形：CSS 度量逐项照搬旧 components.css
  （radius/shadow/padding/字号/颜色变量），hover 行背景、选中行 `--primary-100`+左 3px 蓝条。
- 主题：`html.dark` class（Tailwind 4 `@custom-variant dark`），localStorage key
  `proxy-pool-theme` 保留，index.html 内联脚本防闪屏保留。

## 4. 后端改动（最小集，契约基本不变）

1. `api.py`：`_STATIC_DIR` 语义改为 Vite 产物目录；`/` 读 `static/index.html`（mtime 缓存），
   `add_static("/assets", …)` 服务带 hash 产物（`Cache-Control: immutable`），移除对
   `/assets` 的 no-cache 中间件（旧 no-cache 仅对 index.html 保留）。
2. `/api/status` 与 `/api/status/history` 移除前端未使用的 `per_proxy_traffic` 字段
   （每 2s 轮询少传冗余数据）。其余端点与响应结构不变。
3. `config/config.toml`：`[server] port=8082`、`[web] port=8083`；README/docs/cli.md/
   architecture.md/filestree.md 同步。
4. 删除旧手写前端：`static/js/*`、`static/css/*`、`static/vendor/*`（chart.umd.min.js、
   tailwindcss.js、echarts.min.js；world.json 移入 `frontend/public/`）、旧 `index.html`。
5. `tests/framework/test_web_api.py` 同步 per_proxy_traffic 断言。

## 5. 测试与验证

- **Vitest**：format 工具（字节/时长/延迟/国家名映射）、历史合并裁剪、CSV 转义、
  status store（mock fetch：轮询/暂停/离线/增量游标）、rowSelection 纯逻辑。
- **Playwright E2E**（`tests/e2e/web/`，pytest）：起真实 serve（8082/8083，复用现有
  .cache.db 数据）覆盖——五页导航与标题、主题切换（截图比对暗/亮）、KPI 渲染、
  三趋势图 canvas 存在、地图渲染与统计文本、组选择/过滤/排序/虚拟滚动行高、框选+
  Ctrl/Shift/Ctrl+A/Esc、右键菜单移动/拉黑/还原/删除、添加模态框、导出 CSV/JSON、
  队列页固定/锁定/解锁、hover tooltip+测速按钮、纯洁度检查、设置页调度参数编辑保存、
  源暂停/恢复/拉取、暂停轮询按钮、断连指示。
- **视觉验收**：对 5 张参考图逐项截图比对（延迟趋势/健康率/流量+KPI/hover 浮窗/组表格），
  差异仅允许因渲染引擎不同导致的微小差别。

## 6. 实施顺序

1. 脚手架 + 依赖安装（frontend/，npm）
2. 基础层：types/api/utils/stores/theme/Tailwind 组件样式
3. App 壳 + 路由 + Toast + 侧边栏/顶栏
4. 图表组件（option 工厂 + TrendChart + GeoMap）→ 仪表盘页
5. 代理组页（virtual table + selection + contextmenu + modal + export）
6. 队列页（tooltip/测速/纯洁/设置/锁定）
7. 源页
8. 后端接入 + 端口 + 文档 + 删旧
9. Vitest + Playwright E2E + 视觉比对回归
