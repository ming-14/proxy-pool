"""探测核心：TCP 连通 / 回源验证IP / 下载带宽 / 上传带宽。

带宽口径统一为「传输阶段字节数 / 传输阶段时长」，下载与上传由同一套规则算出：
- 下载：curl time_starttransfer → time_total（扣掉建连、代理协商与 TTFB）
- 上传：curl time_pretransfer → time_starttransfer（扣掉建连与首字节前的协商）
- requests 回退：首字节 → 末字节（与 curl 下载同口径）
传输窗口短于 _MIN_WINDOW 时首字节耗时占比过大，扣除会把速率放大成噪声，此时退回
含全过程的耗时口径（见 _transfer_mbps）。

时限：hard_limit 是真实总时限——curl 用 --max-time（到点断开，-w 仍输出已完成的
统计），requests 回退把建连/读超时压到 hard_limit 以内并在每个 chunk 后判截止。
"""
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

_CONNECT_TIMEOUT = 5.0   # requests 回退路径建连超时（超过 hard_limit 时按 hard_limit 收缩）
_READ_TIMEOUT = 12.0     # requests 回退路径读超时（同上）
_MIN_WINDOW = 0.3        # 传输窗口下限秒数：低于此值不足以代表稳态吞吐
_CHUNK = 256 * 1024      # 流式读取块大小


def _now() -> float:
    return time.monotonic()


def tcp_ping(proxy: Proxy, timeout: float = 5.0) -> dict:
    """TCP 连通性探测（时延）

    经 create_connection 走 getaddrinfo：IPv4/IPv6 字面量与域名统一处理。
    硬编码 AF_INET 会让 IPv6 代理（如 [::1]:1080）一律判为不通而遭误淘汰。
    """
    t0 = _now()
    try:
        with socket.create_connection((proxy.host, proxy.port), timeout=timeout):
            pass
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


def _ok(node_name: str, size: int, mbps: float, ms: float | None) -> dict:
    return {"ok": True, "bytes": size, "mbps": mbps, "ms": ms, "node": node_name, "err": None}


def _fail(node_name: str, err: str, ms: float = 0.0) -> dict:
    return {"ok": False, "bytes": 0, "mbps": 0.0, "ms": ms, "node": node_name, "err": err}


def _transfer_mbps(size: int, window: float, elapsed: float) -> float:
    """传输阶段速率（Mbps）：窗口不足 _MIN_WINDOW 时退回全过程耗时（含 TTFB）

    窗口过短（小文件/极快链路）时首字节耗时占比过大，扣除它会把速率放大成噪声，
    此时含 TTFB 的全程口径更接近真实吞吐。
    """
    span = window if window >= _MIN_WINDOW else elapsed
    if size <= 0 or span <= 0:
        return 0.0
    return round(size * 8 / 1_000_000 / span, 3)


def _proxy_url(sess: requests.Session) -> str | None:
    """Session 的代理 URL（curl 路径复用同一地址，保证两方向走同一出口）"""
    proxies = sess.proxies or {}
    return proxies.get("https") or proxies.get("http")


def _curl_base(curl_path: str, proxy_url: str, hard_limit: float) -> list[str]:
    """curl 公共参数（下载/上传共用，两个方向同工具同口径）

    --max-time 让 hard_limit 成为真实总时限：到点 curl 断开连接，-w 仍打印已完成
    的统计（因此部分传输也能算出速率）；子进程 timeout 只兜底 curl 卡死。
    """
    return [
        curl_path, "--proxy-insecure", "-x", proxy_url, "-k", "-s",
        "--max-time", f"{hard_limit:g}",
        "-H", f"User-Agent: {_UA['User-Agent']}",
        "-H", f"Referer: {_UA['Referer']}",
        "-H", f"Origin: {_UA['Origin']}",
    ]


def _curl_stats(stdout: str, fields: int) -> list[float] | None:
    """解析 curl -w 输出的一行数值（字段不足或非数值返回 None）"""
    parts = stdout.strip().split()
    if len(parts) < fields:
        return None
    try:
        return [float(p) for p in parts[:fields]]
    except ValueError:
        return None


def _curl_err(http_code: float, returncode: int) -> str:
    """curl 未拿到 200 应答时的错误文案（http_code=0 表示无响应，附 curl 退出码）"""
    return f"HTTP {int(http_code)}" if http_code else f"无响应（curl exit {returncode}）"


def _curl_download(curl_path: str, proxy_url: str, node: DownloadNode,
                   want_bytes: int, hard_limit: float) -> dict:
    """curl 下载测速：速率取 time_starttransfer → time_total 的传输窗口"""
    url = node.url.format(n=want_bytes) if node.param_bytes else node.url
    cmd = _curl_base(curl_path, proxy_url, hard_limit) + [
        "-o", os.devnull,
        "-w", "%{http_code} %{size_download} %{time_starttransfer} %{time_total}",
        url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_limit + 5)
    except subprocess.TimeoutExpired:
        return _fail(node.name, "超时")
    except FileNotFoundError:
        return _fail(node.name, "curl 不可用")
    except Exception as e:
        return _fail(node.name, f"curl: {type(e).__name__}: {e}")

    stats = _curl_stats(p.stdout, 4)
    if stats is None:
        return _fail(node.name, "curl 无输出")
    http_code, size, starttransfer, total = stats
    if http_code != 200 or size <= 0:
        return _fail(node.name, _curl_err(http_code, p.returncode))
    return _ok(node.name, int(size),
               _transfer_mbps(int(size), total - starttransfer, total),
               round(total * 1000, 1))


def _requests_download(sess: requests.Session, node: DownloadNode,
                       want_bytes: int, hard_limit: float) -> dict:
    """requests 回退下载：稳态窗口 = 首字节 → 末字节；超时压到 hard_limit 以内

    sess.get 的 timeout 在请求建立后不可改，故以「建连/读超时 ≤ hard_limit + 每个
    chunk 后判截止」保证总耗时不显著超过 hard_limit（curl 路径用 --max-time 硬断）。
    """
    url = node.url.format(n=want_bytes) if node.param_bytes else node.url
    t0 = _now()
    deadline = t0 + hard_limit
    try:
        r = sess.get(url, timeout=(min(_CONNECT_TIMEOUT, hard_limit),
                                   min(_READ_TIMEOUT, hard_limit)),
                     stream=True, allow_redirects=True, verify=False)
        if r.status_code != 200:
            r.close()
            return _fail(node.name, f"HTTP {r.status_code}", round((_now() - t0) * 1000, 1))
        total = 0
        t_first = 0.0
        t_last = 0.0
        for chunk in r.iter_content(_CHUNK):
            if not chunk:
                break
            now = _now()
            if total == 0:
                t_first = now
            total += len(chunk)
            t_last = now
            if total >= want_bytes or now >= deadline:
                break
        r.close()
        if total == 0:
            return _fail(node.name, "空响应", round((_now() - t0) * 1000, 1))
        return _ok(node.name, total,
                   _transfer_mbps(total, t_last - t_first, _now() - t0),
                   round((_now() - t0) * 1000, 1))
    except requests.exceptions.ReadTimeout:
        return _fail(node.name, "读取超时")
    except Exception as e:
        return _fail(node.name, f"{type(e).__name__}: {e}")


def _download_once(sess: requests.Session, node: DownloadNode,
                   want_bytes: int, hard_limit: float) -> dict:
    """下载测速：优先用 curl（与上传同工具同口径），curl 不可用时回退 requests"""
    proxy_url = _proxy_url(sess)
    curl_path = shutil.which("curl")
    if proxy_url and curl_path:
        return _curl_download(curl_path, proxy_url, node, want_bytes, hard_limit)
    return _requests_download(sess, node, want_bytes, hard_limit)


def download(proxy: Proxy, nodes: list[DownloadNode], want_bytes: int,
             multi: int = 1, hard_limit: float = 30.0) -> dict:
    """各节点重复 multi 轮（取峰值），返回最优样本

    多轮是刻意重复：单次结果在慢代理上方差可达数倍，取峰值让排序稳定。
    """
    sess = _session_for(proxy)
    samples: list[dict] = []
    for node in nodes:
        for _ in range(max(1, multi)):
            samples.append(_download_once(sess, node, want_bytes, hard_limit))
    return _summarize(samples)


def _curl_upload(curl_path: str, proxy_url: str, node: UploadNode,
                 payload: bytes, hard_limit: float) -> dict:
    """curl 上传测速：速率取 time_pretransfer → time_starttransfer 的传输窗口

    requests 的 sess.post(data=bytes) 把 payload 一次性写进 socket 缓冲区（内存拷贝），
    不等数据实际发到网络，elapsed 偏短、测速偏高，故上传优先走 curl。
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(payload)
        tmp = f.name
    try:
        cmd = _curl_base(curl_path, proxy_url, hard_limit) + [
            "--data-binary", f"@{tmp}",
            "-H", "Content-Type: application/octet-stream",
            "-o", os.devnull,
            "-w", "%{http_code} %{size_upload} %{time_pretransfer} %{time_starttransfer} %{time_total}",
            node.url,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_limit + 5)
    except subprocess.TimeoutExpired:
        return _fail(node.name, "超时")
    except FileNotFoundError:
        return _fail(node.name, "curl 不可用")
    except Exception as e:
        return _fail(node.name, f"curl: {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    stats = _curl_stats(p.stdout, 5)
    if stats is None:
        return _fail(node.name, "curl 无输出")
    http_code, size, pretransfer, starttransfer, total = stats
    # 上传必须拿到服务端 200：被 hard_limit 截断的上传无法确认服务端收全，记为失败
    if http_code != 200 or size <= 0:
        return _fail(node.name, _curl_err(http_code, p.returncode))
    return _ok(node.name, int(size),
               _transfer_mbps(int(size), starttransfer - pretransfer, total),
               round(total * 1000, 1))


def _requests_upload(sess: requests.Session, node: UploadNode,
                     payload: bytes, hard_limit: float) -> dict:
    """requests 回退上传测速（socket 缓冲区导致偏高，仅 curl 不可用时用）

    超时与下载回退同样收缩到 hard_limit 以内（发送阶段由 requests 单次 post 完成，
    无法逐块判截止，这里是能施加的最强约束）。
    """
    t0 = _now()
    try:
        r = sess.post(node.url, data=payload,
                      timeout=(min(_CONNECT_TIMEOUT, hard_limit),
                               min(_READ_TIMEOUT, hard_limit)),
                      verify=False,
                      headers={**_UA, "Content-Type": "application/octet-stream"})
        elapsed = _now() - t0
        if r.status_code >= 400:
            return _fail(node.name, f"HTTP {r.status_code}", round(elapsed * 1000, 1))
        size = len(payload)
        mbps = round(size * 8 / 1_000_000 / elapsed, 3) if elapsed > 0 else 0.0
        return _ok(node.name, size, mbps, round(elapsed * 1000, 1))
    except requests.exceptions.ReadTimeout:
        return _fail(node.name, "读取超时")
    except Exception as e:
        return _fail(node.name, f"{type(e).__name__}: {e}")


def _upload_once(sess: requests.Session, node: UploadNode,
                 payload: bytes, hard_limit: float) -> dict:
    """上传测速：优先用 curl（准确测量网络发送时间），fallback requests"""
    proxy_url = _proxy_url(sess)
    curl_path = shutil.which("curl")
    if proxy_url and curl_path:
        return _curl_upload(curl_path, proxy_url, node, payload, hard_limit)
    return _requests_upload(sess, node, payload, hard_limit)


def upload(proxy: Proxy, nodes: list[UploadNode], want_bytes: int,
           multi: int = 1, hard_limit: float = 30.0) -> dict:
    """各节点重复 multi 轮（取峰值），返回最优样本"""
    sess = _session_for(proxy)
    payload = b"\x00" * want_bytes
    samples: list[dict] = []
    for node in nodes:
        for _ in range(max(1, multi)):
            samples.append(_upload_once(sess, node, payload, hard_limit))
    return _summarize(samples)


def _summarize(samples: list[dict]) -> dict:
    """汇总样本：峰值速率样本为 best，均值只统计成功样本"""
    ok_samples = [s for s in samples if s["ok"]]
    if not ok_samples:
        return {"ok": False, "best_mbps": 0.0, "avg_mbps": 0.0,
                "bytes": 0, "node": None, "ms": None, "samples": samples,
                "err": samples[0]["err"] if samples else "无节点"}
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
    # 维度被请求却没有节点时直接失败：静默跳过会产出"通过但没速度"的假结果
    if "download" in tests and not dl_nodes:
        raise ValueError("download 维度需要至少一个下载节点")
    if "upload" in tests and not ul_nodes:
        raise ValueError("upload 维度需要至少一个上传节点")

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

    if "download" in tests:
        result["download"] = download(proxy, dl_nodes, dl_bytes, multi, hard_limit)

    if "upload" in tests:
        result["upload"] = upload(proxy, ul_nodes, ul_bytes, multi, hard_limit)

    tcp_ok = result["tcp"]["connected"] if result["tcp"] else True
    dl_ok = result["download"]["ok"] if result["download"] else True
    ul_ok = result["upload"]["ok"] if result["upload"] else True
    result["ok"] = tcp_ok and dl_ok and ul_ok
    return result
