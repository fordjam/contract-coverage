"""Vectorized statistics reduction, daily projection and calendar-aware ADV."""

from __future__ import annotations

from datetime import date

import polars as pl
import numpy as np

from .model import DAILY, IDENTITY, PRICE_SCALE, STAT, UNDEF_INT, UNDEF_TS, BuildError

KEY = ["date", "publisher_id", "instrument_id"]
STAT_NAMES = {3: "settlement", 6: "cleared_volume", 9: "open_interest"}


def latest_events(events: pl.LazyFrame) -> pl.LazyFrame:
    order = ["ts_event", "ts_recv", "sequence", "channel_id"]
    groups = KEY + ["stat_type"]
    latest = events.sort(order).unique(subset=groups, keep="last")
    contenders = events.join(
        latest.select(groups + order), on=groups + order, how="inner", nulls_equal=True
    )
    conflicts = (
        contenders.group_by(groups)
        .agg(
            pl.struct("price_nanos", "quantity", "update_action", "stat_flags")
            .n_unique()
            .alias("n")
        )
        .filter(pl.col("n") > 1)
        .limit(1)
        .collect()
    )
    if conflicts.height:
        raise BuildError("Conflicting statistic values share the same event ordering key")
    if latest.filter(~pl.col("update_action").is_in([1, 2])).limit(1).collect().height:
        raise BuildError("Unknown statistics update_action")
    if (
        latest.filter((pl.col("stat_type").is_in([6, 9])) & (pl.col("quantity") < 0))
        .limit(1)
        .collect()
        .height
    ):
        raise BuildError("Negative cleared volume or open interest")
    return latest


def statistic_candidates(chunk: np.ndarray) -> list[dict]:
    """Discard superseded updates before allocating Python identity dictionaries."""
    chunk = chunk[np.isin(chunk["stat_type"], [3, 6, 9])]
    if not len(chunk):
        return []
    fields = (
        "publisher_id instrument_id ts_event ts_recv ts_ref sequence channel_id "
        "update_action stat_flags stat_type price quantity"
    ).split()
    raw = pl.DataFrame({key: chunk[key] for key in fields})
    if raw.filter(pl.col("ts_event").is_in([0, UNDEF_TS])).height:
        raise BuildError("Missing event timestamp")
    raw = raw.with_columns(
        pl.col("ts_recv").replace({0: None, UNDEF_TS: None}),
        pl.col("ts_ref").replace({0: None, UNDEF_TS: None}),
        pl.col("price").replace({UNDEF_INT: None}).alias("price_nanos"),
        pl.col("quantity").replace({UNDEF_INT: None}),
    )
    local = (
        pl.from_epoch(pl.col("ts_event").cast(pl.Int64), time_unit="ns")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("America/Chicago")
    )
    fallback = (local.dt.date() + pl.duration(days=(local.dt.hour() >= 17).cast(pl.Int64))).cast(
        pl.Date
    )
    raw = raw.with_columns(
        pl.when(pl.col("ts_ref").is_not_null())
        .then(pl.from_epoch(pl.col("ts_ref").cast(pl.Int64), time_unit="ns").dt.date())
        .otherwise(fallback)
        .alias("date"),
        pl.when(pl.col("ts_ref").is_not_null())
        .then(pl.lit("ts_ref"))
        .otherwise(pl.lit("event_17ct"))
        .alias("date_basis"),
    )
    return latest_events(raw.lazy()).collect().to_dicts()


def reduce_statistics(events: pl.LazyFrame) -> pl.DataFrame:
    latest = latest_events(events)
    expressions = [pl.col(c).first() for c in IDENTITY if c not in KEY]
    for stat_type, name in STAT_NAMES.items():
        keep = pl.col("stat_type") == stat_type
        flags = pl.col("stat_flags").filter(keep).first()
        deleted = pl.col("update_action").filter(keep).first() == 2
        value = pl.col("price_nanos" if stat_type == 3 else "quantity").filter(keep).first()
        if stat_type == 3:
            supported = flags.and_(112) == 0
            value = pl.when(~deleted & supported & (flags.and_(128) == 0)).then(value)
            expressions.extend(
                [
                    value.alias("settlement_nanos"),
                    (value / PRICE_SCALE).alias(name),
                    supported.alias("settlement_flags_supported"),
                    pl.when(supported & (flags.and_(128) == 0))
                    .then(flags.and_(1) != 0)
                    .alias("settlement_final"),
                    pl.when(supported & (flags.and_(128) == 0))
                    .then(flags.and_(2) != 0)
                    .alias("settlement_actual"),
                    pl.when(supported & (flags.and_(128) == 0))
                    .then(flags.and_(8) != 0)
                    .alias("settlement_intraday"),
                ]
            )
        else:
            expressions.append(pl.when(~deleted).then(value).cast(pl.UInt64).alias(name))
        expressions.extend(
            [
                pl.col("ts_event").filter(keep).first().alias(name + "_ts_event"),
                pl.col("ts_recv").filter(keep).first().alias(name + "_available_at"),
                flags.alias(name + "_flags"),
                deleted.alias(name + "_deleted"),
                pl.col("date_basis").filter(keep).first().alias(name + "_date_basis"),
            ]
        )
    return (
        latest.group_by(KEY)
        .agg(expressions)
        .collect()
        .select([pl.col(k).cast(t) for k, t in STAT.items()])
    )


def merge_daily(stats: pl.DataFrame, bars: pl.DataFrame) -> pl.DataFrame:
    if bars.group_by(KEY).len().filter(pl.col("len") > 1).height:
        for group in bars.partition_by(KEY):
            if group.height > 1 and group.drop("source_batch", "source_day").unique().height > 1:
                raise BuildError("Conflicting UTC bars for one instrument and day")
        bars = bars.unique(subset=KEY, keep="first")
    selected = bars.select(
        *IDENTITY,
        "bar_date_utc",
        "volume",
        *[(pl.col(c + "_nanos") / PRICE_SCALE).alias(c) for c in ("open", "high", "low", "close")],
    )
    if bars.is_empty():
        daily = stats.with_columns(
            pl.lit(None, dtype=pl.Date).alias("bar_date_utc"),
            pl.lit(None, dtype=pl.UInt64).alias("volume"),
            *[pl.lit(None, dtype=pl.Float64).alias(c) for c in ("open", "high", "low", "close")],
        )
    elif stats.is_empty():
        daily = selected.with_columns(
            [pl.lit(None, dtype=t).alias(k) for k, t in STAT.items() if k not in selected.columns]
        )
    else:
        daily = stats.join(
            selected, on=KEY, how="full", coalesce=True, nulls_equal=True, suffix="_bar"
        )
        for column in IDENTITY:
            if column not in KEY:
                daily = daily.with_columns(pl.coalesce(column, column + "_bar").alias(column)).drop(
                    column + "_bar"
                )
    return daily.with_columns(
        pl.when(pl.col("bar_date_utc").is_not_null())
        .then(pl.lit("utc_day_same_date_label"))
        .alias("ohlcv_basis"),
        pl.coalesce("cleared_volume", "volume").alias("adv_volume"),
        pl.when(pl.col("cleared_volume").is_not_null())
        .then(pl.lit("cleared_volume"))
        .when(pl.col("volume").is_not_null())
        .then(pl.lit("ohlcv_utc_fallback"))
        .otherwise(pl.lit("missing"))
        .alias("adv_volume_basis"),
    )


def add_adv(
    data: pl.DataFrame, calendar: list[date], basis: str, history: pl.DataFrame | None = None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    columns = ["date", "contract_key", "adv_volume"]
    samples = data.select(columns)
    if history is not None and history.height:
        samples = pl.concat([history, samples])
    index = pl.DataFrame(
        {"date": calendar, "session_index": range(len(calendar))},
        schema={"date": pl.Date, "session_index": pl.Int64},
    )
    samples = samples.join(index, on="date", how="inner").sort("contract_key", "session_index")
    # Compute on narrow keys, then attach once instead of copying wide facts per window.
    joined = data.select("date", "contract_key").join(index, on="date", how="left")
    for size in (20, 60):
        rolled = samples.rolling(
            index_column="session_index", period=f"{size}i", group_by="contract_key"
        ).agg(
            pl.col("adv_volume").mean().alias(f"adv{size}"),
            pl.col("adv_volume").count().cast(pl.UInt16).alias(f"adv{size}_samples"),
        )
        joined = joined.join(rolled, on=["contract_key", "session_index"], how="left")
        sessions = pl.min_horizontal(pl.col("session_index") + 1, pl.lit(size)).cast(pl.UInt16)
        joined = joined.with_columns(
            pl.when(pl.col("session_index").is_not_null())
            .then(sessions)
            .alias(f"adv{size}_sessions"),
            (
                (pl.col(f"adv{size}_samples").fill_null(0) < size) | pl.lit(basis != "supplied")
            ).alias(f"adv{size}_partial"),
        )
    joined = joined.drop("session_index").with_columns(pl.lit(basis).alias("calendar_basis"))
    joined = data.join(joined, on=["date", "contract_key"], how="left")
    if calendar and data.height:
        last = max(data["date"])
        past = [d for d in calendar if d <= last][-59:]
        carry = samples.filter(pl.col("date").is_in(past)).select(columns)
    else:
        carry = samples.select(columns)
    return joined, carry


def complete_daily_schema(data: pl.DataFrame) -> pl.DataFrame:
    return data.with_columns(
        [pl.lit(None, dtype=t).alias(k) for k, t in DAILY.items() if k not in data.columns]
    ).select([pl.col(k).cast(t) for k, t in DAILY.items()])
