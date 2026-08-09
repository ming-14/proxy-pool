"""内置测速节点清单：下载 / 上传 / 回源验证IP 三类。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadNode:
    name: str
    region: str
    url: str
    param_bytes: bool = False
    max_bytes: int = 0


@dataclass(frozen=True)
class UploadNode:
    name: str
    region: str
    url: str


@dataclass(frozen=True)
class IpNode:
    name: str
    url: str


DOWNLOAD_NODES: list[DownloadNode] = [
    DownloadNode("cloudflare", "全球CDN", "https://speed.cloudflare.com/__down?bytes={n}", True),
    DownloadNode("fastly", "全球CDN", "https://fastly.com/0MB", False, 0),
    DownloadNode("jsdelivr", "全球CDN", "https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js", False, 0),
    DownloadNode("github-raw", "全球", "https://raw.githubusercontent.com/nicolargo/bench/master/README.md", False, 0),
    DownloadNode("aliyun", "中国", "https://mirrors.aliyun.com/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("tsinghua", "中国", "https://mirrors.tuna.tsinghua.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("huawei", "中国", "https://mirrors.huaweicloud.com/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("ustc", "中国", "https://mirrors.ustc.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("nju", "中国", "https://mirrors.nju.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("sjtu", "中国", "https://mirror.sjtu.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("zju", "中国", "https://mirrors.zju.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img", False, 0),
    DownloadNode("hetzner-de", "欧洲", "https://speed.hetzner.de/10MB.bin", False, 0),
    DownloadNode("hetzner-us", "北美", "https://speed.hetzner.com/10MB.bin", False, 0),
    DownloadNode("hetzner-cloud", "欧洲", "https://speed.hetzner.cloud/10MB.bin", False, 0),
    DownloadNode("linode-tokyo", "亚洲", "http://speedtest.tokyo.linode.com/10MB-tokyo.bin", False, 0),
    DownloadNode("linode-singapore", "亚洲", "http://speedtest.singapore.linode.com/10MB-singapore.bin", False, 0),
    DownloadNode("linode-frankfurt", "欧洲", "http://speedtest.frankfurt.linode.com/10MB-frankfurt.bin", False, 0),
    DownloadNode("linode-london", "欧洲", "http://speedtest.london.linode.com/10MB-london.bin", False, 0),
    DownloadNode("linode-newark", "北美", "http://speedtest.newark.linode.com/10MB-newark.bin", False, 0),
    DownloadNode("cachefly", "全球CDN", "http://cachefly.cachefly.net/10mb.test", False, 0),
    DownloadNode("ovh-fr", "欧洲", "http://proof.ovh.net/files/10Mio.dat", False, 0),
    DownloadNode("thinkbroadband-uk", "欧洲", "http://ipv4.download.thinkbroadband.com/10MB.zip", False, 0),
]

UPLOAD_NODES: list[UploadNode] = [
    UploadNode("cloudflare", "全球CDN", "https://speed.cloudflare.com/__up"),
    UploadNode("httpbin", "全球", "https://httpbin.org/post"),
    UploadNode("postman-echo", "全球", "https://postman-echo.com/post"),
]

IP_NODES: list[IpNode] = [
    IpNode("ipify", "https://api.ipify.org?format=json"),
    IpNode("ipinfo", "https://ipinfo.io/json"),
    IpNode("ifconfig", "https://ifconfig.me/all.json"),
]


def get_download_nodes(names: list[str] | None) -> list[DownloadNode]:
    if not names or names == ["all"]:
        return list(DOWNLOAD_NODES)
    pool = {n.name: n for n in DOWNLOAD_NODES}
    return [pool[n] for n in names if n in pool]


def get_upload_nodes(names: list[str] | None) -> list[UploadNode]:
    if not names or names == ["all"]:
        return list(UPLOAD_NODES)
    pool = {n.name: n for n in UPLOAD_NODES}
    return [pool[n] for n in names if n in pool]
