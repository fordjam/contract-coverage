"""Offline daily-file ingestion and bounded root/year analysis."""

from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl

from . import __version__
from .continuous import Continuous
from .definitions import Definitions
from .model import SCHEMAS, UNDEF_INT, UNDEF_TS, BuildError, digest, frame, utc_date
from .sources import Sources, arrays, discover
from .storage import Writer, compact, scan, within, write_manifest
from .transforms import (
    add_adv,
    complete_daily_schema,
    merge_daily,
    reduce_statistics,
    statistic_candidates,
)

OUTPUT_TABLES = [name for name in SCHEMAS if name != "statistics_events"]
LOG = logging.getLogger(__name__)


def calendar_file(path: Path | None, roots: tuple[str, ...]) -> dict[str, list[date]]:
    if path is None:
        return {}
    result = defaultdict(set)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or []) != {"root", "date"}:
            raise BuildError("Session calendar CSV requires exactly root,date columns")
        for row in reader:
            root, day = row["root"], date.fromisoformat(row["date"])
            if root not in roots or day in result[root]:
                raise BuildError("Calendar contains an unsupported root or duplicate session")
            result[root].add(day)
    if set(result) != set(roots):
        raise BuildError("Calendar must supply sessions for every selected root")
    return {root: sorted(days) for root, days in result.items()}


def ingest(
    sources: Sources, raw: Path, end: date | None, history_start: date | None = None
) -> dict:
    LOG.info("Ingesting %s source files", len(sources.files))
    writer, definitions = Writer(raw), Definitions(sources)
    catalog_cache = OrderedDict()
    counts = defaultdict(int)

    def catalog(day):
        if day not in catalog_cache:
            catalog_cache[day] = definitions.catalog(day)
        catalog_cache.move_to_end(day)
        while len(catalog_cache) > 3:
            catalog_cache.popitem(last=False)
        return catalog_cache[day]

    for source in sources.files:
        if source.schema != "definition" or (end and source.day > end):
            continue
        rows, legs = definitions.load(source)
        resolved = definitions.catalog(source.day, include_inactive=True)
        instruments = []
        for key, row in rows.items():
            if row["root"] in sources.requested_roots:
                instruments.append(
                    {**row, **resolved.get(key, {}), "first_seen": None, "last_seen": None}
                )
        writer.rows("instruments", instruments)
        writer.rows("instrument_legs", [r for r in legs if r["root"] in sources.requested_roots])
    for number, source in enumerate(
        sorted(sources.files, key=lambda s: (s.schema, s.day, str(s.path))), 1
    ):
        if source.schema == "definition":
            continue
        if source.schema == "ohlcv-1d" and end and source.day > end:
            continue
        if number % 100 == 0:
            LOG.info("Source %s/%s: %s %s", number, len(sources.files), source.schema, source.day)
        for chunk in arrays(source):
            counts[source.schema + "_decoded"] += len(chunk)
            if source.schema == "statistics":
                types = chunk["stat_type"]
                target = int(((types == 3) | (types == 6) | (types == 9)).sum())
                counts["statistics_target_messages"] += target
                counts["statistics_other_types"] += len(chunk) - target
            try:
                records = statistic_candidates(chunk) if source.schema == "statistics" else chunk
            except BuildError as exc:
                raise BuildError(f"{source.path}: {exc}") from exc
            rows = []
            for record in records:
                event = int(record["ts_event"])
                utc_date(event)
                if source.schema == "statistics":
                    day, basis = record["date"], record["date_basis"]
                else:
                    day, basis = utc_date(event), "utc_day"
                    if day != source.day:
                        raise BuildError(f"{source.path}: UTC bar date differs from source day")
                if end and day > end:
                    continue
                if history_start and day < history_start:
                    continue
                identity = definitions.identity(
                    catalog(day), day, int(record["publisher_id"]), int(record["instrument_id"])
                )
                if identity["root"] is not None and identity["root"] not in sources.requested_roots:
                    continue
                row = {**identity, "source_batch": source.batch, "source_day": source.day}
                if source.schema == "statistics":
                    for key in (
                        "ts_event",
                        "ts_recv",
                        "sequence",
                        "channel_id",
                        "update_action",
                        "stat_flags",
                        "stat_type",
                    ):
                        value = int(record[key]) if record[key] is not None else None
                        row[key] = None if key == "ts_recv" and value in (0, UNDEF_TS) else value
                    for key, column in [("price", "price_nanos"), ("quantity", "quantity")]:
                        value = record[key]
                        row[column] = None if value == UNDEF_INT else value
                    row["date_basis"] = basis
                else:
                    volume = int(record["volume"])
                    row.update(bar_date_utc=day, volume=None if volume == UNDEF_TS else volume)
                    for key in ("open", "high", "low", "close"):
                        value = int(record[key])
                        row[key + "_nanos"] = None if value == UNDEF_INT else value
                rows.append(row)
            table = "statistics_events" if source.schema == "statistics" else "bars_utc"
            writer.rows(table, rows)
            counts[table + "_retained"] += len(rows)
    writer.finish()
    return dict(counts)


def _date_filter(data: pl.DataFrame, start: date | None, end: date | None) -> pl.DataFrame:
    if start:
        data = data.filter(pl.col("date") >= start)
    if end:
        data = data.filter(pl.col("date") <= end)
    return data


def _periods(raw: Path, root: str, calendar: list[date]) -> list[tuple[date, date]]:
    dates = set(calendar)
    for table in ("bars_utc", "statistics_events"):
        dates.update(scan(raw, table, root).select("date").unique().collect()["date"].to_list())
    result = []
    for year, month in sorted({(day.year, day.month) for day in dates}):
        first = date(year, month, 1)
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        result.append((first, following))
    return result


def _totals(
    data: pl.DataFrame, calendar: list[date], expected: dict[date, dict[str, int]], root: str
) -> pl.DataFrame:
    rows = []
    for (day, instrument_class), group in (
        data.filter(
            (pl.col("instrument_class") == "F") | pl.col("calendar_spread").fill_null(False)
        )
        .select(
            "date",
            "instrument_class",
            "open_interest",
            "cleared_volume",
            "adv_volume",
            "adv_volume_basis",
        )
        .partition_by(["date", "instrument_class"], as_dict=True)
        .items()
    ):
        count = expected.get(day, {}).get(instrument_class, 0)
        item = {
            "date": day,
            "root": root,
            "instrument_class": instrument_class,
            "observed_rows": group.height,
            "expected_instruments": count,
        }
        for metric in ("open_interest", "cleared_volume"):
            known = group[metric].drop_nulls()
            item[metric] = sum(known.to_list()) if len(known) else None
            item[metric + "_rows"] = len(known)
            item[metric + "_complete"] = count > 0 and len(known) == count
        volumes = group["adv_volume"].drop_nulls()
        complete = count > 0 and len(volumes) == count
        item["adv_volume"] = sum(volumes.to_list()) if complete else None
        item["adv_volume_basis"] = (
            "incomplete"
            if not complete
            else "cleared_volume"
            if set(group["adv_volume_basis"]) == {"cleared_volume"}
            else "mixed_utc_fallback"
        )
        rows.append(item)
    present = {(r["date"], r["instrument_class"]) for r in rows}
    for day in calendar:
        for instrument_class in ("F", "S"):
            if (day, instrument_class) not in present:
                rows.append(
                    {
                        "date": day,
                        "root": root,
                        "instrument_class": instrument_class,
                        "expected_instruments": expected.get(day, {}).get(instrument_class, 0),
                        "observed_rows": 0,
                        "open_interest_rows": 0,
                        "cleared_volume_rows": 0,
                        "open_interest_complete": False,
                        "cleared_volume_complete": False,
                        "adv_volume_basis": "missing",
                    }
                )
    schema = {
        k: v
        for k, v in SCHEMAS["root_daily"].items()
        if not (k.startswith("adv20") or k.startswith("adv60") or k == "calendar_basis")
    }
    return frame(rows, schema)


def process_root(
    raw: Path,
    output: Path,
    root: str,
    sources: Sources,
    supplied: list[date] | None,
    start: date | None,
    end: date | None,
    roll_days: int,
) -> None:
    LOG.info("Processing root %s", root)
    writer = Writer(output)
    if root == "__unmapped__":
        for first, following in _periods(raw, root, []):
            selected = pl.col("date").is_between(first, following, closed="left")
            stats = reduce_statistics(
                scan(raw, "statistics_events", root, first.year).filter(selected)
            )
            bars = scan(raw, "bars_utc", root, first.year).filter(selected).collect()
            daily = complete_daily_schema(merge_daily(stats, bars))
            writer.add("daily_statistics", _date_filter(stats, start, end))
            writer.add("bars_utc", _date_filter(bars, start, end))
            writer.add("daily_unmapped", _date_filter(daily, start, end))
        writer.finish()
        LOG.info("Completed root %s", root)
        return
    events = scan(raw, "statistics_events", root)
    observed_dates = events.select("date").unique().collect()["date"].to_list()
    basis = "supplied" if supplied is not None else "observed_statistics_sessions"
    if supplied is None and not observed_dates:
        observed_dates = (
            scan(raw, "bars_utc", root).select("date").unique().collect()["date"].to_list()
        )
        basis = "observed_utc_bar_dates"
    latest_source = max(
        (s.day for s in sources.files if root in s.roots and s.schema != "definition"), default=None
    )
    actual_upper = max(observed_dates) if observed_dates else None
    upper = end or max(
        (day for day in (latest_source, actual_upper) if day is not None), default=None
    )
    calendar = sorted(
        d
        for d in (supplied if supplied is not None else observed_dates)
        if upper is None or d <= upper
    )
    definitions = scan(raw, "instruments", root).unique(subset=["definition_version"]).collect()
    futures = definitions.filter(pl.col("instrument_class") == "F")
    snapshots = {
        key[0]: part.to_dicts() for key, part in futures.partition_by("date", as_dict=True).items()
    }
    snapshot_dates = sorted(
        {
            s.day
            for s in sources.files
            if s.schema == "definition" and root in s.roots and (end is None or s.day <= end)
        }
    )
    carry, totals_carry, seen = None, None, {}
    continuous = Continuous(root, roll_days)
    logged_year = None
    for first, following in _periods(raw, root, calendar):
        if first.year != logged_year:
            LOG.info("Root %s: %s", root, first.year)
            logged_year = first.year
        selected = pl.col("date").is_between(first, following, closed="left")
        stats = reduce_statistics(scan(raw, "statistics_events", root, first.year).filter(selected))
        bars = scan(raw, "bars_utc", root, first.year).filter(selected).collect()
        writer.add("daily_statistics", _date_filter(stats, start, end))
        writer.flush("daily_statistics")
        writer.add("bars_utc", _date_filter(bars, start, end))
        writer.flush("bars_utc")
        daily = merge_daily(stats, bars)
        del stats, bars
        period_calendar = [d for d in calendar if first <= d < following]
        dates = sorted(set(daily["date"].to_list()) | set(period_calendar))
        ranks, expected, chains = [], {}, {}
        for day in dates:
            eligible = [d for d in snapshot_dates if d <= day]
            snapshot = eligible[-1] if eligible else None
            chain = [
                r
                for r in snapshots.get(snapshot, [])
                if not r["deleted"]
                and r["activation"] is not None
                and r["expiration"] is not None
                and r["activation"] <= day <= r["expiration"]
            ]
            chain.sort(key=lambda r: (r["expiration"], r["raw_symbol"], r["contract_key"]))
            chains[day] = chain
            active_keys = [r["contract_key"] for r in chain]
            expected[day] = {
                "F": len(chain),
                "S": definitions.filter(
                    (pl.col("date") == snapshot)
                    & pl.col("calendar_structure").fill_null(False)
                    & ~pl.col("deleted")
                    & (pl.col("activation") <= day)
                    & (pl.col("expiration") >= day)
                    & pl.col("near_key").is_in(active_keys)
                    & pl.col("far_key").is_in(active_keys)
                ).height
                if snapshot
                else 0,
            }
            for rank, row in enumerate(chain, 1):
                ranks.append(
                    {
                        "date": day,
                        "contract_key": row["contract_key"],
                        "expiry_rank": rank,
                        "days_to_expiry": (row["expiration"] - day).days,
                    }
                )
        rank_frame = frame(
            ranks,
            {
                "date": pl.Date,
                "contract_key": pl.String,
                "expiry_rank": pl.UInt16,
                "days_to_expiry": pl.Int32,
            },
        )
        daily = daily.join(rank_frame, on=["date", "contract_key"], how="left")
        daily, carry = add_adv(daily, calendar, basis, carry)
        daily = complete_daily_schema(daily)
        for key, first, last in (
            daily.select("date", "contract_key")
            .filter(pl.col("contract_key").is_not_null())
            .group_by("contract_key")
            .agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"))
            .iter_rows()
        ):
            previous = seen.get(key, (first, last))
            seen[key] = (min(first, previous[0]), max(last, previous[1]))
        outright = daily.filter(pl.col("instrument_class") == "F")
        writer.add("daily_outright", _date_filter(outright, start, end))
        writer.flush("daily_outright")
        spread = daily.filter(pl.col("calendar_spread").fill_null(False)).with_columns(
            pl.lit("listed").alias("kind"),
            pl.lit(None, dtype=pl.UInt16).alias("near_rank"),
            pl.lit(None, dtype=pl.UInt16).alias("far_rank"),
        )
        quotes_by_day = {
            key[0]: {r["contract_key"]: r for r in part.to_dicts()}
            for key, part in outright.select(
                "date",
                "contract_key",
                "settlement",
                "open_interest",
                "open_interest_available_at",
                "settlement_final",
                "settlement_actual",
                "settlement_intraday",
                "settlement_available_at",
            )
            .partition_by("date", as_dict=True)
            .items()
        }
        del outright
        synthetic = []
        for day in period_calendar:
            chain, quotes = chains.get(day, []), quotes_by_day.get(day, {})
            continuous.step(day, chain, quotes)
            for n in range(min(5, len(chain) - 1)):
                near, far = chain[n], chain[n + 1]
                left_quote, right_quote = (
                    quotes.get(near["contract_key"], {}),
                    quotes.get(far["contract_key"], {}),
                )
                left, right = left_quote.get("settlement"), right_quote.get("settlement")
                flags = {}
                for name in ("settlement_final", "settlement_actual"):
                    values = [left_quote.get(name), right_quote.get(name)]
                    flags[name] = all(values) if None not in values else None
                intraday = [
                    left_quote.get("settlement_intraday"),
                    right_quote.get("settlement_intraday"),
                ]
                flags["settlement_intraday"] = any(intraday) if None not in intraday else None
                available = [
                    left_quote.get("settlement_available_at"),
                    right_quote.get("settlement_available_at"),
                ]
                flags["settlement_available_at"] = max(available) if None not in available else None
                synthetic.append(
                    {
                        "date": day,
                        "root": root,
                        "instrument_class": "S",
                        "contract_key": digest(
                            ["synthetic", near["contract_key"], far["contract_key"]]
                        ),
                        "raw_symbol": f"SYN({near['raw_symbol']},{far['raw_symbol']})",
                        "calendar_spread": True,
                        "near_key": near["contract_key"],
                        "calendar_structure": True,
                        "far_key": far["contract_key"],
                        "listed_price_sign": 1,
                        "kind": "synthetic",
                        "near_rank": n + 1,
                        "far_rank": n + 2,
                        "settlement": left - right
                        if left is not None and right is not None
                        else None,
                        **flags,
                        "settlement_date_basis": "derived_session_settlements",
                        "adv_volume_basis": "not_applicable",
                        "calendar_basis": basis,
                        "adv20_partial": True,
                        "adv60_partial": True,
                    }
                )
        if synthetic:
            spread = pl.concat([spread, frame(synthetic, SCHEMAS["daily_spread"])])
        writer.add("daily_spread", _date_filter(spread, start, end))
        writer.flush("daily_spread")
        del spread
        totals = _totals(daily, period_calendar, expected, root)
        totals, totals_carry = add_adv(
            totals.with_columns(pl.col("instrument_class").alias("contract_key")),
            calendar,
            basis,
            totals_carry,
        )
        for table, data in [
            ("root_daily", totals.drop("contract_key")),
            (
                "daily_excluded",
                daily.filter(
                    (pl.col("instrument_class") != "F")
                    & ~pl.col("calendar_spread").fill_null(False)
                ),
            ),
        ]:
            writer.add(table, _date_filter(data, start, end))
    rows, rolls = continuous.adjusted()
    writer.rows("continuous", [r for r in rows if within(r["date"], start, end)])
    writer.rows("roll_dates", [r for r in rolls if within(r["date"], start, end)])
    if definitions.height:
        observations = frame(
            [
                {"contract_key": key, "first_seen": value[0], "last_seen": value[1]}
                for key, value in seen.items()
            ],
            {"contract_key": pl.String, "first_seen": pl.Date, "last_seen": pl.Date},
        )
        dimensions = definitions.drop("first_seen", "last_seen").join(
            observations, on="contract_key", how="left"
        )
        writer.add("instruments", dimensions)
        writer.add("instrument_legs", scan(raw, "instrument_legs", root).unique().collect())
    writer.finish()
    LOG.info("Completed root %s", root)


def build(
    src: Path,
    out: Path,
    *,
    roots: tuple[str, ...] | None = None,
    start: date | None = None,
    end: date | None = None,
    workers: int = 2,
    roll_days: int = 5,
    calendar: Path | None = None,
    history_start: date | None = None,
) -> dict:
    if workers not in (1, 2) or roll_days < 0:
        raise BuildError("workers must be 1 or 2; roll-days must be nonnegative")
    if start and end and start > end:
        raise BuildError("--start must not follow --end")
    if history_start and ((start and history_start > start) or (end and history_start > end)):
        raise BuildError("--history-start must not follow output start or end")
    start = start or history_start
    if out.is_symlink():
        raise BuildError("Output must not be a symlink")
    src, out = src.resolve(), out.resolve()
    if src == out or src in out.parents or out in src.parents:
        raise BuildError("Source and output directories must be separate")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BuildError(
            "Output must be nonexistent or empty; existing builds are never overwritten"
        )
    sources = discover(src, roots)
    if history_start:
        baseline = set()
        scope = {root for source in sources.files for root in source.roots}
        for root in scope:
            previous = [
                s
                for s in sources.files
                if s.schema == "definition" and s.day < history_start and root in s.roots
            ]
            latest = max((s.day for s in previous), default=None)
            baseline.update(s for s in previous if s.day == latest)
        selected = [s for s in sources.files if s.day >= history_start or s in baseline]
        sources = replace(sources, files=selected)
    calendars = calendar_file(calendar, sources.requested_roots)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".futures-ts-", dir=out.parent))
    raw = stage / "_staging"
    try:
        counts = ingest(sources, raw, end, history_start)
        quality = Writer(stage)
        quality.rows(
            "source_quality", [r for r in sources.quality if within(r["date"], start, end)]
        )
        quality.finish()

        def run(root):
            try:
                process_root(raw, stage, root, sources, calendars.get(root), start, end, roll_days)
            except BuildError as exc:
                raise BuildError(f"Root {root}: {exc}") from exc

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(run, [*sources.requested_roots, "__unmapped__"]))
        shutil.rmtree(raw)
        LOG.info("Compacting annual Parquet partitions")
        compact(stage, OUTPUT_TABLES)
        metadata = {
            "format": "futures-ts",
            "version": 1,
            "package_version": __version__,
            "roots": list(sources.requested_roots),
            "start": str(start) if start else None,
            "end": str(end) if end else None,
            "history_start": str(history_start) if history_start else None,
            "warmup_scope": "bounded input history" if history_start else "full available history",
            "roll_days": roll_days,
            "calendar_basis": "supplied" if calendar else "observed_unverified",
            "completeness_scope": "eligible supplied as-of catalog, not exchange-wide",
            "price_storage": "bars: Int64 nanos; daily analysis: Float64 price units",
            "source_subset_files_skipped": sources.skipped_subsets,
            "input_counts": counts,
            "sources": [
                {
                    "schema": s.schema,
                    "day": s.day.isoformat(),
                    "batch": s.batch,
                    "filename": s.path.name,
                    "manifest_hash": s.content_hash,
                    "bytes": s.expected_size,
                }
                for s in sources.files
            ],
        }
        LOG.info("Writing output checksums")
        write_manifest(stage, metadata)
        from .validation import check
        from .coverage import coverage

        LOG.info("Checking output invariants")
        check(stage)
        LOG.info("Generating coverage evidence")
        coverage(stage)
        LOG.info("Publishing completed build")
        if out.exists():
            if any(out.iterdir()):
                raise BuildError("Output became nonempty during build")
            out.rmdir()
        os.replace(stage, out)
        return metadata
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
