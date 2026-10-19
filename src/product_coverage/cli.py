"""Offline, vendor-independent product coverage from mapped CSV/Parquet exports."""

from __future__ import annotations

import os

os.environ.setdefault("POLARS_MAX_THREADS", "2")

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl
from polars.exceptions import PolarsError

from .engine import analyze, evaluate
from .files import fingerprint, load_json, read_policy, resolve_inputs, scan_files, write_report
from .schema import METHODS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="product-coverage", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("normalize", "analyze", "evaluate"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", nargs="+", type=Path, required=True)
        sub.add_argument("--mapping", type=Path, help="Version 1 column/constants mapping JSON")
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument(
            "--include-local-paths",
            action="store_true",
            help="Opt into absolute paths in provenance; reports still contain licensed activity",
        )
        if name != "normalize":
            sub.add_argument("--start", type=date.fromisoformat, required=name == "evaluate")
            sub.add_argument("--end", type=date.fromisoformat)
            sub.add_argument("--universe-complete", action="store_true")
        if name == "analyze":
            sub.add_argument("--method", choices=METHODS, default="monthly-calendar")
            sub.add_argument("--target", default="0.99", help="Exact decimal fraction in (0,1]")
        elif name == "evaluate":
            sub.add_argument("--policy", type=Path, required=True)
    bridge = commands.add_parser("from-futures-ts", help="Export existing futures-ts Parquet")
    bridge.add_argument("--src", type=Path, required=True)
    bridge.add_argument("--out", type=Path, required=True)
    bridge.add_argument("--start", type=date.fromisoformat, required=True)
    bridge.add_argument("--end", type=date.fromisoformat, required=True)
    bridge.add_argument("--include-local-paths", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise ValueError("Output already exists; choose a new path")
        if args.command == "from-futures-ts":
            from .databento import from_futures_ts

            args.out.mkdir(parents=True)
            count = 0
            for count, data in enumerate(from_futures_ts(args.src, args.start, args.end), 1):
                data.with_columns(pl.col("volume").cast(pl.Int64)).sort(
                    "date", "product_id"
                ).write_parquet(
                    args.out / f"part-{count:06d}.parquet",
                    compression="zstd",
                    row_group_size=65_536,
                )
            if not count:
                raise ValueError("No observations in the selected window")
            receipt = {
                "format": "product-coverage-adapter",
                "version": 1,
                "adapter": "futures-ts",
                "start": str(args.start),
                "end": str(args.end),
                "source_manifest": fingerprint(
                    [args.src / "manifest.json"], include_paths=args.include_local_paths
                ),
                "outputs": fingerprint(
                    sorted(args.out.glob("*.parquet")), include_paths=args.include_local_paths
                ),
                "accounting": (
                    "Outright cleared reference-session volume; listed spread UTC-day bar volume. "
                    "Synthetic spreads excluded; unknown-family rows remain in source "
                    "coverage reports."
                ),
            }
            (args.out / "adapter.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps({"partitions": count, "out": str(args.out)}))
            return 0
        mapping = load_json(args.mapping) if args.mapping else None
        paths = resolve_inputs(args.input)
        query = scan_files(paths, mapping)
        if args.command == "normalize":
            query.with_columns(pl.col("volume").cast(pl.Int64)).sink_parquet(
                args.out, compression="zstd", row_group_size=65_536
            )
            print(json.dumps({"out": str(args.out)}))
            return 0
        provenance = {
            "command": args.command,
            "inputs": fingerprint(paths, include_paths=args.include_local_paths),
            "mapping": mapping,
            "start": str(args.start) if args.start else None,
            "end": str(args.end) if args.end else None,
            "universe_complete": args.universe_complete,
        }
        common = {"start": args.start, "end": args.end, "universe_complete": args.universe_complete}
        if args.command == "evaluate":
            provenance["policy_files"] = fingerprint(
                [args.policy / "report.json", *sorted(args.policy.glob("group-*/rules.json"))],
                include_paths=args.include_local_paths,
            )
            results = evaluate(query, read_policy(args.policy), **common)
        else:
            provenance.update(method=args.method, target=args.target)
            results = analyze(query, method=args.method, target=args.target, **common)
        report = write_report(results, args.out, provenance)
        print(json.dumps({"out": str(args.out), "totals": report["totals"]}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError, PolarsError) as exc:
        print(f"product-coverage: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
