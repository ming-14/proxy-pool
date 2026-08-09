"""快捷入口：无需安装，直接 python cli.py 即可使用全部功能。"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from pspeed.cli import main

if __name__ == "__main__":
    sys.exit(main())
