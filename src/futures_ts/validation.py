"""Fail closed on corrupted artifacts and cross-table accounting errors."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from .model import SCHEMAS, BuildError
from .storage import file_hash, manifest, scan

UNIQUE = {
    "instruments": ["definition_version"],
    "instrument_legs": ["definition_version", "leg_index"],
    "bars_utc": ["date", "publisher_id", "instrument_id"],
    "daily_statistics": ["date", "publisher_id", "instrument_id"],
    "daily_outright": ["date", "contract_key"],
    "daily_spread": ["date", "kind", "contract_key"],
    "daily_unmapped": ["date", "publisher_id", "instrument_id"],
    "daily_excluded": ["date", "publisher_id", "instrument_id"],
    "root_daily": ["date", "root", "instrument_class"],
    "continuous": ["date", "root", "rank"],
    "roll_dates": ["date", "root", "rank"],
    "source_quality": ["date", "root", "schema", "batch"],
}


def reject(query: pl.LazyFrame, condition: pl.Expr, message: str) -> None:
    if query.filter(condition).limit(1).collect().height:
        raise BuildError(message)


def check(base: Path) -> dict:
    metadata = manifest(base)
    expected = {entry["path"] for entry in metadata["files"]}
    actual = {p.relative_to(base).as_posix() for p in base.rglob("*.parquet")}
    if actual != expected:
        raise BuildError("Parquet files disagree with output manifest")
    tables = set()
    for entry in metadata["files"]:
        path = base / entry["path"]
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != base.parent):
            raise BuildError("Symlinked output is unsupported")
        if path.stat().st_size != entry["bytes"] or file_hash(path) != entry["sha256"]:
            raise BuildError(f"Output checksum mismatch: {entry['path']}")
        table = Path(entry["path"]).parts[0]
        if table not in UNIQUE:
            raise BuildError(f"Unknown output table {table}")
        if dict(pl.scan_parquet(path, hive_partitioning=False).collect_schema()) != SCHEMAS[table]:
            raise BuildError(f"Schema mismatch in {entry['path']}")
        tables.add(table)
    if tables != set(UNIQUE):
        raise BuildError("Output is missing required tables")
    counts = {}
    for table, keys in UNIQUE.items():
        counts[table] = 0
        # A global group-by would retain every daily key in the entire archive.
        # Root/year placement makes duplicate checks bounded and independent.
        for path in sorted((base / table).rglob("*.parquet")):
            query = pl.scan_parquet(path, hive_partitioning=False)
            count = query.select(pl.len()).collect().item()
            counts[table] += count
            if (path.name == "empty.parquet" and count) or path.name not in (
                "empty.parquet",
                "data.parquet",
            ):
                raise BuildError("Partitions must contain one compact data.parquet file")
            if path.name != "empty.parquet":
                partition_root = path.parent.parent.name[5:]
                partition_year = int(path.parent.name[5:])
                root = (
                    pl.col("root").is_not_null()
                    if partition_root == "__unmapped__"
                    else ~pl.col("root").eq_missing(pl.lit(partition_root))
                )
                reject(
                    query,
                    root | (pl.col("date").dt.year() != partition_year),
                    f"Invalid partition in {table}",
                )
            if query.group_by(keys).len().filter(pl.col("len") > 1).limit(1).collect().height:
                raise BuildError(f"Duplicate keys in {table}")
            if (
                table == "continuous"
                and query.filter(pl.col("contract_key").is_not_null())
                .group_by("date", "root", "contract_key")
                .len()
                .filter(pl.col("len") > 1)
                .limit(1)
                .collect()
                .height
            ):
                raise BuildError("Continuous ranks repeat a contract")
            floats = [~pl.col(c).is_finite() for c, t in SCHEMAS[table].items() if t == pl.Float64]
            if floats:
                reject(query, pl.any_horizontal(floats), f"Nonfinite values in {table}")
            if "definition_date" in SCHEMAS[table] and table != "instruments":
                reject(
                    query,
                    (pl.col("definition_date") > pl.col("date"))
                    | (pl.col("activation") > pl.col("date"))
                    | (pl.col("expiration") < pl.col("date")),
                    f"Invalid as-of mapping in {table}",
                )
    reject(
        scan(base, "daily_unmapped"),
        pl.col("root").is_not_null() | pl.col("contract_key").is_not_null(),
        "Unmapped rows contain guessed metadata",
    )
    reject(
        scan(base, "daily_outright"),
        (pl.col("instrument_class") != "F")
        | pl.col("expiry_rank").is_null()
        | (pl.col("expiry_rank") < 1),
        "Invalid outright ranks",
    )
    reject(
        scan(base, "bars_utc"), pl.col("bar_date_utc") != pl.col("date"), "UTC bar date mismatch"
    )
    reject(
        scan(base, "bars_utc"),
        (pl.col("low_nanos") > pl.col("high_nanos"))
        | (pl.col("low_nanos") > pl.min_horizontal("open_nanos", "close_nanos"))
        | (pl.col("high_nanos") < pl.max_horizontal("open_nanos", "close_nanos")),
        "Invalid OHLC range",
    )
    spread = scan(base, "daily_spread")
    reject(
        spread,
        ~pl.col("kind").is_in(["listed", "synthetic"])
        | pl.col("near_key").is_null()
        | pl.col("far_key").is_null()
        | pl.col("listed_price_sign").is_null()
        | ~pl.col("listed_price_sign").is_in([-1, 1])
        | ~pl.col("calendar_spread"),
        "Invalid calendar spread",
    )
    reject(
        spread.filter(pl.col("kind") == "synthetic"),
        pl.col("volume").is_not_null()
        | (pl.col("listed_price_sign") != 1)
        | pl.col("cleared_volume").is_not_null()
        | pl.col("open_interest").is_not_null(),
        "Synthetic spreads invent activity",
    )
    for partition in sorted((base / "root_daily").glob("root=*/year=*")):
        root, year = partition.parent.name[5:], int(partition.name[5:])
        totals = scan(base, "root_daily", root, year)
        facts = pl.concat(
            [
                scan(base, "daily_outright", root, year).select(
                    "date", "root", "instrument_class", "open_interest", "cleared_volume"
                ),
                scan(base, "daily_spread", root, year)
                .filter(pl.col("kind") == "listed")
                .select("date", "root", "instrument_class", "open_interest", "cleared_volume"),
            ]
        )
        for metric in ("open_interest", "cleared_volume"):
            summed = facts.group_by("date", "root", "instrument_class").agg(
                pl.when(pl.col(metric).count() > 0).then(pl.col(metric).sum()).alias("expected")
            )
            joined = totals.join(summed, on=["date", "root", "instrument_class"], how="left")
            reject(
                joined,
                ~pl.col(metric).eq_missing(pl.col("expected")),
                f"Root {metric} does not reconcile",
            )
        quotes = scan(base, "daily_outright", root, year).select(
            "date", "contract_key", "settlement"
        )
        synthetics = scan(base, "daily_spread", root, year).filter(pl.col("kind") == "synthetic")
        joined = synthetics.join(
            quotes.rename({"contract_key": "near_key", "settlement": "near"}),
            on=["date", "near_key"],
            how="left",
        ).join(
            quotes.rename({"contract_key": "far_key", "settlement": "far"}),
            on=["date", "far_key"],
            how="left",
        )
        calculated = pl.col("near") - pl.col("far")
        reject(
            joined,
            (pl.col("settlement").is_null() != calculated.is_null())
            | ((pl.col("settlement") - calculated).abs() > 1e-8),
            "Synthetic spread sign/value mismatch",
        )
    reject(scan(base, "continuous"), ~pl.col("rank").is_between(1, 6), "Invalid continuous rank")
    oi_rolls = scan(base, "roll_dates").filter(pl.col("reason") == "oi_crossover")
    reject(
        oi_rolls,
        pl.any_horizontal(
            [
                pl.col(c).is_null()
                for c in (
                    "current_oi",
                    "next_oi",
                    "oi_reference_date",
                    "oi_current_available_at",
                    "oi_next_available_at",
                )
            ]
        )
        | (pl.col("oi_reference_date") >= pl.col("date"))
        | (pl.col("oi_current_available_at") > pl.col("oi_cutoff"))
        | (pl.col("oi_next_available_at") > pl.col("oi_cutoff"))
        | (pl.col("next_oi") <= pl.col("current_oi")),
        "OI roll uses unavailable or invalid evidence",
    )
    return {"status": "ok", "rows": counts}
