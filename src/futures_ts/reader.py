"""Partition-pruned lazy access to the persisted analysis tables."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from .model import SCHEMAS, BuildError
from .storage import manifest


def scan_daily(
    base: Path,
    root: str,
    *,
    table: str = "daily_outright",
    start: date | None = None,
    end: date | None = None,
    columns: list[str] | None = None,
) -> pl.LazyFrame:
    """Read only selected root/year files; leave row/column pruning lazy."""
    metadata = manifest(base)
    if root not in metadata["roots"] or table not in {
        "daily_outright",
        "daily_spread",
        "daily_statistics",
        "bars_utc",
        "root_daily",
        "continuous",
    }:
        raise BuildError("Select a built root and an analysis table")
    if start and end and start > end:
        raise BuildError("start must not follow end")
    if columns is not None and (
        len(set(columns)) != len(columns) or set(columns) - set(SCHEMAS[table])
    ):
        raise BuildError("Unknown or duplicate requested columns")
    paths = []
    for item in metadata["files"]:
        path = Path(item["path"])
        if len(path.parts) != 4 or path.parts[:2] != (table, "root=" + root):
            continue
        year = int(path.parts[2][5:])
        if (start is None or year >= start.year) and (end is None or year <= end.year):
            paths.append(base / path)
    query = (
        pl.scan_parquet(paths, hive_partitioning=False)
        if paths
        else pl.DataFrame(schema=SCHEMAS[table]).lazy()
    )
    if start:
        query = query.filter(pl.col("date") >= start)
    if end:
        query = query.filter(pl.col("date") <= end)
    return query.select(columns) if columns is not None else query
