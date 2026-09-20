"""内置测速节点清单：下载 / 上传 / 回源验证IP 三类。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadNode:
    name: str
    region: str
    url: str
    param_bytes: bool = False   # url 含 {n} 占位符时置 True，按目标字节数请求


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
    DownloadNode("fastly", "全球CDN", "https://fastly.com/0MB"),
    DownloadNode("jsdelivr", "全球CDN", "https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"),
    DownloadNode("github-raw", "全球", "https://raw.githubusercontent.com/nicolargo/bench/master/README.md"),
    DownloadNode("aliyun", "中国", "https://mirrors.aliyun.com/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("tsinghua", "中国", "https://mirrors.tuna.tsinghua.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("huawei", "中国", "https://mirrors.huaweicloud.com/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("ustc", "中国", "https://mirrors.ustc.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("nju", "中国", "https://mirrors.nju.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("sjtu", "中国", "https://mirror.sjtu.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("zju", "中国", "https://mirrors.zju.edu.cn/centos-stream/9-stream/BaseOS/x86_64/os/images/pxeboot/initrd.img"),
    DownloadNode("hetzner-de", "欧洲", "https://speed.hetzner.de/10MB.bin"),
    DownloadNode("hetzner-us", "北美", "https://speed.hetzner.com/10MB.bin"),
    DownloadNode("hetzner-cloud", "欧洲", "https://speed.hetzner.cloud/10MB.bin"),
    DownloadNode("linode-tokyo", "亚洲", "http://speedtest.tokyo.linode.com/10MB-tokyo.bin"),
    DownloadNode("linode-singapore", "亚洲", "http://speedtest.singapore.linode.com/10MB-singapore.bin"),
    DownloadNode("linode-frankfurt", "欧洲", "http://speedtest.frankfurt.linode.com/10MB-frankfurt.bin"),
    DownloadNode("linode-london", "欧洲", "http://speedtest.london.linode.com/10MB-london.bin"),
    DownloadNode("linode-newark", "北美", "http://speedtest.newark.linode.com/10MB-newark.bin"),
    DownloadNode("cachefly", "全球CDN", "http://cachefly.cachefly.net/10mb.test"),
    DownloadNode("ovh-fr", "欧洲", "http://proof.ovh.net/files/10Mio.dat"),
    DownloadNode("thinkbroadband-uk", "欧洲", "http://ipv4.download.thinkbroadband.com/10MB.zip"),
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


def _select(nodes: list, names: list[str] | None) -> list:
    """按名称选节点；未知名称直接报错——静默跳过会让本次探测退化成"通过但没速度"

    唯一例外是 names 为空或显式 "all"：这是"用全部节点"的语义，不是拼写错误。
    """
    if not names or names == ["all"]:
        return list(nodes)
    pool = {n.name: n for n in nodes}
    unknown = [n for n in names if n not in pool]
    if unknown:
        raise ValueError(f"未知节点: {', '.join(unknown)}（可用: {', '.join(pool)}）")
    return [pool[n] for n in names]


def get_download_nodes(names: list[str] | None) -> list[DownloadNode]:
    return _select(DOWNLOAD_NODES, names)


def get_upload_nodes(names: list[str] | None) -> list[UploadNode]:
    return _select(UPLOAD_NODES, names)
