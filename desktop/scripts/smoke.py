# 构建期自检：确认内嵌运行时能加载全部运行时依赖与 vendor 代码。
# 由 install-deps.mjs 用内嵌 python.exe 调用，任何失败以非零退出码暴露。
import os
import sys
from importlib.metadata import version


def main() -> int:
    # 标准库能力：cache.db（sqlite3）与配置解析（tomllib）是硬需求
    import sqlite3  # noqa: F401
    import tomllib  # noqa: F401

    import aiohttp
    import geoip2
    import requests
    import socks  # PySocks

    desktop_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyapp = os.path.join(desktop_dir, "staging", "pyapp")

    # vendor 布局：pproxy 在 vendor/ 下，pspeed 在 vendor/pspeed/src 下
    sys.path.insert(0, os.path.join(pyapp, "vendor"))
    sys.path.insert(0, os.path.join(pyapp, "vendor", "pspeed", "src"))
    import pproxy  # noqa: F401
    import pspeed  # noqa: F401

    db = os.path.join(pyapp, "vendor", "geoip", "GeoLite2-City.mmdb")
    if os.path.exists(db):
        from geoip2.database import Reader
        with Reader(db) as reader:
            reader.city("8.8.8.8")
    else:
        print("警告：未包含 GeoLite2-City.mmdb，地图功能将不可用")

    print(
        "smoke OK: python", sys.version.split()[0],
        "| aiohttp", version("aiohttp"),
        "| geoip2", version("geoip2"),
        "| requests", requests.__version__,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())