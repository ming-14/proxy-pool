"""组合根降级行为与惰性导入守卫

覆盖点与 Go 版一致（internal/framework/container_test.go）：
- purity.source=abuseipdb 但缺 API key → purity 为 None
- geoip 库缺失/损坏 → geo 为 None
- 未知 purity.source 仍视为配置错误，保留抛错
- web.enabled=false → purity/geo 均不构造（仅面板消费）
- 惰性导入：import container 不得拉起 requests/geoip2/fake_useragent（子进程隔离验证）
"""

from __future__ import annotations

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


def test_missing_apikey_disables_purity(cache_db, monkeypatch) -> None:
    monkeypatch.setattr(config, "PURITY_SOURCE", "abuseipdb")
    monkeypatch.setattr(config, "ABUSEIPDB_API_KEY", None)

    container = build_container(cache_db=cache_db)

    assert container.purity is None


def test_apikey_present_enables_purity(cache_db, monkeypatch) -> None:
    monkeypatch.setattr(config, "PURITY_SOURCE", "abuseipdb")
    monkeypatch.setattr(config, "ABUSEIPDB_API_KEY", "test-key")

    container = build_container(cache_db=cache_db)

    assert container.purity is not None


def test_missing_mmdb_disables_geo(cache_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "PURITY_SOURCE", "ipok")  # 隔离纯洁度分支
    monkeypatch.setattr(config, "GEOIP_ENABLED", True)
    monkeypatch.setattr(config, "GEOIP_DB_FILE", str(tmp_path / "GeoLite2-City.mmdb"))

    container = build_container(cache_db=cache_db)

    assert container.geo is None


def test_unknown_purity_source_still_raises(cache_db, monkeypatch) -> None:
    monkeypatch.setattr(config, "PURITY_SOURCE", "unknown-source")

    with pytest.raises(ValueError):
        build_container(cache_db=cache_db)


def test_web_disabled_skips_purity_and_geo(cache_db, monkeypatch) -> None:
    """web 关闭：purity/geo 仅面板消费，不构造也不 import 依赖链"""
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    monkeypatch.setattr(config, "PURITY_SOURCE", "abuseipdb")
    monkeypatch.setattr(config, "ABUSEIPDB_API_KEY", "test-key")
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


def test_container_import_is_lazy() -> None:
    """惰性导入守卫（子进程干净解释器）：import container 不拉起三方重依赖链

    回归即失败——这些链一旦被顶层 import 拉回，serve/CLI 白载 ~7MB 边际内存。
    """
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import src.framework.container;"
        "bad = [m for m in ('requests', 'geoip2', 'fake_useragent', 'maxminddb')"
        " if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"  # 注意 sys.exit('') 空字符串同样返回 1，必须显式码
    )
    r = subprocess.run([sys.executable, "-c", code, str(PROJECT_ROOT)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"container 顶层导入拉起重依赖: {r.stdout.strip()}"
