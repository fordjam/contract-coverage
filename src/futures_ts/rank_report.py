"""Self-contained offline report for relative contract coverage."""

from __future__ import annotations

import json
from importlib.resources import files


def render_report(result: dict) -> str:
    data = json.dumps(result, default=str, allow_nan=False).replace("<", "\\u003c")
    template = files("futures_ts").joinpath("rank_report.html").read_text(encoding="utf-8")
    return template.replace("__ANALYSIS_DATA__", data)
