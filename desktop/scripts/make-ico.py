"""生成应用图标（ICO）。

只内置 256×256 一张，由 Windows 按显示尺寸自行缩放。两条 Windows 侧的事实：
- ICO 条目尺寸上限是 256，内嵌不了 512；窗口/托盘不走这里，用 assets/icon.png 原图。
- 若日后要往 ICO 里补小尺寸条目，必须存成 BMP(DIB) 载荷：小尺寸用 PNG 载荷会让
  Shell 连整张图标都渲染不出来，直接回退成系统默认图标（实测过）。
"""

import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

ICON_SIZE = 256


def build_ico(src: Path, dst: Path) -> None:
    base = Image.open(src).convert("RGBA")
    if base.width != base.height:
        raise ValueError(f"源图不是正方形: {base.size}")

    frame = base.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    buf = BytesIO()
    frame.save(buf, format="PNG", optimize=True)
    payload = buf.getvalue()

    # ICONDIR + 单个 ICONDIRENTRY + 载荷；256 在条目里按 0 记录，正好等于 size & 0xFF
    entry = struct.pack(
        "<BBBBHHII",
        ICON_SIZE & 0xFF,
        ICON_SIZE & 0xFF,
        0,  # 调色板色数，真彩固定 0
        0,
        1,  # 色彩平面数
        32,  # 位深
        len(payload),
        6 + 16,  # 载荷偏移 = ICONDIR(6) + 一个条目(16)
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + payload)


def describe_ico(path: Path) -> None:
    """校验产物：确认唯一条目的尺寸与载荷格式。"""
    data = path.read_bytes()
    reserved, ico_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or ico_type != 1:
        raise ValueError(f"不是合法 ICO: {path}")
    print(f"{path}  条目数={count}")
    for i in range(count):
        w, h, _colors, _res, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        head = data[offset:offset + 8]
        kind = "PNG" if head == b"\x89PNG\r\n\x1a\n" else f"?{head[:4].hex()}"
        print(f"  [{i}] {w or 256}x{h or 256} planes={planes} bpp={bpp} bytes={size} fmt={kind}")


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    build_ico(src, dst)
    describe_ico(dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())