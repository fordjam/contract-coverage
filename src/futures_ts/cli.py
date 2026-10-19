"""Offline build, coverage and invariant-check commands."""

from __future__ import annotations

import os

os.environ.setdefault("POLARS_MAX_THREADS", "2")

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from polars.exceptions import PolarsError

from .model import BuildError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="futures-ts", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Build from local Databento batch directories")
    build_parser.add_argument("--src", required=True, type=Path)
    build_parser.add_argument("--out", required=True, type=Path)
    build_parser.add_argument("--roots", help="Comma-separated CME roots; default all 35")
    build_parser.add_argument("--start", type=date.fromisoformat)
    build_parser.add_argument("--end", type=date.fromisoformat)
    build_parser.add_argument(
        "--history-start",
        type=date.fromisoformat,
        help="Explicitly bound input history and roll initialization; default full history",
    )
    build_parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    build_parser.add_argument(
        "--roll-days", type=int, default=5, help="Calendar days before expiry (default 5)"
    )
    build_parser.add_argument(
        "--calendar", type=Path, help="Optional complete session CSV: root,date"
    )
    for command in ("coverage", "check"):
        sub = commands.add_parser(command)
        sub.add_argument("--out", required=True, type=Path)
    analysis = commands.add_parser("analyze", help="Relative C1/C2 and calendar activity coverage")
    analysis.add_argument("--out", required=True, type=Path)
    analysis.add_argument("--start", type=date.fromisoformat)
    analysis.add_argument("--end", type=date.fromisoformat)
    analysis.add_argument("--lookback", type=int, default=60)
    analysis.add_argument("--target", type=float, default=0.95)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.command == "build":
            from .pipeline import build

            result = build(
                args.src,
                args.out,
                roots=tuple(r.strip() for r in args.roots.split(",")) if args.roots else None,
                start=args.start,
                end=args.end,
                workers=args.workers,
                roll_days=args.roll_days,
                calendar=args.calendar,
                history_start=args.history_start,
            )
            result = {"status": "ok", "out": str(args.out), "input_counts": result["input_counts"]}
        elif args.command == "coverage":
            from .coverage import coverage

            result = coverage(args.out)
        elif args.command == "analyze":
            from .rank_coverage import analyze_ranks

            result = analyze_ranks(
                args.out, start=args.start, end=args.end, lookback=args.lookback, target=args.target
            )
        else:
            from .validation import check

            result = check(args.out)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except ModuleNotFoundError as exc:
        if exc.name not in {"databento", "databento_dbn", "numpy"}:
            raise
        print(
            "futures-ts: install optional dependencies with "
            "pip install '.[databento]' from the checkout",
            file=sys.stderr,
        )
        return 2
    except (BuildError, OSError, ValueError, OverflowError, PolarsError) as exc:
        print(f"futures-ts: {exc}", file=sys.stderr)
        return 2
