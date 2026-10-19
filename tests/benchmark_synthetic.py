"""Offline synthetic DBN memory workload; writes only beneath --work."""

from __future__ import annotations

import os

os.environ.setdefault("POLARS_MAX_THREADS", "2")

import argparse
import json
import resource
import sys
from pathlib import Path
from time import perf_counter

from futures_ts.pipeline import build
from futures_ts.validation import check
from tests.generate_fixtures import catalog_workload, example


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=5)
    parser.add_argument("--mode", choices=("revisions", "catalog"), default="revisions")
    args = parser.parse_args()
    if args.scale < 1 or (args.work.exists() and any(args.work.iterdir())):
        parser.error("Use a positive scale and an empty work directory")
    args.work.mkdir(parents=True, exist_ok=True)
    if args.mode == "catalog":
        catalog_workload(args.work / "source")
        roots = ("CL", "NG")
    else:
        example(args.work / "source", args.scale)
        roots = ("CL",)
    started = perf_counter()
    metadata = build(args.work / "source", args.work / "output", roots=roots, workers=2)
    elapsed = perf_counter() - started
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = rss if sys.platform == "darwin" else rss * 1024
    result = {
        "scale": args.scale,
        "mode": args.mode,
        "workers": 2,
        "elapsed_build_s": round(elapsed, 3),
        "peak_process_RSS_MiB": round(rss_bytes / 2**20, 2),
        "input_counts": metadata["input_counts"],
        "checks": check(args.work / "output"),
    }
    (args.work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    if rss_bytes >= 2_000_000_000:
        raise SystemExit("Memory requirement exceeded")


if __name__ == "__main__":
    main()
