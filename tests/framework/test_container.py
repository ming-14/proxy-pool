"""组合根降级行为与惰性导入守卫

覆盖点：
- 纯净度插件：缺环境变量 → purity 为 None；未知源名/模块导入失败/未声明节/插件参数非法 → 抛错
- geoip 库缺失/损坏 → geo 为 None
- web.enabled=false → purity 不构造；geo 仅在启用国家规则时为 serve 路径构造
- 入队筛选测量源接线：国家规则经 geo.resolve_country 判定
- 惰性导入：import container 不得拉起 requests/geoip2/fake_useragent 与纯净度插件模块
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.domain.enqueue_filter import EnqueueRule
from src.framework import config
from src.framework.container import build_container

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cache_db(tmp_path) -> str:
    """每个用例用独立 SQLite 文件，避免污染项目库"""
    return str(tmp_path / "cache.db")


@pytest.fixture(autouse=True)
def _web_enabled(monkeypatch) -> None:
    """purity/geo 构造以 WEB_ENABLED 为前提——显式固定，不依赖本机配置状态"""
    monkeypatch.setattr(config, "WEB_ENABLED", True)


def _enable_abuseipdb(monkeypatch) -> None:
    """启用 abuseipdb 插件并注入假密钥（不依赖本机 .env 状态）"""
    monkeypatch.setattr(config, "IPPURE_SOURCE", "abuseipdb")
    monkeypatch.setenv("ABUSEIPDB_API_KEY", "test-key")


def test_missing_env_key_disables_purity(cache_db, monkeypatch) -> None:
    """缺必需环境变量：只警告并禁用（不阻断启动）"""
    monkeypatch.setattr(config, "IPPURE_SOURCE", "abuseipdb")
    monkeypatch.delenv("ABUSEIPDB_API_KEY", raising=False)

    container = build_container(cache_db=cache_db)

    assert container.purity is None


def test_env_key_present_enables_purity(cache_db, monkeypatch) -> None:
    _enable_abuseipdb(monkeypatch)

    container = build_container(cache_db=cache_db)

    assert container.purity is not None
    assert (container.purity.source, container.purity.label) == ("abuseipdb", "AbuseIPDB")


def test_empty_source_disables_purity(cache_db, monkeypatch) -> None:
    """source 为空 = 禁用纯净度检查（不构造、不 import 插件）"""
    monkeypatch.setattr(config, "IPPURE_SOURCE", "")

    assert build_container(cache_db=cache_db).purity is None


def test_plugin_config_injected(cache_db, monkeypatch) -> None:
    """插件只吃核心注入的 timeout/代理/本节配置/env（自己不读 TOML 与环境变量）"""
    import importlib

    from src.domain.purity import ProxyRule

    _enable_abuseipdb(monkeypatch)
    monkeypatch.setattr(config, "IPPURE_TIMEOUT", 7.5)
    monkeypatch.setattr(config, "IPPURE_PROXY_RULES",
                        (ProxyRule(match="abuse*", use="http://127.0.0.1:7890"),))
    monkeypatch.setattr(config, "IPPURE_SECTIONS", {"abuseipdb": {"max_age_in_days": 30}})
    module = importlib.import_module("src.interfaces.ippure.abuseipdb")
    real_factory = module.create_checker
    captured: dict = {}

    def spy(factory_config):
        captured["config"] = factory_config
        return real_factory(factory_config)

    monkeypatch.setattr(module, "create_checker", spy)

    container = build_container(cache_db=cache_db)

    assert container.purity is not None
    assert captured["config"].timeout == 7.5
    assert captured["config"].proxy == "http://127.0.0.1:7890"
    assert captured["config"].section == {"max_age_in_days": 30}
    assert captured["config"].env == {"ABUSEIPDB_API_KEY": "test-key"}


def test_missing_mmdb_disables_geo(cache_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "IPPURE_SOURCE", "")  # 隔离纯净度分支
    monkeypatch.setattr(config, "GEOIP_ENABLED", True)
    monkeypatch.setattr(config, "GEOIP_DB_FILE", str(tmp_path / "GeoLite2-City.mmdb"))

    container = build_container(cache_db=cache_db)

    assert container.geo is None


def test_unknown_purity_source_still_raises(cache_db, monkeypatch) -> None:
    monkeypatch.setattr(config, "IPPURE_SOURCE", "unknown-source")

    with pytest.raises(ValueError):
        build_container(cache_db=cache_db)


def test_unknown_plugin_module_raises(cache_db, monkeypatch) -> None:
    """[[plugins]] 里的模块路径拼错 → 启动报错（配置错误不做运行期兜底）"""
    monkeypatch.setattr(config, "IPPURE_PLUGIN_MODULES", ("src.interfaces.ippure.nope",))

    with pytest.raises(ValueError) as exc:
        build_container(cache_db=cache_db)

    assert "导入失败" in str(exc.value)


def test_undeclared_plugin_section_raises(cache_db, monkeypatch) -> None:
    """ippure.toml 出现没有任何插件声明的节 → 启动报错（防拼错节名静默失效）"""
    monkeypatch.setattr(config, "IPPURE_SECTIONS", {"nobody": {}})

    with pytest.raises(ValueError) as exc:
        build_container(cache_db=cache_db)

    assert "nobody" in str(exc.value)


def test_plugin_config_invalid_raises(cache_db, monkeypatch) -> None:
    """插件自身参数非法 → 启动报错"""
    _enable_abuseipdb(monkeypatch)
    monkeypatch.setattr(config, "IPPURE_SECTIONS", {"abuseipdb": {"max_age_in_days": 0}})

    with pytest.raises(ValueError) as exc:
        build_container(cache_db=cache_db)

    assert "max_age_in_days" in str(exc.value)


class _FakeGeoLite2:
    """GeoLite2Locator 替身：静态归属 + fingerprint（隔离 mmdb 文件依赖）"""

    fingerprint = "test-fingerprint"

    def __init__(self, path: str) -> None:
        pass

    def locate(self, ip: str) -> dict | None:
        if ip == "1.2.3.4":
            return {"country": "美国", "country_code": "US", "province": None,
                    "city": None, "lat": 1.0, "lng": 2.0}
        return None


def _patch_country_rule(monkeypatch, *, enabled: bool) -> None:
    """注入国家规则并替身定位器（不依赖本机 mmdb 与实际配置）"""
    monkeypatch.setattr(config, "IPPURE_SOURCE", "")  # 隔离纯净度分支
    monkeypatch.setattr(config, "GEOIP_ENABLED", True)
    monkeypatch.setattr(config, "ENQUEUE_RULES", (
        EnqueueRule(metric="country", priority=1, params={"allow": ["JP"]}, enabled=enabled),
    ))
    monkeypatch.setattr("src.interfaces.geoip_client.GeoLite2Locator", _FakeGeoLite2)


def test_web_disabled_skips_purity_and_geo(cache_db, monkeypatch) -> None:
    """web 关闭且无启用规则：purity/geo 都不构造，不 import 依赖链"""
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    monkeypatch.setattr(config, "ENQUEUE_RULES", ())
    _enable_abuseipdb(monkeypatch)
    monkeypatch.setattr(config, "GEOIP_ENABLED", True)

    container = build_container(cache_db=cache_db)

    assert container.purity is None
    assert container.geo is None


def test_geo_constructed_for_country_rule_without_web(cache_db, monkeypatch) -> None:
    """web 关闭但启用了国家规则：geo 照构造（serve 核心路径不再依赖面板开关）

    同时验证组合根把 geo.resolve_country 接成了筛选的国家测量源：
    US ∉ {JP} 应拒绝，且拒绝理由是该国家规则。
    """
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    _patch_country_rule(monkeypatch, enabled=True)

    container = build_container(cache_db=cache_db)

    assert container.geo is not None
    assert asyncio.run(container.geo.resolve_country("http://1.2.3.4:8080")) == "US"
    outcome = asyncio.run(
        container.enqueue_filter.evaluate("http://1.2.3.4:8080", latency=0.1))
    assert outcome.passed is False
    assert outcome.rejected_by is not None and outcome.rejected_by.metric == "country"


def test_purity_constructed_for_purity_rule_without_web(cache_db, monkeypatch) -> None:
    """web 关闭但启用了纯净度规则：插件照加载，筛选经缓存分数拒绝

    缓存直接写库，避免真调检查源；同时验证 (源, IP) 分区下的分数已接通筛选。
    """
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    _enable_abuseipdb(monkeypatch)
    monkeypatch.setattr(config, "ENQUEUE_RULES", (
        EnqueueRule(metric="purity", priority=1, params={"max_score": 50}, enabled=True),
    ))

    container = build_container(cache_db=cache_db)

    assert container.purity is not None
    container.store.save_purity("abuseipdb", "1.2.3.4",
                                {"score": 88, "usage_type": "hosting", "extra": {}})
    outcome = asyncio.run(
        container.enqueue_filter.evaluate("http://1.2.3.4:8080", latency=0.1))
    assert outcome.passed is False
    assert outcome.rejected_by is not None and outcome.rejected_by.metric == "purity"


def test_speed_rule_uses_tester(cache_db, monkeypatch) -> None:
    """测速规则经 PspeedTester.download_mbps 判定（组合根接线）"""
    monkeypatch.setattr(config, "IPPURE_SOURCE", "")
    monkeypatch.setattr(config, "GEOIP_ENABLED", False)
    monkeypatch.setattr(config, "ENQUEUE_RULES", (
        EnqueueRule(metric="speed", priority=1, params={"min_download_mbps": 10}, enabled=True),
    ))
    monkeypatch.setattr("src.interfaces.speedtest._measure", lambda proxy: {
        "proxy": proxy,
        "download": {"ok": True, "best_mbps": 5.0},
        "upload": None,
    })

    container = build_container(cache_db=cache_db)

    outcome = asyncio.run(
        container.enqueue_filter.evaluate("http://1.2.3.4:8080", latency=0.1))
    assert outcome.passed is False
    assert outcome.rejected_by is not None and outcome.rejected_by.metric == "speed"


def test_geo_skipped_when_country_rule_disabled(cache_db, monkeypatch) -> None:
    """规则未启用（且无面板）：不为它加载 mmdb"""
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    _patch_country_rule(monkeypatch, enabled=False)

    container = build_container(cache_db=cache_db)

    assert container.geo is None


def test_standby_windows_injected_from_config(cache_db, monkeypatch) -> None:
    """两个窗口各取各的配置项，不得互串

    留存窗口（`METRICS_WINDOW`）决定采样点保留多久，队列页显示窗口
    （`STANDBY_CHART_WINDOW`）只决定队列页行内图显示多长。二者一旦互串——例如把
    显示窗口当留存注入——延迟/队列采样点会被提前清掉，仪表盘曲线只剩最右一小段。
    两个值故意取成不同数量级，串参即可被断言捕获。
    """
    monkeypatch.setattr(config, "METRICS_WINDOW", 1800)
    monkeypatch.setattr(config, "STANDBY_CHART_WINDOW", 60)

    container = build_container(cache_db=cache_db)

    assert container.standby.metrics_window == 1800
    assert container.standby.chart_window == 60


def test_link_gate_config_invalid_fails_fast(tmp_path) -> None:
    """链路门控配置非法时加载即报错（子进程干净解释器）

    无探测目标会让门控恒判"链路不可用"，serve 的心跳/补测/填充全部静默停摆，
    所以必须在配置加载阶段暴露，不做运行期兜底。
    """
    cfg_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", cfg_dir)
    (cfg_dir / ".env").unlink(missing_ok=True)  # 真实 .env 含密钥，不带进子进程
    serve_toml = cfg_dir / "serve.toml"
    serve_toml.write_text(
        re.sub(r"network_probe_urls = \[[^\]]*\]", "network_probe_urls = []",
               serve_toml.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    code = "import sys; sys.path.insert(0, sys.argv[1]); import src.framework.config"
    r = subprocess.run([sys.executable, "-c", code, str(PROJECT_ROOT)],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PROXY_POOL_CONFIG=str(cfg_dir)))
    assert r.returncode != 0
    assert "network_probe_urls" in r.stderr


def test_container_import_is_lazy() -> None:
    """惰性导入守卫（子进程干净解释器）：import container 不拉起三方重依赖链与纯净度插件

    回归即失败——这些链一旦被顶层 import 拉回，serve/CLI 白载 ~7MB 边际内存。
    """
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import src.framework.container;"
        "bad = [m for m in ('requests', 'geoip2', 'fake_useragent', 'maxminddb')"
        " if m in sys.modules]"
        " + [m for m in sys.modules if m.startswith('src.interfaces.ippure')];"
        "print(bad); sys.exit(1 if bad else 0)"  # 注意 sys.exit('') 空字符串同样返回 1，必须显式码
    )
    r = subprocess.run([sys.executable, "-c", code, str(PROJECT_ROOT)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"container 顶层导入拉起重依赖: {r.stdout.strip()}"
