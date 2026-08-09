"""User-Agent 生成器：基于 fake-useragent，按 seed 确定性生成请求头

供代理源适配器构建浏览器化请求头使用。
"""

import hashlib
import random
import re
from fake_useragent import UserAgent


# ---------------------------------------------------------------- UA 字符串 → Client Hints 解析
# Sec-Ch-Ua-Platform 标准值：Windows / macOS / Linux / Android / iOS / Chrome OS
# 依据 UA 字符串线索推导，不依赖 fake_useragent 内部数据结构（更可靠）。
_MOBILE_KEYWORDS = ("iPhone", "iPad", "Android")


def _parse_platform(ua_str: str) -> str | None:
    """从 UA 字符串推导 Sec-Ch-Ua-Platform 标准值

    Client Hints 平台标准值：Windows / macOS / Linux / Android / iOS / Chrome OS。
    无法识别返回 None（调用方应跳过该 Client Hint 头）。
    """
    if "Windows NT" in ua_str:
        return '"Windows"'
    if "iPhone" in ua_str or "iPad" in ua_str:
        return '"iOS"'
    if "Android" in ua_str:
        return '"Android"'
    if "CrOS" in ua_str:
        return '"Chrome OS"'
    if "Mac OS X" in ua_str or "Macintosh" in ua_str:
        return '"macOS"'
    if "Linux" in ua_str:
        return '"Linux"'
    return None


def _is_mobile_ua(ua_str: str) -> bool:
    """从 UA 字符串判断是否移动设备（iPhone/iPad/Android）"""
    return any(k in ua_str for k in _MOBILE_KEYWORDS)


class UAGenerator:
    def __init__(self):
        self._ua = UserAgent()
        self._ua_list = [item["useragent"] for item in self._ua.data_browsers]
        self._cache = {}

    @staticmethod
    def _seed_to_int(seed):
        return int(hashlib.md5(str(seed).encode()).hexdigest(), 16)

    def get(self, seed):
        if seed not in self._cache:
            rng = random.Random(self._seed_to_int(seed))
            ua_str = rng.choice(self._ua_list)
            browser = (
                "Chrome" if "Chrome" in ua_str
                else "Firefox" if "Firefox" in ua_str
                else "Safari" if "Safari" in ua_str
                else "Other"
            )
            if browser == "Chrome":
                m = re.search(r"Chrome/(\d+)", ua_str)
                ver = m.group(1) if m else "120"
                sec_ch_ua = f'"Chromium";v="{ver}", "Not.A/Brand";v="8", "Google Chrome";v="{ver}"'
                self._cache[seed] = {
                    "User-Agent": ua_str,
                    "Sec-Ch-Ua": sec_ch_ua,
                    "Sec-Ch-Ua-Mobile": "?1" if _is_mobile_ua(ua_str) else "?0",
                    "Sec-Ch-Ua-Platform": _parse_platform(ua_str),
                }
            else:
                self._cache[seed] = {
                    "User-Agent": ua_str,
                    "Sec-Ch-Ua": None,
                    "Sec-Ch-Ua-Mobile": None,
                    "Sec-Ch-Ua-Platform": None,
                }
        return self._cache[seed]

    def get_headers(self, seed):
        ua_info = self.get(seed)
        headers = {"User-Agent": ua_info["User-Agent"]}
        if ua_info["Sec-Ch-Ua"]:
            headers["Sec-Ch-Ua"] = ua_info["Sec-Ch-Ua"]
            headers["Sec-Ch-Ua-Mobile"] = ua_info["Sec-Ch-Ua-Mobile"]
            headers["Sec-Ch-Ua-Platform"] = ua_info["Sec-Ch-Ua-Platform"]
        return headers
