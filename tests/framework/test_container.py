"""组合根降级行为与惰性导入守卫

覆盖点：
- 纯净度插件：缺环境变量 → purity 为 None；未知源名/模块导入失败/未声明节/插件参数非法 → 抛错
- geoip 库缺失/损坏 → geo 为 None
- web.enabled=false → purity/geo 均不构造（仅面板消费）
- 惰性导入：import container 不得拉起 requests/geoip2/fake_useragent 与纯净度插件模块
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_web_disabled_skips_purity_and_geo(cache_db, monkeypatch) -> None:
    """web 关闭：purity/geo 仅面板消费，不构造也不 import 依赖链"""
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    _enable_abuseipdb(monkeypatch)
    monkeypatch.setattr(config, "GEOIP_ENABLED", True)

    container = build_container(cache_db=cache_db)

    assert container.purity is None
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
