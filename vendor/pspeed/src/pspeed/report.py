"""JSON 结构化输出（子进程友好）。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from pspeed import __version__


def build_report(results: list[dict]) -> dict:
    ok = sum(1 for r in results if r.get("ok"))
    return {
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {"total": len(results), "ok": ok, "fail": len(results) - ok},
        "results": results,
    }


def emit(report: dict, pretty: bool = False, stream=None) -> str:
    stream = stream or sys.stdout
    text = json.dumps(report, ensure_ascii=False,
                      indent=2 if pretty else None,
                      separators=(",", ":") if not pretty else None)
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    return text
