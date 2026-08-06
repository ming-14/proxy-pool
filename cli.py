"""proxy-pool 命令行薄入口：引导 sys.path 后委托 src.framework.cli.main()"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.framework.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
