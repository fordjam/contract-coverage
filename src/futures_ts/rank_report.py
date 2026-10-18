"""Self-contained offline report for relative contract coverage."""

from __future__ import annotations

import json
from importlib.resources import files


def render_report(result: dict) -> str:
    data = json.dumps(result, default=str, allow_nan=False).replace("<", "\\u003c")
    template = files("futures_ts").joinpath("rank_report.html").read_text(encoding="utf-8")
    return template.replace("__ANALYSIS_DATA__", data)


def render_summary(result: dict) -> str:
    lines = [
        "# Futures contract coverage",
        "",
        f"Period: {result['start']} through {result['end']}. "
        f"Target: {100 * result['target']:g}% of aggregate known activity.",
        "",
        "C labels are dated expiry ranks. Spread labels mean far minus near. "
        "Selections are in-sample and conditional on known mapped activity; "
        "they do not certify exchange-wide completeness.",
    ]
    for category, metric, title in [
        ("outrights", "cleared_volume", "Outrights: reference-session cleared volume"),
        ("listed_spreads", "volume", "Listed calendars: UTC-day traded volume"),
    ]:
        lines.extend(
            [
                "",
                "## " + title,
                "",
                "| Root | Selected labels | Aggregate | Daily 5th pct | Dates meeting target |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for q in result["root_quality"]:
            row = next(
                (
                    r
                    for r in result["recommendations"]
                    if r["root"] == q["root"]
                    and r["category"] == category
                    and r["metric"] == metric
                    and r["target"] == result["target"]
                ),
                None,
            )
            if row is None:
                lines.append(f"| {q['root']} | No known positive activity | — | — | — |")
                continue
            limit = len(row["labels"]) if category == "outrights" else 12
            labels = ", ".join(row["labels"][:limit])
            if len(row["labels"]) > limit:
                labels += f" … ({row['label_count']} labels total)"
            lines.append(
                f"| {q['root']} | {labels} | {100 * row['aggregate_share']:.2f}% | "
                f"{100 * row['daily_p05_share']:.2f}% | "
                f"{100 * row['days_meeting_target_fraction']:.2f}% |"
            )
    lines.extend(
        [
            "",
            "## Settlement-history completeness",
            "",
            "Percent of observed archive sessions with a known settlement. "
            "Synthetic C3-C1 requires both outright leg settlements. "
            "Availability does not establish final/actual settlement status.",
            "",
            "| Root | C1 | C2 | C3-C1 | C3-C1 catalog dates | Latest definition |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for q in result["root_quality"]:
        cells = []
        for category, label in [
            ("outrights", "C1"),
            ("outrights", "C2"),
            ("synthetic_spreads", "C3-C1"),
        ]:
            row = next(
                (
                    r
                    for r in result["availability"]
                    if r["root"] == q["root"] and r["category"] == category and r["label"] == label
                ),
                None,
            )
            cells.append(f"{100 * row['settlement_fraction']:.2f}%" if row else "unavailable")
        dates = f"{row['eligible_dates']}/{row['expected_dates']}" if row else "unavailable"
        lines.append(f"| {q['root']} | {' | '.join(cells)} | {dates} | {q['latest_definition']} |")
    fraction = result["unknown_volume_fraction"]
    unknown = f"{100 * fraction:.3f}%" if fraction is not None else "unavailable"
    lines.extend(
        [
            "",
            f"Unmapped UTC-day volume across all supplied roots: {unknown}. "
            "Unknown IDs cannot be allocated to individual roots.",
            "",
            "Full label selections, contiguous-chain comparisons and actual symbol mappings "
            "are in the adjacent CSV/JSON files and offline report.html.",
        ]
    )
    return "\n".join(lines) + "\n"
