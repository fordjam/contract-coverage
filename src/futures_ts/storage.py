"""Typed, compact root/year Parquet partitions and build manifests."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import polars as pl

from .model import ROOTS, SCHEMAS, BuildError, frame


def scan(base: Path, table: str, root: str | None = None, year: int | None = None) -> pl.LazyFrame:
    path = base / table
    if root is not None:
        path /= "root=" + root
    paths = sorted(path.rglob("*.parquet")) if path.exists() else []
    if year is not None:
        paths = [p for p in paths if p.parent.name == f"year={year}"]
    if not paths:
        return pl.DataFrame(schema=SCHEMAS[table]).lazy()
    return pl.scan_parquet(paths, hive_partitioning=False)


class Writer:
    def __init__(self, base: Path, threshold: int = 100_000):
        self.base, self.threshold = base, threshold
        self.buffers = defaultdict(list)
        self.counts = defaultdict(int)
        self.serial = 0

    def rows(self, table: str, rows: list[dict]) -> None:
        if rows:
            self.add(table, frame(rows, SCHEMAS[table]))

    def add(self, table: str, data: pl.DataFrame) -> None:
        for piece in data.iter_slices(n_rows=65_536):
            if piece.is_empty():
                continue
            piece = piece.select([pl.col(k).cast(t) for k, t in SCHEMAS[table].items()])
            self.buffers[table].append(piece)
            self.counts[table] += piece.height
            if self.counts[table] >= self.threshold:
                self.flush(table)

    def flush(self, table: str) -> None:
        if not self.buffers[table]:
            return
        data = pl.concat(self.buffers.pop(table)).with_columns(
            pl.col("date").dt.year().alias("__year")
        )
        self.counts[table] = 0
        for (root, year), part in data.partition_by(["root", "__year"], as_dict=True).items():
            directory = self.base / table / f"root={root or '__unmapped__'}" / f"year={year}"
            directory.mkdir(parents=True, exist_ok=True)
            self.serial += 1
            part.drop("__year").write_parquet(
                directory / f"part-{self.serial:08}.parquet",
                compression="zstd",
                row_group_size=65_536,
                statistics=True,
            )

    def finish(self) -> None:
        for table in list(self.buffers):
            self.flush(table)


def compact(base: Path, tables: list[str]) -> None:
    for table in tables:
        directory = base / table
        for partition in sorted(directory.glob("root=*/year=*")):
            paths = sorted(partition.glob("part-*.parquet"))
            if not paths:
                continue
            columns = SCHEMAS[table]
            order = [
                c
                for c in (
                    "date",
                    "contract_key",
                    "instrument_class",
                    "definition_version",
                    "publisher_id",
                    "instrument_id",
                    "rank",
                    "kind",
                    "leg_index",
                    "schema",
                    "batch",
                )
                if c in columns
            ]
            query = pl.scan_parquet(paths, hive_partitioning=False).sort(order)
            target = partition / "data.parquet"
            query.sink_parquet(target, compression="zstd", row_group_size=65_536, statistics=True)
            for path in paths:
                path.unlink()
        if not directory.exists():
            directory.mkdir(parents=True)
            pl.DataFrame(schema=SCHEMAS[table]).write_parquet(directory / "empty.parquet")


def file_hash(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_manifest(base: Path, metadata: dict) -> None:
    files = []
    for path in sorted(base.rglob("*.parquet")):
        files.append(
            {
                "path": path.relative_to(base).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
            }
        )
    (base / "manifest.json").write_text(
        json.dumps({**metadata, "files": files}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def manifest(base: Path) -> dict:
    try:
        value = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        if value.get("format") != "futures-ts" or value.get("version") != 1:
            raise BuildError("Unsupported futures-ts manifest")
        roots = value["roots"]
        if (
            not isinstance(roots, list)
            or not roots
            or set(roots) - set(ROOTS)
            or len(roots) != len(set(roots))
        ):
            raise BuildError("Invalid roots in output manifest")
        seen = set()
        for item in value["files"]:
            path = Path(item["path"])
            if path.is_absolute() or ".." in path.parts or path.suffix != ".parquet":
                raise BuildError("Unsafe output manifest path")
            if path.as_posix() in seen:
                raise BuildError("Duplicate output manifest path")
            seen.add(path.as_posix())
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"Cannot read output manifest: {exc}") from exc


def within(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or day >= start) and (end is None or day <= end)
