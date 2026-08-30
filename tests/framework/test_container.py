"""组合根降级行为：可选资源缺失时只禁用对应功能，不抛异常中断启动

覆盖点与 Go 版一致（internal/framework/container_test.go）：
- purity.source=abuseipdb 但缺 API key → purity 为 None
- geoip 库缺失/损坏 → geo 为 None
- 未知 purity.source 仍视为配置错误，保留抛错
"""

from __future__ import annotations

import pytest

from src.framework import config
from src.framework.container import build_container


@pytest.fixture
def cache_db(tmp_path) -> str:
    """每个用例用独立 SQLite 文件，避免污染项目库"""
    return str(tmp_path / "cache.db")


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
