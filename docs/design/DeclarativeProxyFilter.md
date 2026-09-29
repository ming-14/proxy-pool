# 设计方案：声明式代理筛选规则

## 1. 背景

拉取入池的丢弃规则硬编码在 `src/use_cases/fetch_proxies.py`：

```python
# 155-157
DISABLED_PORTS = {80}

# 160-166
def _is_disabled_proxy(proxy: str) -> bool:
    try:
        port = urlsplit(proxy if "://" in proxy else f"//{proxy}").port
    except ValueError:
        return False
    return port in DISABLED_PORTS
```

调用点 `apply()` 第 120 行，命中即 `add_to_group(blacklist_group, p)`。

问题：规则硬编码、只能按端口、与同类阈值（`dedup_threshold` 等都在 toml）归属不一致、无法按源差异化。

## 2. 方案

规则写在 `config/sources.toml`，对**完整代理 URL** 做正则匹配，**命中即拉黑**。没有动作分支，没有外部数据。

```toml
[[fetch_filter]]
pattern = ":80$"
reason = "端口 80 为 Cloudflare 假代理"
```

IP、端口、scheme 都由正则自己表达（正则内天然是 AND，要 OR 写 `(a|b)`）：

| 想筛 | 正则 |
|---|---|
| 端口 80 | `:80$` |
| HTTP 协议（scheme） | `^http://` |
| 内网 IP 段 | `://(10\|127\|192\.168)\.` |
| 端口 + 协议同时满足 | `^https://.*:8080$` |

匹配目标是完整 URL 全串（含 scheme）。裸 `ip:port` 入池前必被 `_with_protocol` 补 scheme，规则永远面对统一形态。

用 `re.search` 而非 `match`：写 `:80$` 这种局部规则时不必补 `.*` 前缀；要全串锚定就自己写 `^...$`。

### 2.1 配置格式

```toml
# ---------------------------------------------------------------- 入池筛选规则
# 命中即拉黑（进黑名单组，ttl 内不再入池）。对完整代理 URL（含 scheme）做正则匹配，
# 按书写顺序取首个命中，无命中即正常入池。pattern 编译失败启动报错。
# match 为源名 glob（省略或 "*" = 全部源）。
[[fetch_filter]]
pattern = ":80$"
reason = "端口 80 为 Cloudflare 假代理（CONNECT 400）"

# [[fetch_filter]]
# match = "src.interfaces.sources.geonode"
# pattern = "^http://"
# reason = "本源不需要 http"

# [[fetch_filter]]
# pattern = '://(10|127|192\.168)\.'
# reason = "内网地址"
```

三个键：`pattern`（必填）、`reason`（必填，日志用）、`match`（选填，源名 glob）。

### 2.2 数据模型（`src/domain/source_filter.py`，新增）

```python
@dataclass(frozen=True)
class FilterRule:
    pattern: re.Pattern[str]
    reason: str
    source_glob: str = "*"

    def matches(self, source: str, proxy: str) -> bool:
        return (fnmatch.fnmatchcase(source, self.source_glob)
                and self.pattern.search(proxy) is not None)


def match_rule(rules, source: str, proxy: str) -> FilterRule | None:
    """按声明顺序取首个命中"""
    for rule in rules:
        if rule.matches(source, proxy):
            return rule
    return None
```

领域层，零 IO、无第三方依赖（`re` + `fnmatch`——后者已由 `domain/purity.py::resolve_proxy` 使用，同一模式）。

### 2.3 校验（`src/framework/config.py`）

```python
def _parse_filter_rule(raw: dict, index: int) -> FilterRule: ...
FETCH_FILTER_RULES: tuple[FilterRule, ...] = tuple(
    _parse_filter_rule(r, i) for i, r in enumerate(_toml.get("fetch_filter") or [])
)
```

| 校验 | 提示 |
|---|---|
| 未知键 | `第 N 条含未知键 [...]（可选 pattern/reason/match）` |
| 缺 `pattern` | 必填 |
| `pattern` 编译失败 | 带原正则 + 异常 |
| 缺 `reason` | 必填 |

`sources.toml` 补进节归属白名单：

```python
"sources.toml": {"fetch", "sources", "fetch_filter"},
```

### 2.4 用例层改造（`src/use_cases/fetch_proxies.py`）

构造注入 `filter_rules`（必填，与同类策略参数一致），`apply()` 内循环把硬编码判定换成规则匹配：

```python
filtered: dict[str, int] = {}   # 命中规则的 reason → 拉黑数量
...
for p in proxy_list:
    if p in seen or self._pool.find_proxy(p)[0] is not None or self._pool.is_blacklisted(p):
        continue
    seen.add(p)
    rule = match_rule(self._filter_rules, src.name, p)
    if rule is not None:
        self._pool.add_to_group(self._pool.blacklist_group, p)
        filtered[rule.reason] = filtered.get(rule.reason, 0) + 1
        continue
    group = _resolve_group(src, p)
    if not self._pool.add_to_group(group, p):
        logger.warning("代理 %s 未入池（组 %s 未定义或协议不受支持）", p, group)
        continue
    new_count += 1
# 命中是批量事件（FPW 一次常上百个），逐条记会刷屏，按规则汇总
for reason, count in filtered.items():
    logger.info("入池筛选拉黑 %d 个代理 (%s)", count, reason)
```

删除 `DISABLED_PORTS` / `_is_disabled_proxy`，不留兼容接口。

`container.build_container` 传 `filter_rules=config.FETCH_FILTER_RULES`。同时删掉现在已无用的 `from urllib.parse import urlsplit`。

### 2.5 现状等价改写

| 现状 | 新配置 |
|---|---|
| `DISABLED_PORTS = {80}` → 入黑名单 | `pattern = ":80$"` |

行为完全一致：入黑名单、blacklist ttl 10800s、3h 后过期可重新入池。`:80$` 的 `$` 锚定行尾，`:8080` 不会被误命中。

## 3. 涉及文件

| 文件 | 改动 |
|---|---|
| `src/domain/source_filter.py` | 新增：`FilterRule` / `match_rule` |
| `src/framework/config.py` | 新增 `_parse_filter_rule` + `FETCH_FILTER_RULES`；节归属补 `fetch_filter` |
| `config/sources.toml` | 新增 `[[fetch_filter]]`（端口 80 规则） |
| `src/use_cases/fetch_proxies.py` | `apply()` 换规则匹配；删 `DISABLED_PORTS` / `_is_disabled_proxy` / `urlsplit` 导入 |
| `src/framework/container.py` | 注入 `filter_rules` |
| `tests/domain/test_source_filter.py` | 新增：匹配单测 |
| `tests/framework/test_config.py` | 补：非法配置报错用例 |
| `tests/use_cases/test_use_cases.py` | 补：入池筛选端到端 |
| `docs/cli.md` / `README.md` / `src/GUIDE.md` | 同步说明与用例数 |

## 4. 测试计划

| 层级 | 用例 |
|---|---|
| 单测 | `:80$` 命中 `...:80`、不命中 `...:8080`；`^http://` 只命中 http；IP 段正则 |
| 单测 | `match` glob 限定源；省略 `match` 对所有源生效；首个命中即返回（顺序敏感） |
| 单测 | 配置校验：未知键 / 缺 pattern / 正则非法 / 缺 reason 全部报错 |
| e2e | 端口 80 代理进 blacklist（**回归现状**）；未命中规则的代理正常入池 |

## 5. 取舍

| 项 | 说明 |
|---|---|
| 只有拉黑，没有 drop/group | 按需求简化。真要丢弃时写规则移出即可（面板可手动 move） |
| 只有 AND，没有 OR | 正则内 `(a\|b)` 已能表达 OR |
| 正则写错会误拉黑 | 启动只挡语法错误；语义错误靠 `reason` 在日志显式暴露 |
| 顺序敏感 | 取首个命中，特化规则写前面，配置注释写明 |
| 规则很多时每代理多次匹配 | 20 条内可忽略；上百条需先按源名分组 |
| 命中日志按规则汇总 | 命中是批量事件（FPW 一次常上百个），逐条记会刷屏，故只按 `reason` 记一行数量 |
