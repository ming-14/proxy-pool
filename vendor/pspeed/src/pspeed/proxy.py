"""代理解析与协议适配：支持 http / https / socks4 / socks5。"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, quote

SUPPORTED = ("http", "https", "socks4", "socks5")

_GUESS = {
    80: "http", 8080: "http", 3128: "http", 8888: "http",
    443: "https", 8443: "https",
    1080: "socks5", 1081: "socks5",
    4145: "socks4",
}


@dataclass(frozen=True)
class Proxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def display(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:***@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def requests_proxies(self) -> dict[str, str]:
        url = self._url_for_requests()
        return {"http": url, "https": url}

    def _url_for_requests(self) -> str:
        auth = ""
        if self.username:
            u = quote(self.username, safe="")
            p = quote(self.password or "", safe="")
            auth = f"{u}:{p}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


def parse_one(text: str) -> Proxy:
    text = text.strip()
    if not text:
        raise ValueError("空代理字符串")

    if "://" in text:
        parsed = urlparse(text)
        scheme = (parsed.scheme or "").lower()
        if scheme not in SUPPORTED:
            raise ValueError(f"不支持的协议: {scheme}")
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            raise ValueError(f"无法解析主机/端口: {text}")
        return Proxy(raw=text, scheme=scheme, host=host, port=port,
                     username=parsed.username, password=parsed.password)

    if ":" not in text:
        raise ValueError(f"缺少端口且无协议前缀: {text}")
    host, _, port_s = text.partition(":")
    port_s = port_s.split("/")[0].strip()
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"端口非整数: {text}")
    scheme = _GUESS.get(port, "http")
    return Proxy(raw=f"{scheme}://{host}:{port}", scheme=scheme,
                 host=host, port=port)


def parse_many(items: list[str]) -> list[Proxy]:
    out, seen = [], set()
    for it in items:
        try:
            p = parse_one(it)
        except ValueError:
            continue
        key = (p.scheme, p.host, p.port)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def load_file(path: str) -> list[str]:
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines
