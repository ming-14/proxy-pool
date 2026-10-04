"""配置加载单测（.env 注入 / 节归属 / ippure.toml 校验）

config 在 import 期加载，故所有用例都在子进程里跑：复制真实 config/ 到临时目录、
按需覆写文件、用 PROXY_POOL_CONFIG 指向它，再 import src.framework.config。

真实 config/ 只当脚手架（给未被覆写的文件提供合法结构），**断言的值必须来自用例自己写入的
覆写内容**——断言真实配置的内容会让测试随开发机的本地实验一起红，那是测试在依赖环境状态。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_SRC = PROJECT_ROOT / "config"
_ENV_KEY = "TEST_IPPURE_KEY"


def _ippure_toml(
    source: str = "abuseipdb",
    *,
    timeout: str = "10",
    cache_ttl: str = "1000",
    ippure_extra: tuple[str, ...] = (),
    plugins: tuple[str, ...] = ("src.interfaces.ippure.abuseipdb", "src.interfaces.ippure.ipok"),
    rules: tuple[tuple[str, str], ...] = (),
    sections: tuple[str, ...] = ('[abuseipdb]\nmax_age_in_days = 90', "[ipok]"),
) -> str:
    """按需拼装 ippure.toml（用例逐项构造，避免脆弱的正则替换）"""
    lines = ["[ippure]", f'source = "{source}"', f"timeout = {timeout}",
             f"cache_ttl = {cache_ttl}", *ippure_extra, ""]
    for name in plugins:
        lines += ["[[plugins]]", f'name = "{name}"', ""]
    for match, use in rules:
        lines += ["[[proxy_rules]]", f'match = "{match}"', f'use = "{use}"', ""]
    for section in sections:
        lines += [section, ""]
    return "\n".join(lines)


def _run_config(tmp_path: Path, *, env_file: str | None = None,
                ippure: str | None = None, panel: str | None = None,
                listen_protocols: tuple[str, ...] | None = None,
                fetch_filter: str | None = None,
                enqueue_rules: str | None = None,
                reject_group: str | None = None,
                reject_group_defined: bool = True,
                reject_group_requestable: bool = False,
                extra_env: dict | None = None,
                drop_ippure: bool = False) -> subprocess.CompletedProcess:
    """复制真实配置目录 → 覆写指定文件 → 子进程 import config"""
    cfg_dir = tmp_path / "config"
    shutil.copytree(CONFIG_SRC, cfg_dir)
    (cfg_dir / ".env").unlink(missing_ok=True)  # 真实 .env 含密钥，绝不带进测试进程
    if env_file is not None:
        (cfg_dir / ".env").write_text(env_file, encoding="utf-8")
    if drop_ippure:
        (cfg_dir / "ippure.toml").unlink()
    elif ippure is not None:
        (cfg_dir / "ippure.toml").write_text(ippure, encoding="utf-8")
    if panel is not None:
        (cfg_dir / "panel.toml").write_text(panel, encoding="utf-8")
    if fetch_filter is not None:
        # 只替换 [[fetch_filter]] 段（sources.toml 的 [fetch]/[[sources]] 是合法结构脚手架）
        src_path = cfg_dir / "sources.toml"
        patched, hits = re.subn(r"\[\[fetch_filter\]\][\s\S]*$",
                                fetch_filter.strip() + "\n",
                                src_path.read_text(encoding="utf-8"))
        assert hits == 1, f"sources.toml 的 [[fetch_filter]] 段命中 {hits} 次（应为 1 次）"
        src_path.write_text(patched, encoding="utf-8")
    if listen_protocols is not None or enqueue_rules is not None or reject_group is not None:
        # 只替换指定行/段（serve.toml 其余键是合法 [server] 结构的脚手架）
        serve_path = cfg_dir / "serve.toml"
        text = serve_path.read_text(encoding="utf-8")
        if listen_protocols is not None:
            value = "[" + ", ".join(f'"{p}"' for p in listen_protocols) + "]"
            text, hits = re.subn(r"^listen_protocols = \[[^\]]*\]",
                                 f"listen_protocols = {value}", text, flags=re.MULTILINE)
            assert hits == 1, f"serve.toml 的 listen_protocols 命中 {hits} 次（应为 1 次）"
        if reject_group is not None:
            text, hits = re.subn(r'^enqueue_reject_group = .*$',
                                 f'enqueue_reject_group = "{reject_group}"',
                                 text, flags=re.MULTILINE)
            assert hits == 1, f"serve.toml 的 enqueue_reject_group 命中 {hits} 次（应为 1 次）"
        if enqueue_rules is not None:
            # 数组表在文件尾部，整段替换（空串 = 无规则）
            text, hits = re.subn(r"\[\[server\.enqueue_rules\]\][\s\S]*$",
                                 enqueue_rules.strip() + "\n" if enqueue_rules.strip() else "",
                                 text)
            assert hits == 1, f"serve.toml 的 [[server.enqueue_rules]] 段命中 {hits} 次（应为 1 次）"
        serve_path.write_text(text, encoding="utf-8")
    if not reject_group_defined or reject_group_requestable:
        pool_path = cfg_dir / "pool.toml"
        text = pool_path.read_text(encoding="utf-8")
        if not reject_group_defined:
            text, hits = re.subn(r'^"enqueue-rejected".*$\n?', "", text, flags=re.MULTILINE)
            assert hits == 1, f"pool.toml 的 enqueue-rejected 组命中 {hits} 次（应为 1 次）"
        if reject_group_requestable:
            text, hits = re.subn(r'(^requestable_groups = \[\n)', r'\1    "enqueue-rejected",\n',
                                 text, flags=re.MULTILINE)
            assert hits == 1, f"pool.toml 的 requestable_groups 命中 {hits} 次（应为 1 次）"
        pool_path.write_text(text, encoding="utf-8")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from src.framework import config as c;"
        "print('SOURCE', c.IPPURE_SOURCE);"
        "print('RULES', [(r.match, r.use) for r in c.IPPURE_PROXY_RULES]);"
        "print('SECTIONS', sorted(c.IPPURE_SECTIONS));"
        "print('LISTEN', c.SERVER_LISTEN_PROTOCOLS);"
        "print('FILTER', [(r.source_glob, r.pattern.pattern, r.reason) for r in c.FETCH_FILTER_RULES]);"
        "print('ENQUEUE', [(r.metric, r.priority, r.enabled, dict(r.params)) for r in c.ENQUEUE_RULES]);"
        "print('REJECT', c.ENQUEUE_REJECT_GROUP);"
        "print('GLOBE', (c.GLOBE_ENABLED, c.GLOBE_SELF_LAT, c.GLOBE_SELF_LNG, c.GLOBE_MAX_FLOWS,"
        " c.GLOBE_PROBE_CONCURRENCY, c.GLOBE_PROBE_TIMEOUT, c.GLOBE_TARGET_CACHE_TTL,"
        " c.GLOBE_ACTIVATION_WINDOW, c.GLOBE_PROBE_WINDOW, c.GLOBE_PRUNE_AFTER, c.GLOBE_DOH_ENDPOINTS));"
        f"print('KEY', repr(__import__('os').environ.get({_ENV_KEY!r})))"
    )
    env = dict(os.environ, PROXY_POOL_CONFIG=str(cfg_dir))
    env.pop(_ENV_KEY, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-c", code, str(PROJECT_ROOT)],
                          capture_output=True, text=True, timeout=60, env=env)


# ---------------------------------------------------------------- .env 注入
def test_env_file_injected(tmp_path) -> None:
    """.env 的键注入进程环境变量（密钥读取统一走环境变量）"""
    r = _run_config(tmp_path, env_file=f"{_ENV_KEY}=abc123\n")
    assert r.returncode == 0, r.stderr
    assert "KEY 'abc123'" in r.stdout


def test_env_file_does_not_override_existing(tmp_path) -> None:
    """已存在的真实环境变量优先：.env 不覆盖"""
    r = _run_config(tmp_path, env_file=f"{_ENV_KEY}=from-file\n",
                    extra_env={_ENV_KEY: "from-env"})
    assert r.returncode == 0, r.stderr
    assert "KEY 'from-env'" in r.stdout


def test_env_file_missing_is_silent(tmp_path) -> None:
    """无 .env 不报错（密钥可由真实环境变量提供）"""
    r = _run_config(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "KEY None" in r.stdout


def test_env_file_values_parsed(tmp_path) -> None:
    """注释/空行/引号/值内含 = 均按 KEY=VALUE 解析"""
    r = _run_config(tmp_path, env_file=f'# c\n\n{_ENV_KEY}="a=b"\n')
    assert r.returncode == 0, r.stderr
    assert "KEY 'a=b'" in r.stdout


def test_env_file_invalid_line_fails_fast(tmp_path) -> None:
    """非 KEY=VALUE 行直接报错并给出行号"""
    r = _run_config(tmp_path, env_file="not-a-kv-line\n")
    assert r.returncode != 0
    assert "第 1 行" in r.stderr


# ---------------------------------------------------------------- serve.toml（入站监听协议）
def test_listen_protocols_parsed(tmp_path) -> None:
    """入站监听协议按配置解析；入站 socks4 仍受支持（出站 socks4 已从策略中移除）"""
    r = _run_config(tmp_path, listen_protocols=("http", "socks4"))
    assert r.returncode == 0, r.stderr
    assert "LISTEN ('http', 'socks4')" in r.stdout


def test_listen_protocols_unknown_fails_fast(tmp_path) -> None:
    """入站协议取 pproxy 不认识的取值时报错（防拼错静默失效）"""
    r = _run_config(tmp_path, listen_protocols=("http", "telnet"))
    assert r.returncode != 0
    assert "telnet" in r.stderr


def test_listen_protocols_empty_fails_fast(tmp_path) -> None:
    """入站协议集合为空报错（监听 URI 无协议段）"""
    r = _run_config(tmp_path, listen_protocols=())
    assert r.returncode != 0
    assert "listen_protocols" in r.stderr


# ---------------------------------------------------------------- ippure.toml
def test_ippure_default_config_parsed(tmp_path) -> None:
    """ippure.toml 按声明的缺省值解析：选中 abuseipdb + 插件节透传 + 无代理规则

    断言用例自带的配置内容，不读工作区真实的 ippure.toml——真实配置随本地实验变动，
    断言它会让测试依赖开发机状态。
    """
    r = _run_config(tmp_path, ippure=_ippure_toml())
    assert r.returncode == 0, r.stderr
    assert "SOURCE abuseipdb" in r.stdout
    assert "RULES []" in r.stdout
    assert "SECTIONS ['abuseipdb', 'ipok']" in r.stdout


def test_ippure_proxy_rules_parsed_in_order(tmp_path) -> None:
    """代理规则按书写顺序解析（首条命中生效由 domain.resolve_proxy 保证）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(rules=(
        ("abuse*", "http://127.0.0.1:7890"),
        ("*", "socks5://127.0.0.1:1080"),
    )))
    assert r.returncode == 0, r.stderr
    assert "[('abuse*', 'http://127.0.0.1:7890'), ('*', 'socks5://127.0.0.1:1080')]" in r.stdout


def test_ippure_source_can_be_disabled(tmp_path) -> None:
    """source 为空字符串 = 禁用纯净度检查（配置仍须合法）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(source=""))
    assert r.returncode == 0, r.stderr
    assert "SOURCE \n" in r.stdout


def test_ippure_unknown_key_fails_fast(tmp_path) -> None:
    """[ippure] 未知键报错（防拼错静默失效）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(ippure_extra=("bad_key = 1",)))
    assert r.returncode != 0
    assert "bad_key" in r.stderr


def test_ippure_missing_required_key_fails_fast(tmp_path) -> None:
    """[ippure] 缺必需键报错"""
    r = _run_config(tmp_path, ippure=_ippure_toml(cache_ttl='"never"'))
    assert r.returncode != 0


def test_ippure_empty_plugins_fails_fast(tmp_path) -> None:
    """[[plugins]] 为空报错（无从加载任何检查源）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(plugins=()))
    assert r.returncode != 0
    assert "plugins" in r.stderr


def test_ippure_non_positive_timeout_fails_fast(tmp_path) -> None:
    """timeout 非正数报错"""
    r = _run_config(tmp_path, ippure=_ippure_toml(timeout="0"))
    assert r.returncode != 0
    assert "timeout" in r.stderr


def test_ippure_proxy_rule_bad_scheme_fails_fast(tmp_path) -> None:
    """代理规则 use 非代理 URL 报错"""
    r = _run_config(tmp_path, ippure=_ippure_toml(rules=(("abuse*", "127.0.0.1:7890"),)))
    assert r.returncode != 0
    assert "use 非法" in r.stderr


def test_ippure_proxy_rule_without_match_fails_fast(tmp_path) -> None:
    """代理规则缺 match 报错"""
    r = _run_config(tmp_path, ippure=_ippure_toml(rules=(("", "http://127.0.0.1:7890"),)))
    assert r.returncode != 0
    assert "match" in r.stderr


def test_misplaced_section_fails_fast(tmp_path) -> None:
    """节放错文件报错（panel.toml 不允许 [weather] 这类未知节）"""
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n[weather]\nx = 1\n")
    assert r.returncode != 0
    assert "不属于本文件" in r.stderr


def test_unknown_plugin_section_is_passed_through(tmp_path) -> None:
    """插件节名由插件自声明：未知节名本层放行、原样透传（组合根按插件元数据核对并报错）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(sections=("[nonsense]",)))
    assert r.returncode == 0, r.stderr
    assert "SECTIONS ['nonsense']" in r.stdout


def test_duplicate_section_across_files_fails_fast(tmp_path) -> None:
    """跨文件重复定义节报错（否则后加载文件静默覆盖前者的节）"""
    r = _run_config(tmp_path, ippure=_ippure_toml(sections=("[groups]\nx = 1",)))
    assert r.returncode != 0
    assert "重复定义节" in r.stderr


def test_missing_ippure_toml_fails_fast(tmp_path) -> None:
    """缺 ippure.toml 报错（配置目录为固定清单，无回退）"""
    r = _run_config(tmp_path, drop_ippure=True)
    assert r.returncode != 0
    assert "ippure.toml" in r.stderr


# ---------------------------------------------------------------- 入池筛选规则
def test_fetch_filter_real_config_parsed(tmp_path) -> None:
    """真实 sources.toml 的端口 80 规则被解析：pattern 编译为对象、reason 保留"""
    r = _run_config(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "FILTER [('*', ':80$', " in r.stdout


def test_fetch_filter_parsed_in_order_with_glob(tmp_path) -> None:
    """规则按书写顺序解析，match 省略时归一为 "*"（用例自带内容，不读真实配置）"""
    r = _run_config(tmp_path, fetch_filter=(
        "[[fetch_filter]]\n"
        'match = "src.interfaces.sources.geonode"\n'
        'pattern = "^http://"\n'
        'reason = "自家规则"\n'
        "\n"
        "[[fetch_filter]]\n"
        'pattern = ":1080$"\n'
        'reason = "通用规则"\n'
    ))
    assert r.returncode == 0, r.stderr
    assert ("FILTER [('src.interfaces.sources.geonode', '^http://', '自家规则'), "
            "('*', ':1080$', '通用规则')]") in r.stdout


def test_fetch_filter_empty_allowed(tmp_path) -> None:
    """无 [[fetch_filter]] 合法（不做任何入池筛选）"""
    r = _run_config(tmp_path, fetch_filter="")
    assert r.returncode == 0, r.stderr
    assert "FILTER []" in r.stdout


def test_fetch_filter_missing_pattern_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, fetch_filter='[[fetch_filter]]\nreason = "只给原因"\n')
    assert r.returncode != 0
    assert "缺少 pattern" in r.stderr


def test_fetch_filter_missing_reason_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, fetch_filter='[[fetch_filter]]\npattern = ":80$"\n')
    assert r.returncode != 0
    assert "缺少 reason" in r.stderr


def test_fetch_filter_bad_regex_fails_fast(tmp_path) -> None:
    """正则编译失败启动报错（否则该规则静默失效）"""
    r = _run_config(tmp_path, fetch_filter='[[fetch_filter]]\npattern = "([unclosed"\nreason = "r"\n')
    assert r.returncode != 0
    assert "pattern 非法" in r.stderr


def test_fetch_filter_unknown_key_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, fetch_filter=(
        '[[fetch_filter]]\npattern = ":80$"\nreason = "r"\naction = "drop"\n'
    ))
    assert r.returncode != 0
    assert "未知键" in r.stderr and "action" in r.stderr


# ---------------------------------------------------------------- 入队筛选规则
def test_enqueue_rules_parsed(tmp_path) -> None:
    """规则按书写顺序解析，参数经 METRIC_SPECS 校验后归一（此处断言用例自带内容）"""
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 1\nmax_latency = 0.5\nenabled = true\n'
        "\n"
        '[[server.enqueue_rules]]\nmetric = "country"\npriority = 2\nallow = ["us", "jp"]\n'
    ))
    assert r.returncode == 0, r.stderr
    assert ("ENQUEUE [('latency', 1, True, {'max_latency': 0.5}), "
            "('country', 2, False, {'allow': ['US', 'JP']})]") in r.stdout
    assert "REJECT enqueue-rejected" in r.stdout


def test_enqueue_rules_absent_allowed(tmp_path) -> None:
    """无 [[server.enqueue_rules]] 合法：不做任何入队筛选"""
    r = _run_config(tmp_path, enqueue_rules="")
    assert r.returncode == 0, r.stderr
    assert "ENQUEUE []" in r.stdout


def test_enqueue_unknown_metric_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "weather"\npriority = 1\n'
    ))
    assert r.returncode != 0
    assert "未知指标" in r.stderr and "weather" in r.stderr


def test_enqueue_unknown_key_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 1\nmax_latency = 1.0\nmin_speed = 5\n'
    ))
    assert r.returncode != 0
    assert "未知键" in r.stderr and "min_speed" in r.stderr


def test_enqueue_missing_required_param_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, enqueue_rules='[[server.enqueue_rules]]\nmetric = "purity"\npriority = 1\n')
    assert r.returncode != 0
    assert "缺少必需参数" in r.stderr and "max_score" in r.stderr


def test_enqueue_invalid_param_fails_fast(tmp_path) -> None:
    """参数值非法在启动期报错（如延时阈值非正数），不静默失效"""
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 1\nmax_latency = 0\n'
    ))
    assert r.returncode != 0
    assert "max_latency" in r.stderr


def test_enqueue_priority_non_positive_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 0\nmax_latency = 1.0\n'
    ))
    assert r.returncode != 0
    assert "priority" in r.stderr


def test_enqueue_priority_duplicate_fails_fast(tmp_path) -> None:
    """优先级重复时顺序不确定，启动即报错"""
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 1\nmax_latency = 1.0\n'
        "\n"
        '[[server.enqueue_rules]]\nmetric = "purity"\npriority = 1\nmax_score = 50\n'
    ))
    assert r.returncode != 0
    assert "priority 重复" in r.stderr


def test_enqueue_duplicate_metric_fails_fast(tmp_path) -> None:
    """同一指标写两条无法定位面板开关/阈值，启动即报错"""
    r = _run_config(tmp_path, enqueue_rules=(
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 1\nmax_latency = 1.0\n'
        "\n"
        '[[server.enqueue_rules]]\nmetric = "latency"\npriority = 2\nmax_latency = 2.0\n'
    ))
    assert r.returncode != 0
    assert "指标重复" in r.stderr


def test_enqueue_reject_group_undefined_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, reject_group_defined=False)
    assert r.returncode != 0
    assert "未在 [groups] 定义" in r.stderr


def test_enqueue_reject_group_requestable_fails_fast(tmp_path) -> None:
    """拒绝组可请求 = 被拒候选立即重新成为候选，规则形同虚设"""
    r = _run_config(tmp_path, reject_group_requestable=True)
    assert r.returncode != 0
    assert "requestable_groups" in r.stderr


# ---------------------------------------------------------------- panel.toml（3D 地球与目标地区探测）
_GLOBE_PANEL = (
    "[geoip]\nenabled = false\n"
    "[globe]\n"
    "enabled = false\n"
    "self_lat = 31.23\n"
    "self_lng = 121.47\n"
    "max_flows = 10\n"
    "probe_concurrency = 1\n"
    "probe_timeout = 2.5\n"
    "target_cache_ttl = 3600\n"
    "activation_window = 30\n"
    "probe_window = 120\n"
    "prune_after = 86400\n"
    'doh_endpoints = ["https://doh.example/resolve"]\n'
)


def test_globe_defaults_when_section_absent(tmp_path) -> None:
    """[globe] 缺省：探测开启、本机坐标未配置、默认三端点按序回退"""
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n")
    assert r.returncode == 0, r.stderr
    assert "GLOBE (True, None, None, 60, 3, 8.0, 86400.0, 60.0, 600.0, 2592000.0," in r.stdout
    assert "'https://cloudflare-dns.com/dns-query', 'https://dns.google/resolve', 'https://223.5.5.5/resolve'" in r.stdout


def test_globe_parsed(tmp_path) -> None:
    """[globe] 全键解析：开关/本机坐标/数值/自定义端点（断言用例自带内容）"""
    r = _run_config(tmp_path, panel=_GLOBE_PANEL)
    assert r.returncode == 0, r.stderr
    assert ("GLOBE (False, 31.23, 121.47, 10, 1, 2.5, 3600.0, 30.0, 120.0, 86400.0, "
            "('https://doh.example/resolve',))") in r.stdout


def test_globe_unknown_key_fails_fast(tmp_path) -> None:
    """[globe] 未知键报错（防拼错静默失效）"""
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n[globe]\nprobe_concurency = 2\n")
    assert r.returncode != 0
    assert "probe_concurency" in r.stderr


def test_globe_single_coordinate_fails_fast(tmp_path) -> None:
    """本机坐标只填一个报错（无法定位本机）"""
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n[globe]\nself_lat = 31.23\n")
    assert r.returncode != 0
    assert "成对配置" in r.stderr


def test_globe_coordinate_out_of_range_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, panel=(
        "[geoip]\nenabled = false\n[globe]\nself_lat = 91.0\nself_lng = 0.0\n"))
    assert r.returncode != 0
    assert "self_lat 越界" in r.stderr


def test_globe_empty_endpoints_fails_fast(tmp_path) -> None:
    """DoH 端点表为空报错（无处发查询）"""
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n[globe]\ndoh_endpoints = []\n")
    assert r.returncode != 0
    assert "doh_endpoints" in r.stderr


def test_globe_bad_endpoint_scheme_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, panel=(
        "[geoip]\nenabled = false\n[globe]\ndoh_endpoints = [\"ftp://doh.example\"]\n"))
    assert r.returncode != 0
    assert "doh_endpoints" in r.stderr


def test_globe_non_positive_value_fails_fast(tmp_path) -> None:
    r = _run_config(tmp_path, panel="[geoip]\nenabled = false\n[globe]\nprobe_concurrency = 0\n")
    assert r.returncode != 0
    assert "probe_concurrency" in r.stderr


def test_globe_section_in_other_file_fails_fast(tmp_path) -> None:
    """[globe] 归属 panel.toml：写到 serve.toml 直接报错（防放错文件静默失效）"""
    r = _run_config(tmp_path, enqueue_rules="[globe]\nenabled = false")
    assert r.returncode != 0
    assert "不属于本文件" in r.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
