"""Shared types and date/identity rules for offline daily futures data."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

ROOTS = tuple(
    (
        "CL NG RB HO GC SI HG PL PA ZC ZS ZW KE ZL ZM LE HE GF ES NQ RTY YM "
        "ZT ZF ZN ZB UB 6E 6J 6B 6A 6C 6S 6N 6M"
    ).split()
)
UNDEF_TS = 2**64 - 1
UNDEF_INT = 2**63 - 1
PRICE_SCALE = 1_000_000_000
CT = ZoneInfo("America/Chicago")


class BuildError(ValueError):
    """Source or output evidence cannot be interpreted without guessing."""


def text(value: object) -> str:
    return value.decode("utf-8").rstrip("\0") if isinstance(value, bytes) else str(value)


def utc_date(ns: int) -> date:
    if ns in (0, UNDEF_TS):
        raise BuildError("Missing event timestamp")
    return datetime.fromtimestamp(ns // PRICE_SCALE, timezone.utc).date()


def optional_date(ns: int) -> date | None:
    return None if ns in (0, UNDEF_TS) else utc_date(ns)


def session_date(ts_ref: int, ts_event: int) -> tuple[date, str]:
    utc_date(ts_event)
    if ts_ref not in (0, UNDEF_TS):
        return utc_date(ts_ref), "ts_ref"
    stamp = datetime.fromtimestamp(ts_event // PRICE_SCALE, timezone.utc).astimezone(CT)
    return stamp.date() + timedelta(days=stamp.time() >= time(17)), "event_17ct"


def session_open(day: date) -> int:
    stamp = datetime.combine(day - timedelta(days=1), time(17), CT)
    return int(stamp.timestamp()) * PRICE_SCALE


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:32]


def price(value: int) -> float | None:
    return None if value == UNDEF_INT else value / PRICE_SCALE


IDENTITY = {
    "date": pl.Date,
    "root": pl.String,
    "contract_key": pl.String,
    "publisher_id": pl.UInt16,
    "instrument_id": pl.UInt32,
    "raw_symbol": pl.String,
    "instrument_class": pl.String,
    "expiration": pl.Date,
    "activation": pl.Date,
    "definition_date": pl.Date,
    "definition_version": pl.String,
    "mapping_reason": pl.String,
    "calendar_spread": pl.Boolean,
    "exclusion_reason": pl.String,
    "calendar_structure": pl.Boolean,
    "near_key": pl.String,
    "far_key": pl.String,
    "delivery_month": pl.String,
}
INSTRUMENT = {**IDENTITY, "deleted": pl.Boolean, "first_seen": pl.Date, "last_seen": pl.Date}
LEG = {
    "date": pl.Date,
    "root": pl.String,
    "contract_key": pl.String,
    "definition_version": pl.String,
    "leg_index": pl.UInt16,
    "leg_instrument_id": pl.UInt32,
    "leg_raw_symbol": pl.String,
    "leg_side": pl.String,
    "ratio_numerator": pl.Int32,
    "ratio_denominator": pl.Int32,
}
BAR = {
    **IDENTITY,
    "bar_date_utc": pl.Date,
    **{c + "_nanos": pl.Int64 for c in ("open", "high", "low", "close")},
    "volume": pl.UInt64,
    "source_batch": pl.String,
    "source_day": pl.Date,
}
EVENT = {
    **IDENTITY,
    "stat_type": pl.UInt16,
    "price_nanos": pl.Int64,
    "quantity": pl.Int64,
    "ts_event": pl.UInt64,
    "ts_recv": pl.UInt64,
    "sequence": pl.UInt32,
    "channel_id": pl.UInt16,
    "update_action": pl.UInt8,
    "stat_flags": pl.UInt8,
    "date_basis": pl.String,
    "source_batch": pl.String,
    "source_day": pl.Date,
}
STAT_FIELDS = {}
for _name in ("settlement", "cleared_volume", "open_interest"):
    STAT_FIELDS.update(
        {
            _name: pl.Float64 if _name == "settlement" else pl.UInt64,
            _name + "_ts_event": pl.UInt64,
            _name + "_available_at": pl.UInt64,
            _name + "_flags": pl.UInt8,
            _name + "_deleted": pl.Boolean,
            _name + "_date_basis": pl.String,
        }
    )
STAT = {
    **IDENTITY,
    **STAT_FIELDS,
    "settlement_nanos": pl.Int64,
    "settlement_final": pl.Boolean,
    "settlement_actual": pl.Boolean,
    "settlement_intraday": pl.Boolean,
    "settlement_flags_supported": pl.Boolean,
}
DAILY = {
    **STAT,
    "bar_date_utc": pl.Date,
    "ohlcv_basis": pl.String,
    **{c: pl.Float64 for c in ("open", "high", "low", "close")},
    "volume": pl.UInt64,
    "days_to_expiry": pl.Int32,
    "expiry_rank": pl.UInt16,
    "adv_volume": pl.UInt64,
    "adv_volume_basis": pl.String,
    "adv20": pl.Float64,
    "adv60": pl.Float64,
    "adv20_samples": pl.UInt16,
    "adv60_samples": pl.UInt16,
    "adv20_sessions": pl.UInt16,
    "adv60_sessions": pl.UInt16,
    "adv20_partial": pl.Boolean,
    "adv60_partial": pl.Boolean,
    "calendar_basis": pl.String,
}
SPREAD = {**DAILY, "kind": pl.String, "near_rank": pl.UInt16, "far_rank": pl.UInt16}
TOTAL = {
    "date": pl.Date,
    "root": pl.String,
    "instrument_class": pl.String,
    "open_interest": pl.UInt64,
    "cleared_volume": pl.UInt64,
    "open_interest_rows": pl.UInt32,
    "cleared_volume_rows": pl.UInt32,
    "observed_rows": pl.UInt32,
    "open_interest_complete": pl.Boolean,
    "expected_instruments": pl.UInt32,
    "cleared_volume_complete": pl.Boolean,
    "adv_volume": pl.UInt64,
    "adv_volume_basis": pl.String,
    **{k: v for k, v in DAILY.items() if k.startswith("adv20") or k.startswith("adv60")},
    "calendar_basis": pl.String,
}
CONTINUOUS = {
    "date": pl.Date,
    "root": pl.String,
    "rank": pl.UInt8,
    "contract_key": pl.String,
    "raw_symbol": pl.String,
    "settlement": pl.Float64,
    "difference_adjusted": pl.Float64,
    "ratio_adjusted": pl.Float64,
    "difference_valid": pl.Boolean,
    "ratio_valid": pl.Boolean,
    "settlement_final": pl.Boolean,
    "settlement_actual": pl.Boolean,
    "settlement_intraday": pl.Boolean,
    "settlement_available_at": pl.UInt64,
}
ROLL = {
    "date": pl.Date,
    "root": pl.String,
    "rank": pl.UInt8,
    "old_key": pl.String,
    "new_key": pl.String,
    "reason": pl.String,
    "old_settlement": pl.Float64,
    "new_settlement": pl.Float64,
    "difference_gap": pl.Float64,
    "ratio": pl.Float64,
    "oi_cutoff": pl.UInt64,
    "current_oi": pl.UInt64,
    "next_oi": pl.UInt64,
    "oi_current_key": pl.String,
    "oi_next_key": pl.String,
    "oi_reference_date": pl.Date,
    "oi_current_available_at": pl.UInt64,
    "oi_next_available_at": pl.UInt64,
}
QUALITY = {
    "date": pl.Date,
    "root": pl.String,
    "schema": pl.String,
    "batch": pl.String,
    "condition": pl.String,
    "file_present": pl.Boolean,
    "partial_day": pl.Boolean,
    "file_expected": pl.Boolean,
    "date_basis": pl.String,
}
SCHEMAS = {
    "instruments": INSTRUMENT,
    "instrument_legs": LEG,
    "bars_utc": BAR,
    "statistics_events": EVENT,
    "daily_statistics": STAT,
    "daily_outright": DAILY,
    "daily_spread": SPREAD,
    "root_daily": TOTAL,
    "continuous": CONTINUOUS,
    "roll_dates": ROLL,
    "source_quality": QUALITY,
    "daily_unmapped": DAILY,
    "daily_excluded": DAILY,
}


def frame(rows: list[dict], schema: dict) -> pl.DataFrame:
    return pl.from_dicts(rows, schema=schema, strict=True) if rows else pl.DataFrame(schema=schema)
