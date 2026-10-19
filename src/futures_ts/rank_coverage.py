"""Relative-contract activity coverage from a validated local futures build."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import polars as pl
from product_coverage.presentation import safe_frame

from .coverage import atomic_text
from .model import BuildError, frame
from .storage import manifest, scan

LOG = logging.getLogger(__name__)
METRICS = ("cleared_volume", "volume", "open_interest")


def rank_catalog(instruments: pl.DataFrame, days: list[date]) -> pl.DataFrame:
    """Rank the whole eligible snapshot, including contracts without observations."""
    snapshots = {}
    for (day,), group in instruments.partition_by("date", as_dict=True).items():
        snapshots[day] = group.filter(pl.col("instrument_class") == "F").to_dicts()
    dates = sorted(snapshots)
    rows = []
    for day in days:
        previous = [d for d in dates if d <= day]
        if not previous:
            continue
        chain = [
            r
            for r in snapshots[previous[-1]]
            if not r["deleted"]
            and r["activation"] is not None
            and r["expiration"] is not None
            and r["activation"] <= day <= r["expiration"]
        ]
        chain.sort(key=lambda r: (r["expiration"], r["raw_symbol"], r["contract_key"]))
        for rank, row in enumerate(chain, 1):
            rows.append(
                {
                    "date": day,
                    "contract_key": row["contract_key"],
                    "rank": rank,
                    "symbol": row["raw_symbol"],
                    "expiration": row["expiration"],
                }
            )
    return frame(
        rows,
        {
            "date": pl.Date,
            "contract_key": pl.String,
            "rank": pl.UInt16,
            "symbol": pl.String,
            "expiration": pl.Date,
        },
    )


def label_activity(data: pl.DataFrame, ranks: pl.DataFrame, category: str) -> pl.DataFrame:
    if category == "outrights":
        return data.join(
            ranks.select("date", "contract_key", "rank"), on=["date", "contract_key"], how="left"
        ).with_columns(pl.concat_str(pl.lit("C"), pl.col("rank")).alias("label"))
    near = ranks.select(
        "date",
        pl.col("contract_key").alias("near_key"),
        pl.col("rank").alias("near_rank"),
        pl.col("symbol").alias("near_symbol"),
    )
    far = ranks.select(
        "date",
        pl.col("contract_key").alias("far_key"),
        pl.col("rank").alias("far_rank"),
        pl.col("symbol").alias("far_symbol"),
    )
    return (
        data.filter(pl.col("kind") == "listed")
        .drop("near_rank", "far_rank")
        .join(near, on=["date", "near_key"], how="left")
        .join(far, on=["date", "far_key"], how="left")
        .with_columns(
            pl.concat_str(pl.lit("C"), "far_rank", pl.lit("-C"), "near_rank").alias("label"),
            (-pl.col("listed_price_sign") * pl.col("settlement")).alias("label_settlement"),
        )
    )


def summarize_activity(
    data: pl.DataFrame, root: str, category: str, metric: str, targets: tuple[float, ...]
) -> tuple[list[dict], list[dict]]:
    """Minimize fixed label count for aggregate known activity, without leg costs."""
    known = data.filter(pl.col(metric).is_not_null())
    total = int(known[metric].sum()) if known.height else 0
    if total <= 0:
        return [], []
    groups = (
        known.group_by("label")
        .agg(pl.col(metric).sum().alias("activity"), pl.col("date").n_unique().alias("known_days"))
        .sort(["activity", "label"], descending=[True, False], nulls_last=True)
    )
    daily = known.group_by("date").agg(pl.col(metric).sum().alias("denominator"))
    positive = daily.filter(pl.col("denominator") > 0)
    rows, cumulative = [], 0
    for row in groups.to_dicts():
        # Unranked activity stays in the denominator but cannot be selected by a C label.
        if row["label"] is not None:
            cumulative += row["activity"]
        rows.append(
            {
                "root": root,
                "category": category,
                "metric": metric,
                **row,
                "share": row["activity"] / total,
                "cumulative_share": cumulative / total,
                "denominator": total,
            }
        )
    candidates = [r for r in rows if r["label"] is not None and r["activity"] > 0]
    recommendations = []
    for target in targets:
        selected, captured = [], 0
        for row in candidates:
            selected.append(row["label"])
            captured += row["activity"]
            if captured / total >= target:
                break
        numerator = (
            known.filter(pl.col("label").is_in(selected))
            .group_by("date")
            .agg(pl.col(metric).sum().alias("numerator"))
        )
        daily_share = positive.join(numerator, on="date", how="left").with_columns(
            (pl.col("numerator").fill_null(0) / pl.col("denominator")).alias("share")
        )["share"]
        legs = set()
        for label in selected:
            legs.update(label.split("-") if category == "listed_spreads" else [label])
        recommendations.append(
            {
                "root": root,
                "category": category,
                "metric": metric,
                "target": target,
                "labels": selected,
                "label_count": len(selected),
                "required_legs": sorted(legs, key=lambda x: int(x[1:])),
                "aggregate_share": captured / total,
                "target_met": captured / total >= target,
                "known_activity_days": positive.height,
                "daily_min_share": daily_share.min(),
                "daily_p05_share": daily_share.quantile(0.05, interpolation="nearest"),
                "days_meeting_target_fraction": (daily_share >= target).mean(),
                "denominator": total,
                "unranked_activity": sum(r["activity"] for r in rows if r["label"] is None),
            }
        )
    return rows, recommendations


def _read(base: Path, table: str, root: str, start: date, end: date) -> pl.DataFrame:
    query = scan(base, table, root).filter(pl.col("date").is_between(start, end))
    columns = ["date", "contract_key", "settlement", *METRICS]
    if table == "daily_spread":
        # Settlement-only calendar catalogs dominate the archive. Activity summaries
        # need traded/statistic rows and only the final date's price quotations.
        query = query.filter(
            (pl.col("kind") == "listed")
            & (
                pl.any_horizontal([pl.col(m).is_not_null() for m in METRICS])
                | (pl.col("date") == end)
            )
        )
        columns.extend(
            [
                "kind",
                "near_key",
                "far_key",
                "near_rank",
                "far_rank",
                "raw_symbol",
                "listed_price_sign",
            ]
        )
    else:
        columns.append("close")
    return query.select(columns).collect()


def _volume_counts(base: Path, table: str, root: str, start: date, end: date) -> dict:
    return (
        scan(base, table, root)
        .filter(pl.col("date").is_between(start, end))
        .select(pl.len().alias("rows"), pl.col("volume").sum().alias("volume"))
        .collect()
        .row(0, named=True)
    )


def read_catalog(base: Path, root: str, start: date, end: date) -> pl.DataFrame:
    query = scan(base, "instruments", root)
    baseline = query.filter(pl.col("date") <= start).select(pl.col("date").max()).collect().item()
    # Earlier snapshots cannot govern any date in this window. Keep the complete
    # preceding snapshot, including unobserved instruments, and all later ones.
    return (
        query.filter(pl.col("date").is_between(baseline or start, end))
        .select(
            "date",
            "contract_key",
            "raw_symbol",
            "instrument_class",
            "activation",
            "expiration",
            "deleted",
            "calendar_structure",
            "near_key",
            "far_key",
            "listed_price_sign",
        )
        .collect()
    )


def front_chain_curve(rows: list[dict]) -> list[dict]:
    ranked = [
        (max(int(c[1:]) for c in r["label"].split("-")), r) for r in rows if r["label"] is not None
    ]
    if not ranked:
        return []
    sample = ranked[0][1]
    return [
        {
            "root": sample["root"],
            "category": sample["category"],
            "metric": sample["metric"],
            "depth": depth,
            "share": sum(r["share"] for rank, r in ranked if rank <= depth),
        }
        for depth in range(1, max(rank for rank, _ in ranked) + 1)
    ]


def series_availability(
    outright: pl.DataFrame, ranks: pl.DataFrame, root: str, session_dates: list[date]
) -> list[dict]:
    """Price-history completeness on observed archive sessions, without zero fills."""
    fields = ["settlement", "cleared_volume", "open_interest", "close"]
    joined = ranks.filter(pl.col("date").is_in(session_dates)).join(
        outright.select("date", "contract_key", *fields),
        on=["date", "contract_key"],
        how="left",
    )
    result = []
    for (rank,), group in joined.partition_by("rank", as_dict=True).items():
        row = {
            "root": root,
            "category": "outrights",
            "label": f"C{rank}",
            "expected_dates": len(session_dates),
            "eligible_dates": group.height,
            "basis": "observed_archive_sessions",
        }
        for field in fields:
            row[field + "_dates"] = group[field].count()
            row[field + "_fraction"] = group[field].count() / len(session_dates)
        result.append(row)
    for near in range(1, 6):
        left = joined.filter(pl.col("rank") == near).select("date", "settlement")
        for far in range(near + 1, 7):
            right = joined.filter(pl.col("rank") == far).select("date", "settlement")
            pair = left.join(right, on="date", suffix="_far")
            if pair.is_empty():
                continue
            count = pair.filter(
                pl.col("settlement").is_not_null() & pl.col("settlement_far").is_not_null()
            ).height
            result.append(
                {
                    "root": root,
                    "category": "synthetic_spreads",
                    "label": f"C{far}-C{near}",
                    "expected_dates": len(session_dates),
                    "eligible_dates": pair.height,
                    "basis": "both_outright_session_settlements",
                    "settlement_dates": count,
                    "settlement_fraction": count / len(session_dates),
                    **{
                        f + suffix: None
                        for f in fields
                        if f != "settlement"
                        for suffix in ("_dates", "_fraction")
                    },
                }
            )
    return result


def analyze_ranks(
    base: Path,
    *,
    start: date | None = None,
    end: date | None = None,
    lookback: int = 60,
    target: float = 0.95,
) -> dict:
    if lookback < 1 or not 0 < target <= 1:
        raise BuildError("lookback must be positive and target must be in (0, 1]")
    metadata = manifest(base)
    quality = scan(base, "source_quality").collect()
    activity_quality = quality.filter(pl.col("schema").is_in(["ohlcv-1d", "statistics"]))
    partial = activity_quality.filter(pl.col("partial_day"))["date"].unique().to_list()
    dates = (
        scan(base, "daily_outright")
        .filter(pl.col("cleared_volume") > 0)
        .select("date")
        .unique()
        .sort("date")
        .collect()["date"]
        .to_list()
    )
    if not dates:
        dates = (
            scan(base, "bars_utc").select("date").unique().sort("date").collect()["date"].to_list()
        )
    complete = [d for d in dates if d not in partial and (end is None or d <= end)]
    if not complete:
        raise BuildError("No complete observed activity dates in requested range")
    end = end or complete[-1]
    start = start or complete[max(0, len(complete) - lookback)]
    if start > end:
        raise BuildError("Analysis start must not follow end")
    targets = tuple(sorted({0.90, 0.95, 0.99, target}))
    ranked, recommendations, contracts, root_quality, availability = [], [], [], [], []
    front_chain = []
    session_dates = [d for d in complete if start <= d <= end]
    for root in metadata["roots"]:
        LOG.info("Analyzing relative contract coverage: %s", root)
        outright = _read(base, "daily_outright", root, start, end)
        spread = _read(base, "daily_spread", root, start, end)
        observed = set(outright["date"].to_list()) | set(spread["date"].to_list())
        days = sorted(observed | set(session_dates) | {end})
        instruments = read_catalog(base, root, start, end)
        ranks = rank_catalog(instruments, days)
        availability.extend(series_availability(outright, ranks, root, session_dates))
        latest = end
        for row in ranks.filter(pl.col("date") == latest).to_dicts():
            contracts.append(
                {
                    "root": root,
                    "category": "outrights",
                    "date": row["date"],
                    "label": f"C{row['rank']}",
                    "symbol": row["symbol"],
                    "near_symbol": None,
                    "far_symbol": None,
                    "listed_price_sign": None,
                    "label_settlement": None,
                }
            )
        for category, data in [("outrights", outright), ("listed_spreads", spread)]:
            labeled = label_activity(data, ranks, category)
            for metric in METRICS:
                rows, chosen = summarize_activity(labeled, root, category, metric, targets)
                ranked.extend(rows)
                front_chain.extend(front_chain_curve(rows))
                recommendations.extend(chosen)
        snapshot = instruments["date"].max()
        snapshot_filter = pl.col("date") == snapshot if snapshot is not None else pl.lit(False)
        listed = (
            instruments.filter(
                snapshot_filter
                & pl.col("calendar_structure").fill_null(False)
                & ~pl.col("deleted")
                & (pl.col("activation") <= end)
                & (pl.col("expiration") >= end)
            )
            .with_columns(
                pl.lit(end).alias("date"),
                pl.lit("listed").alias("kind"),
                pl.lit(None, dtype=pl.UInt16).alias("near_rank"),
                pl.lit(None, dtype=pl.UInt16).alias("far_rank"),
            )
            .join(
                spread.filter((pl.col("date") == end) & (pl.col("kind") == "listed")).select(
                    "contract_key", "settlement"
                ),
                on="contract_key",
                how="left",
            )
        )
        for row in (
            label_activity(listed, ranks, "listed_spreads")
            .filter(pl.col("label").is_not_null())
            .to_dicts()
        ):
            contracts.append(
                {
                    "root": root,
                    "category": "listed_spreads",
                    "date": latest,
                    "label": row["label"],
                    "symbol": row["raw_symbol"],
                    "near_symbol": row["near_symbol"],
                    "far_symbol": row["far_symbol"],
                    "listed_price_sign": row["listed_price_sign"],
                    "label_settlement": row["label_settlement"],
                }
            )
        excluded = _volume_counts(base, "daily_excluded", root, start, end)
        q = quality.filter((pl.col("root") == root) & pl.col("date").is_between(start, end))
        root_quality.append(
            {
                "root": root,
                "observed_dates": len(observed),
                "reference_dates": len(session_dates),
                "latest_observed_date": str(max(observed)) if observed else None,
                "contract_mapping_date": str(end),
                "latest_definition": str(instruments["date"].max()) if instruments.height else None,
                "degraded_source_dates": q.filter(pl.col("condition") == "degraded")[
                    "date"
                ].n_unique(),
                "missing_source_dates": q.filter(pl.col("file_expected") & ~pl.col("file_present"))[
                    "date"
                ].n_unique(),
                "partial_source_dates": q.filter(pl.col("partial_day"))["date"].n_unique(),
                "excluded_rows": excluded["rows"],
                "excluded_utc_volume": int(excluded["volume"]),
            }
        )
    unknown = _volume_counts(base, "daily_unmapped", "__unmapped__", start, end)
    total_bar_volume = (
        scan(base, "bars_utc")
        .filter(pl.col("date").is_between(start, end))
        .select(pl.col("volume").sum())
        .collect()
        .item()
    )
    unknown_volume = int(unknown["volume"])
    result = {
        "start": str(start),
        "end": str(end),
        "target": target,
        "scope": "known mapped outrights and eligible listed calendars; categories separate",
        "selection": "in-sample minimum fixed label count for aggregate activity; no leg costs",
        "rank_basis": "whole eligible as-of outright expiry order; independent of continuous rolls",
        "spread_sign": "C3-C1 means far minus near; listed price multiplied by -listed_price_sign",
        "volume_basis": "volume: UTC-day OHLCV; cleared_volume/open_interest: reference sessions",
        "unknown_volume": unknown_volume,
        "total_utc_bar_volume": total_bar_volume,
        "unknown_volume_fraction": unknown_volume / total_bar_volume if total_bar_volume else None,
        "unmapped_rows": unknown["rows"],
        "archive_last_source_date": str(activity_quality["date"].max()),
        "archive_partial_source_dates": sorted(str(d) for d in partial),
        "root_quality": root_quality,
        "ranked": ranked,
        "recommendations": recommendations,
        "contracts": contracts,
        "availability": availability,
        "front_chain": front_chain,
    }
    output = base / "analysis"
    output.mkdir(exist_ok=True)
    for name, rows in [
        ("rank_coverage", ranked),
        ("recommendations", recommendations),
        ("contracts", contracts),
        ("source_quality", root_quality),
        ("series_availability", availability),
        ("front_chain", front_chain),
    ]:
        # CSV cells contain JSON lists so the selection remains machine-readable.
        flat = [
            {k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()} for row in rows
        ]
        csv = safe_frame(pl.DataFrame(flat, infer_schema_length=None)).write_csv() if flat else ""
        atomic_text(output / f"{name}.csv", csv)
    atomic_text(output / "analysis.json", json.dumps(result, default=str, allow_nan=False))
    from .rank_report import render_report, render_summary

    atomic_text(output / "report.html", render_report(result))
    atomic_text(output / "summary.md", render_summary(result))
    return {
        "status": "ok",
        "report": str(output / "report.html"),
        "start": str(start),
        "end": str(end),
        "roots": len(metadata["roots"]),
        "unmapped_volume_fraction": result["unknown_volume_fraction"],
    }
