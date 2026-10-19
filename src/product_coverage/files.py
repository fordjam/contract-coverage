"""Local CSV/Parquet adapters and reviewable, versioned policy reports."""

from __future__ import annotations

import hashlib
import glob
import json
import platform
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import polars as pl

from .engine import AnalysisResult
from .schema import GROUP, normalize
from .policy import integer, validate_policy
from .presentation import safe_frame


def resolve_inputs(paths: list[Path]) -> list[Path]:
    """Resolve explicit files, literal shell globs and dataset directories once."""
    resolved = set()
    for path in paths:
        if path.is_file():
            matches = [path]
        elif path.is_dir():
            matches = [
                p for p in path.rglob("*") if p.suffix in {".csv", ".parquet"} and p.is_file()
            ]
        else:
            matches = [Path(p) for p in glob.glob(str(path), recursive=True) if Path(p).is_file()]
        if not matches:
            raise ValueError(f"Input pattern or directory matched no files: {path}")
        for match in matches:
            if match.suffix not in {".csv", ".parquet"}:
                raise ValueError("Inputs must be CSV or Parquet files")
            resolved.add(match.resolve())
    if not resolved:
        raise ValueError("At least one input file is required")
    return sorted(resolved)


def scan_files(paths: list[Path], mapping: dict | None = None) -> pl.LazyFrame:
    """Read a homogeneous export or normalized partitions; no vendor SDK is needed."""
    paths = resolve_inputs(paths)
    suffixes = {path.suffix for path in paths}
    # One scan node avoids a universe-sized concat plan for every filtered group.
    if suffixes == {".parquet"}:
        by_schema = defaultdict(list)
        for path in paths:
            by_schema[
                tuple(
                    pl.scan_parquet(path, glob=False, hive_partitioning=False)
                    .collect_schema()
                    .items()
                )
            ].append(path)
        # Null-only optional columns are common in separate outright/spread exports.
        return pl.concat(
            [
                normalize(pl.scan_parquet(parts, glob=False, hive_partitioning=False), mapping)
                for parts in by_schema.values()
            ]
        )
    if suffixes == {".csv"}:
        return normalize(
            pl.scan_csv(paths, glob=False, infer_schema=False, try_parse_dates=False), mapping
        )
    queries = []
    for path in paths:
        if path.suffix == ".parquet":
            queries.append(pl.scan_parquet(path, glob=False, hive_partitioning=False))
        elif path.suffix == ".csv":
            queries.append(pl.scan_csv(path, glob=False, infer_schema=False, try_parse_dates=False))
        else:
            raise ValueError("Inputs must be CSV or Parquet files")
    return pl.concat([normalize(query, mapping) for query in queries])


def file_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def fingerprint(paths: list[Path], *, include_paths: bool = False) -> list[dict]:
    rows = []
    for index, path in enumerate(paths, 1):
        rows.append(
            {
                **(
                    {"path": str(path.resolve())}
                    if include_paths
                    else {"input": f"input-{index:06d}", "format": path.suffix}
                ),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
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
            safe_frame(getattr(result, name)).write_csv(directory / f"{name}.csv")
        rule_file = directory / "rules.json"
        # Stream exact identities separately from spreadsheet presentation escaping.
        with rule_file.open("w", encoding="utf-8") as handle:
            training = {
                k: v
                for k, v in result.summary.items()
                if k not in {"directory", "rules_file", "rules_sha256"}
            }
            header = {"format": "product-coverage-rules", "version": 1, "summary": training}
            handle.write(json.dumps(header, sort_keys=True, allow_nan=False)[:-1] + ',"rules":[')
            for row_index, row in enumerate(result.rules.iter_rows(named=True)):
                if row_index:
                    handle.write(",")
                handle.write(json.dumps(row, sort_keys=True, allow_nan=False))
            handle.write("]}\n")
        summary = {
            **training,
            "directory": directory.name,
            "rules_file": "rules.json",
            "rules_sha256": file_sha256(rule_file),
        }
        summaries.append(summary)
    if not summaries:
        raise ValueError("No groups to report")
    safe_frame(pl.DataFrame(summaries, infer_schema_length=None)).write_csv(out / "summary.csv")
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
        "version": 2,
        "csv_encoding": "spreadsheet-text-v1; exact policy identities are in rules.json",
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
        "CSV cells use spreadsheet text escaping; use checksummed rules.json for exact identities.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pending = out / "report.json.partial"
    pending.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pending.replace(out / "report.json")
    return report


def load_json(path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON field: {key}")
            value[key] = item
        return value

    def invalid(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=invalid
    )
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def read_policy(path: Path) -> list[AnalysisResult]:
    value = load_json(path / "report.json")
    if (
        value.get("format") != "product-coverage"
        or type(value.get("version")) is not int
        or value["version"] != 2
    ):
        raise ValueError("Unsupported policy; regenerate legacy v1 reports with analyze")
    results = []
    seen = set()
    if not isinstance(value.get("groups"), list):
        raise ValueError("Invalid policy groups")
    for summary in value["groups"]:
        if not isinstance(summary, dict):
            raise ValueError("Invalid policy summary")
        directory = summary["directory"]
        if directory != f"group-{len(results) + 1:06d}":
            raise ValueError("Invalid policy group path")
        key = tuple(summary[c] for c in GROUP)
        if key in seen:
            raise ValueError("Duplicate policy group")
        seen.add(key)
        if summary.get("rules_file") != "rules.json":
            raise ValueError("Invalid policy rules path")
        rule_file = path / directory / "rules.json"
        if (path / directory).is_symlink() or rule_file.is_symlink():
            raise ValueError("Symlinked policy groups or rules are unsupported")
        if file_sha256(rule_file) != summary.get("rules_sha256"):
            raise ValueError("Policy rules checksum mismatch")
        payload = load_json(rule_file)
        binding = {
            k: v for k, v in summary.items() if k not in {"directory", "rules_file", "rules_sha256"}
        }
        if (
            payload.get("format") != "product-coverage-rules"
            or type(payload.get("version")) is not int
            or payload.get("version") != 1
            or payload.get("summary") != binding
            or not isinstance(payload.get("rules"), list)
        ):
            raise ValueError("Policy rules do not match their training summary")
        records = []
        for row in payload["rules"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"state", "candidate", "volume", "priority"}
                or not isinstance(row["state"], str)
                or not isinstance(row["candidate"], str)
            ):
                raise ValueError("Invalid policy rule record")
            records.append(
                {
                    **row,
                    "volume": integer(row["volume"], "volume", positive=True),
                    "priority": integer(row["priority"], "priority", positive=True),
                }
            )
        rules = pl.DataFrame(
            records, infer_schema_length=None, schema_overrides={"volume": pl.Int128}
        )
        fitted = AnalysisResult(summary, rules, pl.DataFrame(), pl.DataFrame())
        fitted.rules = validate_policy(fitted)
        results.append(fitted)
    if not results:
        raise ValueError("Empty policy")
    return results
