"""Local CSV/Parquet adapters and reviewable, versioned policy reports."""

from __future__ import annotations

import hashlib
import json
import platform
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import polars as pl

from .engine import AnalysisResult
from .schema import GROUP, normalize


def scan_files(paths: list[Path], mapping: dict | None = None) -> pl.LazyFrame:
    """Read a homogeneous export or normalized partitions; no vendor SDK is needed."""
    if not paths:
        raise ValueError("At least one input file is required")
    suffixes = {path.suffix for path in paths}
    # One scan node avoids a universe-sized concat plan for every filtered group.
    if suffixes == {".parquet"}:
        by_schema = defaultdict(list)
        for path in paths:
            by_schema[tuple(pl.read_parquet_schema(path).items())].append(path)
        # Null-only optional columns are common in separate outright/spread exports.
        return pl.concat(
            [
                normalize(pl.scan_parquet(parts, hive_partitioning=False), mapping)
                for parts in by_schema.values()
            ]
        )
    if suffixes == {".csv"}:
        return normalize(pl.scan_csv(paths, infer_schema=False, try_parse_dates=False), mapping)
    queries = []
    for path in paths:
        if path.suffix == ".parquet":
            queries.append(pl.scan_parquet(path, hive_partitioning=False))
        elif path.suffix == ".csv":
            queries.append(pl.scan_csv(path, infer_schema=False, try_parse_dates=False))
        else:
            raise ValueError("Inputs must be CSV or Parquet files")
    return pl.concat([normalize(query, mapping) for query in queries])


def fingerprint(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        checksum = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
        rows.append(
            {
                "path": str(path.resolve()),
                "bytes": path.stat().st_size,
                "sha256": checksum.hexdigest(),
            }
        )
    return rows


def write_report(results: Iterable[AnalysisResult], out: Path, provenance: dict) -> dict:
    """Write each group's large tables immediately; the final manifest signals success."""
    out.mkdir(parents=True, exist_ok=False)
    summaries = []
    for index, result in enumerate(results, 1):
        directory = out / f"group-{index:06d}"
        directory.mkdir()
        for name in ("rules", "curve", "daily"):
            getattr(result, name).write_csv(directory / f"{name}.csv")
        summary = {**result.summary, "directory": directory.name}
        summaries.append(summary)
    if not summaries:
        raise ValueError("No groups to report")
    pl.DataFrame(summaries, infer_schema_length=None).write_csv(out / "summary.csv")
    totals = []
    # A sum of independent capacities, never a pooled volume or joint-cost optimum.
    for category in sorted({row["category"] for row in summaries}):
        rows = [row for row in summaries if row["category"] == category]
        if all(row["optimality"] == "frozen_policy_evaluation" for row in rows):
            totals.append(
                {
                    "category": category,
                    "groups": len(rows),
                    "groups_meeting_target": sum(row["target_met"] for row in rows),
                    "sum_of_policy_capacities": sum(row["policy_capacity"] for row in rows),
                }
            )
            continue
        resolved = [row for row in rows if row["minimum_capacity"] is not None]
        totals.append(
            {
                "category": category,
                "groups": len(rows),
                "resolved_groups": len(resolved),
                "sum_of_resolved_per_group_capacities": sum(
                    row["minimum_capacity"] for row in resolved
                ),
                "complete_total": len(resolved) == len(rows),
            }
        )
    report = {
        "format": "product-coverage",
        "version": 1,
        "provenance": provenance,
        "runtime": {"python": platform.python_version(), "polars": pl.__version__},
        "scope": (
            "Independent equal-slot per venue/family/category; aggregate known volume; "
            "in-sample unless frozen-policy evaluation. No joint spread-leg cost "
            "optimization or future guarantee."
        ),
        "totals": totals,
        "groups": summaries,
    }
    lines = [
        "# Minimum product coverage",
        "",
        report["scope"],
        "",
        "Capacity means a fixed concurrent slot count for monthly-calendar and daily-oracle; "
        "it means a fixed subset count for fixed-products/fixed-patterns. Daily-oracle uses "
        "hindsight. Full-universe coverage requires a complete input assertion and no "
        "missing volume.",
        "",
        "| Venue | Family | Category | Method | Capacity | Known capture | "
        "Days at target | Missing volume rows |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in summaries:
        capture = (
            f"{row['capture_fraction']:.4%}" if row["capture_fraction"] is not None else "unknown"
        )
        fields = [
            *[row[c] for c in GROUP],
            row["method"],
            row["minimum_capacity"]
            if row["minimum_capacity"] is not None
            else f"{row['policy_capacity']} (frozen)"
            if "policy_capacity" in row
            else "unresolved",
            capture,
            f"{row['days_meeting_target']}/{row['positive_volume_dates']}",
            row["missing_volume_rows"],
        ]
        lines.append(
            "| "
            + " | ".join(str(field).replace("|", "\\|").replace("\n", " ") for field in fields)
            + " |"
        )
    lines += [
        "",
        "See summary.csv and group-*/rules.csv, curve.csv, daily.csv for exact thresholds, "
        "chosen rules and daily shortfalls.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    pending = out / "report.json.partial"
    pending.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    pending.replace(out / "report.json")
    return report


def read_policy(path: Path) -> list[AnalysisResult]:
    value = json.loads((path / "report.json").read_text())
    if value.get("format") != "product-coverage" or value.get("version") != 1:
        raise ValueError("Unsupported product-coverage policy")
    results = []
    seen = set()
    for summary in value["groups"]:
        directory = summary["directory"]
        if directory != f"group-{len(results) + 1:06d}":
            raise ValueError("Invalid policy group path")
        key = tuple(summary[c] for c in GROUP)
        if key in seen:
            raise ValueError("Duplicate policy group")
        seen.add(key)
        rules = pl.read_csv(path / directory / "rules.csv", infer_schema=False)
        if rules.select("state", "candidate").is_duplicated().any():
            raise ValueError("Duplicate policy rule")
        results.append(AnalysisResult(summary, rules, pl.DataFrame(), pl.DataFrame()))
    if not results:
        raise ValueError("Empty policy")
    return results
