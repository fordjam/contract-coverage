"""Prepare a bounded, explicit-basis payload for the standalone analyzer."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from .model import BuildError
from .storage import manifest, scan


def prepare_selection(
    base: Path,
    root: str,
    *,
    analysis_date: date | None = None,
    start_date: date | None = None,
    lookback_sessions: int = 60,
    lifecycle_basis: str | None = None,
    policy: dict | None = None,
    max_instrument_sessions: int = 250_000,
) -> dict:
    """Use cleared volume only; never silently mix UTC bar and session activity.

    The caller explicitly accepts vendor activation/expiration as lifecycle bounds.
    The payload remains incomplete where listed-spread volume or metadata is absent.
    Catalog capture timestamps and historical preliminary vintages are unavailable,
    so this adapter makes no complete-market or point-in-time verification claim.
    """
    metadata = manifest(base)
    if root not in metadata["roots"] or lookback_sessions < 1 or max_instrument_sessions < 1:
        raise BuildError("Select a built root and a positive lookback_sessions")
    if lifecycle_basis != "activation_expiration":
        raise BuildError(
            "Explicit lifecycle_basis='activation_expiration' is required; "
            "last-trading dates are not supplied"
        )
    sessions = sorted(
        scan(base, "root_daily", root).select("date").unique().collect()["date"].to_list()
    )
    if not sessions:
        raise BuildError("No sessions for this root")
    last = analysis_date or sessions[-1]
    if last not in sessions or (start_date and start_date > last):
        raise BuildError("Invalid selection analysis/start date")
    history = [day for day in sessions if day <= last]
    beginning = history[-max(60, lookback_sessions) :][0]
    if start_date:
        beginning = min(beginning, start_date)
    sessions = [day for day in history if day >= beginning]
    dimension_scan = scan(base, "instruments", root).filter(pl.col("date") <= last)
    all_snapshot_dates = sorted(dimension_scan.select("date").unique().collect()["date"].to_list())
    earlier = [stamp for stamp in all_snapshot_dates if stamp <= sessions[0]]
    snapshot_dates = ([earlier[-1]] if earlier else []) + [
        stamp for stamp in all_snapshot_dates if sessions[0] < stamp <= last
    ]
    dimensions = dimension_scan.filter(pl.col("date").is_in(snapshot_dates)).collect()
    snapshots, eligible_keys = [], {}
    instrument_sessions = 0
    for day in sessions:
        prior = [stamp for stamp in snapshot_dates if stamp <= day]
        catalog = (
            dimensions.filter(
                (pl.col("date") == prior[-1])
                & ~pl.col("deleted")
                & (pl.col("activation") <= day)
                & (pl.col("expiration") >= day)
            )
            if prior
            else dimensions.head(0)
        )
        active_keys = catalog.filter(pl.col("instrument_class") == "F")["contract_key"].to_list()
        instruments = []
        for row in catalog.filter(
            (pl.col("instrument_class") == "F")
            | (
                pl.col("calendar_structure")
                & pl.col("near_key").is_in(active_keys)
                & pl.col("far_key").is_in(active_keys)
            )
        ).to_dicts():
            item = {
                "instrument_id": row["contract_key"],
                "product_id": row["raw_symbol"],
                "listing_date": row["activation"].isoformat(),
                "last_trading_date": row["expiration"].isoformat(),
                "expiry_date": row["expiration"].isoformat(),
            }
            if row["instrument_class"] == "F":
                if row["delivery_month"] is None:
                    raise BuildError(
                        "Definition lacks delivery month; the adapter will not infer it from expiry"
                    )
                item.update(kind="outright", delivery_month=row["delivery_month"])
            else:
                item.update(
                    kind="calendar_spread", front_leg_id=row["near_key"], back_leg_id=row["far_key"]
                )
            instruments.append(item)
        eligible_keys[day] = {item["instrument_id"] for item in instruments}
        instrument_sessions += len(instruments)
        if instrument_sessions > max_instrument_sessions:
            raise BuildError(
                "Selection payload exceeds max_instrument_sessions; "
                "use columnar queries or a narrower explicit scope"
            )
        if instruments:
            snapshots.append(
                {"trade_date": day.isoformat(), "complete": True, "instruments": instruments}
            )
    if not snapshots:
        raise BuildError("No eligible catalog for the selected period")
    observations = (
        scan(base, "daily_statistics", root)
        .filter(
            pl.col("date").is_in(sessions)
            & ((pl.col("instrument_class") == "F") | pl.col("calendar_spread"))
        )
        .select(
            "date",
            "contract_key",
            "cleared_volume",
            "open_interest",
            "cleared_volume_available_at",
            "open_interest_available_at",
        )
        .collect()
    )
    daily = []
    for row in observations.to_dicts():
        if row["contract_key"] not in eligible_keys[row["date"]]:
            continue
        item = {
            "trade_date": row["date"].isoformat(),
            "instrument_id": row["contract_key"],
            "volume": row["cleared_volume"],
            "open_interest": row["open_interest"],
            "oi_date": row["date"].isoformat(),
        }
        stamps = [
            row[c]
            for c in ("cleared_volume_available_at", "open_interest_available_at")
            if row[c] is not None
        ]
        if stamps:
            item["available_at"] = datetime.fromtimestamp(
                max(stamps) / 1e9, timezone.utc
            ).isoformat()
        daily.append(item)
    excluded = (
        scan(base, "daily_excluded", root)
        .filter(pl.col("date").is_in(sessions))
        .select(pl.len())
        .collect()
        .item()
    )
    unknown = (
        scan(base, "daily_unmapped")
        .filter(pl.col("date").is_in(sessions))
        .select(pl.len())
        .collect()
        .item()
    )
    result = {
        "exchange": "CME",
        "family_id": root,
        "sessions": [d.isoformat() for d in sessions],
        "volume_basis": "contract_month_total",
        "volume_unit": "contracts",
        "instrument_snapshots": snapshots,
        "daily": daily,
        "analysis_date": last.isoformat(),
        "lookback_sessions": lookback_sessions,
        "metadata": {
            "catalog_complete": False,
            "sessions_complete": metadata["calendar_basis"] == "supplied",
            "reports_complete": False,
            "absent_rows_mean_zero": False,
            "source": "futures-ts",
            "lifecycle_basis": lifecycle_basis,
            "volume_source": "cleared_volume_only",
            "upstream_excluded_rows": excluded,
            "upstream_unattributed_rows": unknown,
            "point_in_time": "unverified_catalog_capture_and_preliminary_vintages",
        },
    }
    if start_date:
        result["start_date"] = start_date.isoformat()
    if policy is not None:
        result["policy"] = deepcopy(policy)
    return result
