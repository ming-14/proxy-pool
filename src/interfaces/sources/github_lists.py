"""GitHub 静态代理列表源（多仓库纯文本列表，按协议分组）

这些仓库只提供纯文本列表（每行一个代理），没有 base_url/params 语义，故不套用
HttpSourceFetcher，只复用其重试策略 retry_fetch。一次拉取多条列表并合并，由拉取
用例按 scheme 分拣入 github-http / github-https / github-socks5 组。

列表准入口径（每条都经抽样存活检测 + 实链路复测）：
    - 抽样存活率 >= 10%；条目数不设硬阈值——小而准的列表（32~115 条）同样接入
    - 协议必须可判定：路径（目录/文件名）给出唯一协议，或全部条目自带 scheme
      由 parse_line 逐行归组——裸 ip:port 混合的 all 类列表一律不收，避免错分组
    - socks4 不在 SUPPORTED_SCHEMES 内（工程只做入站），不收
    - 内容 md5 相同的镜像仓库只留一处

raw.githubusercontent.com 在部分网络直连被 SSL 中断，镜像前缀由环境变量
GITHUB_LISTS_MIRROR 提供（未设置或为空即直连，代码不内嵌任何镜像地址）。
单条列表失败只记日志并跳过，不影响其它列表——与拉取用例「单源失败不中断」同一口径。
"""

from __future__ import annotations

import logging
import os

from src.domain.ports import ProxySource
from src.interfaces import http_client
from src.interfaces.sources.base import parse_line, retry_fetch

logger = logging.getLogger(__name__)

MIRROR_ENV = "GITHUB_LISTS_MIRROR"
_RAW_ROOT = "https://raw.githubusercontent.com/"

# (raw 路径, 列表协议)：条目自带 scheme 时以条目为准，裸 ip:port 用列表协议补全。
# 协议取自文件路径（目录/文件名），与各仓库自身的分文件口径一致；分组计数见各段注释。
LISTS: tuple[tuple[str, str], ...] = (
    # ---- http (59) ----
    (f"{_RAW_ROOT}398001/ProxysList/main/all_working.txt", "http"),
    (f"{_RAW_ROOT}6yyn/free-proxy-list/main/free_http_proxies.txt", "http"),
    (f"{_RAW_ROOT}ALIILAPRO/Proxy/main/http.txt", "http"),
    (f"{_RAW_ROOT}Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt", "http"),
    (f"{_RAW_ROOT}anutmagang/Free-HighQuality-Proxy-Socks/main/results/http.txt", "http"),
    (f"{_RAW_ROOT}azestkingscrown/Free_Proxy_List/main/working_proxies.txt", "http"),
    (f"{_RAW_ROOT}b4mbo-o/Check-Free-Proxy/main/alive_http.txt", "http"),
    (f"{_RAW_ROOT}B4RC0DE-TM/proxy-list/main/HTTP.txt", "http"),
    (f"{_RAW_ROOT}Bes-js/public-proxy-list/main/proxies.txt", "http"),
    (f"{_RAW_ROOT}BlacKSnowDot0/Proxy-Pulse/main/all.txt", "http"),
    (f"{_RAW_ROOT}Ch4120N/Ch4120N-Proxy-List/master/proxies/all.txt", "http"),
    (f"{_RAW_ROOT}Ciara-12/PROXY-free/main/http.txt", "http"),
    (f"{_RAW_ROOT}Denisyoya/Proxy-List-Gamt/main/proxy/http.txt", "http"),
    (f"{_RAW_ROOT}dpangestuw/Free-Proxy/main/All_proxies.txt", "http"),
    (f"{_RAW_ROOT}ebrasha/abdal-proxy-hub/main/http-proxy-list-by-EbraSha.txt", "http"),
    (f"{_RAW_ROOT}elliottophellia/proxylist/master/results/pmix_checked.txt", "http"),
    (f"{_RAW_ROOT}ErcinDedeoglu/proxies/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}fmw86/free-proxy-pool/main/http_fast.txt", "http"),
    (f"{_RAW_ROOT}hproxy-com/free-proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}i-am-unbekannt/BLITZPROXY/main/out-files/http.txt", "http"),
    (f"{_RAW_ROOT}iplocate/free-proxy-list/main/all-proxies.txt", "http"),
    (f"{_RAW_ROOT}IPParrot/proxy_ips/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}komutan234/Proxy-List-Free/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}krokmazagaga/http-proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}litportnet/free-proxy-list/main/proxies/all.txt", "http"),
    (f"{_RAW_ROOT}lucasjustinudin/Free-HighQuality-Proxy-Socks/main/output/by_protocol/http.txt", "http"),
    (f"{_RAW_ROOT}maximilianfeix/free-proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}mmpx12/proxy-list/master/http.txt", "http"),
    (f"{_RAW_ROOT}Moleway/Free-Proxy-List/main/all.txt", "http"),
    (f"{_RAW_ROOT}monosans/proxy-list/main/proxies/all.txt", "http"),
    (f"{_RAW_ROOT}monosans/proxy-list/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}mzyui/proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}NikolaiT/free-proxy-list/main/proxies/http_working.txt", "http"),
    (f"{_RAW_ROOT}officialputuid/KangProxy/main/xResults/RAW.txt", "http"),
    (f"{_RAW_ROOT}proxifly/free-proxy-list/main/proxies/protocols/http/data.txt", "http"),
    (f"{_RAW_ROOT}proxio-io/proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}proxmint/free-proxy-list/main/proxies/all.txt", "http"),
    (f"{_RAW_ROOT}proxy-free/free-proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}proxylist-to/proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}ProxyScrape/free-proxy-list/main/proxies/protocols/http/data.txt", "http"),
    (f"{_RAW_ROOT}pwnx0/proxy/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}Pxys-io/DailyProxyList/master/working_proxies.txt", "http"),
    (f"{_RAW_ROOT}rdavydov/proxy-list/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}relayglass/free-proxy-list/main/protocol/http/http.txt", "http"),
    (f"{_RAW_ROOT}rianilham28/proxian/main/out/proxies.txt", "http"),
    (f"{_RAW_ROOT}RioMMO/ProxyFree/main/HTTP.txt", "http"),
    (f"{_RAW_ROOT}Riyoway/Proxies/master/all.txt", "http"),
    (f"{_RAW_ROOT}ShiftyTR/Proxy-List/master/http.txt", "http"),
    (f"{_RAW_ROOT}stormsia/proxy-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}sunny9577/proxy-scraper/master/generated/http_proxies.txt", "http"),
    (f"{_RAW_ROOT}TheSpeedX/PROXY-List/master/http.txt", "http"),
    (f"{_RAW_ROOT}trio666/proxy-checker/main/all.txt", "http"),
    (f"{_RAW_ROOT}vakhov/fresh-proxy-list/master/http.txt", "http"),
    (f"{_RAW_ROOT}Vann-Dev/proxy-list/main/proxies/http.txt", "http"),
    (f"{_RAW_ROOT}vmheaven/VMHeaven.io-Free-Proxy-List/main/http.txt", "http"),
    (f"{_RAW_ROOT}VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt", "http"),
    (f"{_RAW_ROOT}xyzs996/free-proxy-health-list/main/http.txt", "http"),
    (f"{_RAW_ROOT}z3a4/free-proxy-list/main/proxies/unchecked/http-proxies.txt", "http"),
    (f"{_RAW_ROOT}Zaeem20/FREE_PROXIES_LIST/master/http.txt", "http"),

    # ---- https (11) ----
    (f"{_RAW_ROOT}ErcinDedeoglu/proxies/main/proxies/https.txt", "https"),
    (f"{_RAW_ROOT}hproxy-com/free-proxy-list/main/https.txt", "https"),
    (f"{_RAW_ROOT}maximilianfeix/free-proxy-list/main/https.txt", "https"),
    (f"{_RAW_ROOT}mmpx12/proxy-list/master/https.txt", "https"),
    (f"{_RAW_ROOT}proxifly/free-proxy-list/main/proxies/protocols/https/data.txt", "https"),
    (f"{_RAW_ROOT}ProxyScrape/free-proxy-list/main/proxies/protocols/https/data.txt", "https"),
    (f"{_RAW_ROOT}roosterkid/openproxylist/main/HTTPS_RAW.txt", "https"),
    (f"{_RAW_ROOT}Thordata/awesome-free-proxy-list/main/proxies/https.txt", "https"),
    (f"{_RAW_ROOT}vmheaven/VMHeaven.io-Free-Proxy-List/main/https.txt", "https"),
    (f"{_RAW_ROOT}xyzs996/free-proxy-health-list/main/https.txt", "https"),
    (f"{_RAW_ROOT}Zaeem20/FREE_PROXIES_LIST/master/https.txt", "https"),

    # ---- socks5 (26) ----
    (f"{_RAW_ROOT}ALIILAPRO/Proxy/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt", "socks5"),
    (f"{_RAW_ROOT}databay-labs/free-proxy-list/master/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}dpangestuw/Free-Proxy/main/socks5_proxies.txt", "socks5"),
    (f"{_RAW_ROOT}elliottophellia/proxylist/master/results/socks5/global/socks5_checked.txt", "socks5"),
    (f"{_RAW_ROOT}ErcinDedeoglu/proxies/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}gnxD3RfTT2WE/proxy-list-plain-text/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}HankNovic/ProxyClean/main/SOCKS5.txt", "socks5"),
    (f"{_RAW_ROOT}hookzof/socks5_list/master/proxy.txt", "socks5"),
    (f"{_RAW_ROOT}hproxy-com/free-proxy-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}iplocate/free-proxy-list/main/protocols/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}litportnet/free-proxy-list/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}maximilianfeix/free-proxy-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}mmpx12/proxy-list/master/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}mzyui/proxy-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}proxio-io/proxy-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}proxmint/free-proxy-list/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}proxy-free/free-proxy-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}ProxyScrape/free-proxy-list/main/proxies/protocols/socks5/data.txt", "socks5"),
    (f"{_RAW_ROOT}rdavydov/proxy-list/main/proxies/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt", "socks5"),
    (f"{_RAW_ROOT}xyzs996/free-proxy-health-list/main/socks5.txt", "socks5"),
    (f"{_RAW_ROOT}z3a4/free-proxy-list/main/proxies/unchecked/socks5-proxies.txt", "socks5"),
    (f"{_RAW_ROOT}Zaeem20/FREE_PROXIES_LIST/master/socks5.txt", "socks5"),
)

def _mirror_prefix() -> str:
    """镜像前缀；未设置或为空串时直连"""
    return (os.environ.get(MIRROR_ENV) or "").strip()


class GithubListsFetcher:
    """多仓库静态列表代理源（ProxySource 端口实现）"""

    RETRIES = 2
    TIMEOUT = 15

    def __init__(self, *, lists=LISTS, mirror: str | None = None,
                 retries: int | None = None, timeout: int | None = None) -> None:
        """mirror=None 时读环境变量；其余参数缺省用类属性"""
        self._lists = tuple(lists)
        self._mirror = _mirror_prefix() if mirror is None else mirror
        self.retries = self.RETRIES if retries is None else retries
        self.timeout = self.TIMEOUT if timeout is None else timeout

    def fetch_proxies(self, retries=None, timeout=None) -> list[str]:
        """逐列表拉取并合并（跨列表去重），返回带 scheme 前缀的代理 URL 列表"""
        max_retries = self.retries if retries is None else retries
        req_timeout = self.timeout if timeout is None else timeout
        merged: dict[str, None] = {}
        for path, protocol in self._lists:
            for proxy in self._fetch_one(self._mirror + path, protocol, max_retries, req_timeout):
                merged.setdefault(proxy, None)
        logger.info("GitHub 列表源共获取 %d 个代理（%d 个列表）", len(merged), len(self._lists))
        return list(merged)

    def _fetch_one(self, url: str, protocol: str, retries: int, timeout: int) -> list[str]:
        """拉取单条列表并解析；重试直到拿到非空结果，全失败返回空列表"""
        def attempt() -> list[str]:
            resp = http_client.get(url, timeout=timeout)
            resp.raise_for_status()
            return [p for p in (parse_line(line, protocol) for line in resp.text.splitlines()) if p]

        proxies = retry_fetch(attempt, retries=retries, label=url)
        if proxies:
            logger.info("列表 %s 获取 %d 个代理 (%s)", url, len(proxies), protocol)
        return proxies or []


def create_source(config=None) -> ProxySource:
    """加载器入口：返回本源实例（config 为组合根注入的通用参数，本源无专属配置，忽略）"""
    return GithubListsFetcher()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for item in GithubListsFetcher().fetch_proxies():
        print(item)
