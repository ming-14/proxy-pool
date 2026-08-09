"""探测核心：TCP 连通 / 回源验证IP / 下载带宽 / 上传带宽。"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import json as _json

import requests
import urllib3

from pspeed.proxy import Proxy
from pspeed.nodes import DownloadNode, UploadNode, IpNode, IP_NODES
from pspeed import geoip

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://speed.cloudflare.com/",
    "Origin": "https://speed.cloudflare.com",
}


def _now() -> float:
    return time.monotonic()


def tcp_ping(proxy: Proxy, timeout: float = 5.0) -> dict:
    t0 = _now()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((proxy.host, proxy.port))
        s.close()
        return {"connected": True, "latency_ms": round((_now() - t0) * 1000, 1)}
    except Exception:
        return {"connected": False, "latency_ms": None}


def _session_for(proxy: Proxy) -> requests.Session:
    sess = requests.Session()
    sess.proxies = proxy.requests_proxies()
    sess.verify = False
    sess.headers.update(_UA)
    return sess


def verify_ip(proxy: Proxy, timeout: float = 12.0, nodes: list[IpNode] | None = None) -> dict:
    nodes = nodes or IP_NODES
    sess = _session_for(proxy)
    for node in nodes:
        try:
            r = sess.get(node.url, timeout=(5, timeout), verify=False)
            if r.status_code != 200:
                continue
            txt = r.text.strip()
            try:
                d = _json.loads(txt)
            except Exception:
                return {"out_ip": txt[:64], "source": node.name, "geo": None}
            ip = d.get("ip") or d.get("origin") or d.get("query") or d.get("ip_addr")
            if ip:
                geo = geoip.lookup(ip)
                return {"out_ip": ip, "source": node.name, "geo": geo}
        except Exception:
            continue
    return {"out_ip": None, "source": None, "geo": None}


def _download_once(sess: requests.Session, node: DownloadNode,
                   want_bytes: int, hard_limit: float) -> dict:
    url = node.url.format(n=want_bytes) if node.param_bytes else node.url
    t0 = _now()
    try:
        r = sess.get(url, timeout=(5, 12), stream=True, allow_redirects=True, verify=False)
        if r.status_code != 200:
            return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                    "node": node.name, "err": f"HTTP {r.status_code}"}
        total = 0
        for chunk in r.iter_content(1024 * 256):
            if not chunk:
                break
            total += len(chunk)
            if total >= want_bytes or _now() - t0 >= hard_limit:
                break
        r.close()
        elapsed = _now() - t0
        if total == 0:
            return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": round(elapsed * 1000, 1),
                    "node": node.name, "err": "空响应"}
        mbps = (total * 8 / 1_000_000) / elapsed if elapsed > 0 else 0.0
        return {"ok": True, "bytes": total, "mbps": round(mbps, 3),
                "ms": round(elapsed * 1000, 1), "node": node.name, "err": None}
    except requests.exceptions.ReadTimeout:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": "读取超时"}
    except Exception as e:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": f"{type(e).__name__}: {e}"}


def download(proxy: Proxy, nodes: list[DownloadNode], want_bytes: int,
             multi: int = 1, hard_limit: float = 30.0) -> dict:
    sess = _session_for(proxy)
    samples: list[dict] = []
    for node in nodes:
        for _ in range(max(1, multi)):
            res = _download_once(sess, node, want_bytes, hard_limit)
            samples.append(res)
            if res["ok"]:
                break
    ok_samples = [s for s in samples if s["ok"]]
    if not ok_samples:
        err = samples[0]["err"] if samples else "无节点"
        return {"ok": False, "best_mbps": 0.0, "avg_mbps": 0.0,
                "bytes": 0, "node": None, "ms": None, "samples": samples, "err": err}
    best = max(ok_samples, key=lambda s: s["mbps"])
    avg = sum(s["mbps"] for s in ok_samples) / len(ok_samples)
    return {"ok": True, "best_mbps": best["mbps"], "avg_mbps": round(avg, 3),
            "bytes": best["bytes"], "node": best["node"], "ms": best["ms"],
            "samples": samples, "err": None}


def _upload_once(sess: requests.Session, node: UploadNode,
                 payload: bytes, hard_limit: float) -> dict:
    """上传测速：优先用 curl（准确测量网络发送时间），fallback requests

    requests 的 sess.post(data=bytes) 把 payload 一次性写入 socket 缓冲区（内存拷贝），
    不等数据实际发到网络，导致 elapsed 偏短、测速偏高。curl 的 speed_upload 由 libcurl
    内部在发送层面测量，准确反映网络上传带宽。
    """
    proxy_url = (sess.proxies or {}).get("https") or (sess.proxies or {}).get("http")
    curl_path = shutil.which("curl")
    if proxy_url and curl_path:
        return _curl_upload(curl_path, proxy_url, node, payload, hard_limit)
    return _requests_upload(sess, node, payload, hard_limit)


def _curl_upload(curl_path: str, proxy_url: str, node: UploadNode,
                 payload: bytes, hard_limit: float) -> dict:
    """用 curl 子进程上传：speed_upload 准确反映网络发送速度"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(payload)
        tmp = f.name
    try:
        cmd = [
            curl_path, "--proxy-insecure", "-x", proxy_url,
            "--data-binary", f"@{tmp}",
            "-H", "Content-Type: application/octet-stream",
            "-H", f"User-Agent: {_UA['User-Agent']}",
            "-H", f"Referer: https://speed.cloudflare.com/",
            "-H", "Origin: https://speed.cloudflare.com",
            "-k", "-s", "-o", os.devnull,
            "-w", "%{http_code} %{time_total} %{speed_upload}",
            node.url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_limit + 5)
        parts = r.stdout.strip().split()
        if len(parts) >= 3 and parts[0].isdigit():
            http_code = int(parts[0])
            time_total = float(parts[1])
            speed_upload = float(parts[2])  # B/s
            if http_code == 200 and speed_upload > 0:
                mbps = (speed_upload * 8) / 1_000_000
                return {"ok": True, "bytes": len(payload), "mbps": round(mbps, 3),
                        "ms": round(time_total * 1000, 1), "node": node.name, "err": None}
            return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                    "node": node.name, "err": f"HTTP {http_code}"}
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": "curl 无输出"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": "超时"}
    except FileNotFoundError:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": "curl 不可用"}
    except Exception as e:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": f"curl: {type(e).__name__}: {e}"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _requests_upload(sess: requests.Session, node: UploadNode,
                     payload: bytes, hard_limit: float) -> dict:
    """requests fallback 上传测速（socket 缓冲区导致偏高，仅 curl 不可用时用）"""
    t0 = _now()
    try:
        r = sess.post(node.url, data=payload, timeout=(10, 30), verify=False,
                      headers={**_UA, "Content-Type": "application/octet-stream"})
        elapsed = _now() - t0
        if r.status_code >= 400:
            return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": round(elapsed * 1000, 1),
                    "node": node.name, "err": f"HTTP {r.status_code}"}
        size = len(payload)
        mbps = (size * 8 / 1_000_000) / elapsed if elapsed > 0 else 0.0
        return {"ok": True, "bytes": size, "mbps": round(mbps, 3),
                "ms": round(elapsed * 1000, 1), "node": node.name, "err": None}
    except requests.exceptions.ReadTimeout:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": "读取超时"}
    except Exception as e:
        return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": 0.0,
                "node": node.name, "err": f"{type(e).__name__}: {e}"}


def upload(proxy: Proxy, nodes: list[UploadNode], want_bytes: int,
           multi: int = 1, hard_limit: float = 30.0) -> dict:
    sess = _session_for(proxy)
    payload = b"\x00" * want_bytes
    samples: list[dict] = []
    for node in nodes:
        for _ in range(max(1, multi)):
            res = _upload_once(sess, node, payload, hard_limit)
            samples.append(res)
            if res["ok"]:
                break
    ok_samples = [s for s in samples if s["ok"]]
    if not ok_samples:
        err = samples[0]["err"] if samples else "无节点"
        return {"ok": False, "best_mbps": 0.0, "avg_mbps": 0.0,
                "bytes": 0, "node": None, "ms": None, "samples": samples, "err": err}
    best = max(ok_samples, key=lambda s: s["mbps"])
    avg = sum(s["mbps"] for s in ok_samples) / len(ok_samples)
    return {"ok": True, "best_mbps": best["mbps"], "avg_mbps": round(avg, 3),
            "bytes": best["bytes"], "node": best["node"], "ms": best["ms"],
            "samples": samples, "err": None}


def probe_one(proxy: Proxy, tests: list[str],
              dl_nodes: list[DownloadNode] | None = None,
              ul_nodes: list[UploadNode] | None = None,
              dl_bytes: int = 2 * 1024 * 1024,
              ul_bytes: int = 1 * 1024 * 1024,
              multi: int = 1, hard_limit: float = 30.0,
              tcp_timeout: float = 5.0) -> dict:
    result: dict = {
        "proxy": proxy.raw,
        "protocol": proxy.scheme,
        "ok": False,
        "tcp": None,
        "ip": None,
        "download": None,
        "upload": None,
        "error": None,
    }

    if "tcp" in tests:
        result["tcp"] = tcp_ping(proxy, timeout=tcp_timeout)
        if not result["tcp"]["connected"]:
            result["error"] = "TCP端口不通"
            return result

    if "ip" in tests:
        result["ip"] = verify_ip(proxy)

    if "download" in tests and dl_nodes:
        result["download"] = download(proxy, dl_nodes, dl_bytes, multi, hard_limit)

    if "upload" in tests and ul_nodes:
        result["upload"] = upload(proxy, ul_nodes, ul_bytes, multi, hard_limit)

    tcp_ok = result["tcp"]["connected"] if result["tcp"] else True
    dl_ok = result["download"]["ok"] if result["download"] else True
    ul_ok = result["upload"]["ok"] if result["upload"] else True
    result["ok"] = tcp_ok and dl_ok and ul_ok
    return result
