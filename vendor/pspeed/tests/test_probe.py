import pytest

from pspeed.proxy import parse_one, parse_many
from pspeed.cli import _parse_bytes
from pspeed.nodes import DOWNLOAD_NODES, UPLOAD_NODES, IP_NODES, get_download_nodes


def test_parse_http():
    p = parse_one("http://1.2.3.4:8080")
    assert p.scheme == "http" and p.host == "1.2.3.4" and p.port == 8080


def test_parse_socks5_auth():
    p = parse_one("socks5://user:pass@5.6.7.8:1080")
    assert p.scheme == "socks5" and p.port == 1080
    assert p.username == "user" and p.password == "pass"
    assert p.requests_proxies()["https"] == "socks5://user:pass@5.6.7.8:1080"


def test_parse_no_scheme_guess():
    p = parse_one("9.9.9.9:1080")
    assert p.scheme == "socks5"
    p2 = parse_one("9.9.9.9:8080")
    assert p2.scheme == "http"
    p3 = parse_one("9.9.9.9:443")
    assert p3.scheme == "https"


def test_parse_dedup():
    items = ["http://1.1.1.1:8080", "1.1.1.1:8080", "http://2.2.2.2:3128"]
    out = parse_many(items)
    assert len(out) == 2


def test_parse_bytes():
    assert _parse_bytes("1024") == 1024
    assert _parse_bytes("1K") == 1024
    assert _parse_bytes("2M") == 2 * 1024 * 1024
    assert _parse_bytes("1G") == 1024 ** 3
    assert _parse_bytes("1.5M") == int(1.5 * 1024 * 1024)


def test_nodes_present():
    assert len(DOWNLOAD_NODES) >= 15
    assert len(UPLOAD_NODES) >= 2
    assert len(IP_NODES) >= 2
    names = {n.name for n in DOWNLOAD_NODES}
    assert {"cloudflare", "aliyun", "tsinghua"}.issubset(names)


def test_get_download_nodes_filter():
    sub = get_download_nodes(["cloudflare", "aliyun"])
    assert len(sub) == 2
    assert {n.name for n in sub} == {"cloudflare", "aliyun"}
    alln = get_download_nodes(["all"])
    assert len(alln) == len(DOWNLOAD_NODES)


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_one("ftp://1.2.3.4:21")
    with pytest.raises(ValueError):
        parse_one("")
