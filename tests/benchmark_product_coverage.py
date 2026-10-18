"""Offline synthetic scale benchmark; all output lives in the requested work directory."""

from __future__ import annotations

import os

os.environ.setdefault("POLARS_MAX_THREADS", "2")

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import resource
import sys
from time import perf_counter

import polars as pl

from product_coverage import analyze
from product_coverage.files import scan_files, write_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--families", type=int, default=200)
    parser.add_argument("--days", type=int, default=1260)
    args = parser.parse_args()
    if args.families < 1 or args.days < 1:
        parser.error("families and days must be positive")
    args.work.mkdir(parents=True, exist_ok=False)
    inputs = args.work / "facts"
    inputs.mkdir()
    days, current = [], date(2020, 1, 1)
    while len(days) < args.days:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    paths = []
    for family in range(args.families):
        for category, size in (("outright", 12), ("listed_spread", 8)):
            rows = []
            for day in days:
                for index in range(size):
                    delivery = f"{day.year + 1}-{index + 1:02d}"
                    far = f"{day.year + 1}-{index + 2:02d}"
                    rows.append(
                        {
                            "date": day,
                            "venue": "SYNTHETIC",
                            "family": f"FAMILY-{family:06d}",
                            "category": category,
                            "source": "synthetic-benchmark",
                            "metric": "trade_volume",
                            "unit": "contracts",
                            "date_basis": "synthetic_weekday",
                            "product_id": delivery
                            if category == "outright"
                            else delivery + "/" + far,
                            "delivery_month": delivery if category == "outright" else None,
                            "near_month": delivery if category == "listed_spread" else None,
                            "far_month": far if category == "listed_spread" else None,
                            "volume": 2 ** (size - index) + day.day % 5,
                        }
                    )
            path = inputs / f"{family:06d}-{category}.parquet"
            pl.DataFrame(rows).sort("date", "product_id").write_parquet(
                path, compression="zstd", row_group_size=65_536
            )
            paths.append(path)
    began = perf_counter()
    report = write_report(
        analyze(scan_files(paths), target="0.99"),
        args.work / "analysis",
        {"kind": "synthetic_benchmark"},
    )
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = rss if sys.platform == "darwin" else rss * 1024
    result = {
        "families": args.families,
        "groups": 2 * args.families,
        "dates": args.days,
        "rows": args.families * args.days * 20,
        "parquet_bytes": sum(path.stat().st_size for path in paths),
        "analysis_seconds": perf_counter() - began,
        "process_peak_rss_bytes_including_generation": rss_bytes,
        "totals": report["totals"],
    }
    (args.work / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
