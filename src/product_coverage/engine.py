"""Exact independent equal-slot objectives, processed one family/category at a time."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

import polars as pl

from .schema import ACCOUNTING, GROUP, METHODS, REQUIRED, normalize
from .policy import validate_policy


@dataclass
class AnalysisResult:
    summary: dict
    rules: pl.DataFrame
    curve: pl.DataFrame
    daily: pl.DataFrame


def target_fraction(target: str) -> Fraction:
    value = Fraction(str(target))
    if not 0 < value <= 1:
        raise ValueError("target must be greater than 0 and at most 1")
    return value


def groups(query: pl.LazyFrame, start: date | None, end: date | None) -> Iterator[pl.DataFrame]:
    if start and end and start > end:
        raise ValueError("start must not follow end")
    query = normalize(query)
    if start:
        query = query.filter(pl.col("date") >= start)
    if end:
        query = query.filter(pl.col("date") <= end)
    bad = query.filter(
        pl.any_horizontal([pl.col(c).is_null() for c in REQUIRED if c != "volume"])
    ).limit(1)
    if bad.collect(engine="streaming").height:
        raise ValueError("Null date, grouping or accounting field")
    keys = query.select(GROUP).unique().sort(GROUP).collect(engine="streaming")
    if keys.is_empty():
        raise ValueError("No observations in the selected window")
    for key in keys.iter_rows(named=True):
        selected = query.filter(pl.all_horizontal([pl.col(c) == key[c] for c in GROUP]))
        yield validate(selected.collect(engine="streaming"))


def validate(data: pl.DataFrame) -> pl.DataFrame:
    """Reject ambiguous accounting, uncollapsed updates and unsafe identities."""
    for column in [*GROUP, *ACCOUNTING]:
        if data.filter(pl.col(column).str.strip_chars().str.len_chars() == 0).height:
            raise ValueError(f"Empty {column}")
    if data["category"].unique().to_list() not in (["outright"], ["listed_spread"]):
        raise ValueError("category must be outright or listed_spread")
    if data.select(ACCOUNTING).unique().height != 1:
        raise ValueError("Mixed sources, metrics, units or date bases within a group")
    bad = data.filter(pl.col("volume").is_not_null() & ~pl.col("volume").str.contains(r"^[0-9]+$"))
    if bad.height:
        raise ValueError("volume must be a nonnegative integer or null; no floats or NaN")
    # Int128 aggregation prevents UInt64 overflow; each observation is signed-64 bounded.
    data = data.with_columns(pl.col("volume").cast(pl.Int128))
    if data.filter(pl.col("volume") > 2**63 - 1).height:
        raise ValueError("Per-observation volume exceeds signed 64-bit range")
    identified = data.filter(pl.col("product_id").is_not_null())
    if identified.select("date", "product_id").is_duplicated().any():
        raise ValueError("Duplicate date/product_id; resolve updates or aliases upstream")
    for column in ("delivery_month", "near_month", "far_month"):
        if data.filter(
            pl.col(column).is_not_null()
            & ~pl.col(column).str.contains(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
        ).height:
            raise ValueError(f"Invalid YYYY-MM {column}")
        if (
            identified.group_by("product_id")
            .agg(pl.col(column).drop_nulls().n_unique())
            .filter(pl.col(column) > 1)
            .height
        ):
            raise ValueError("Reused product_id changes delivery identity; resolve as-of upstream")
    if data.filter(pl.col("near_month") >= pl.col("far_month")).height:
        raise ValueError("Calendar spread near_month must precede far_month")
    return data


def candidates(data: pl.DataFrame, method: str) -> pl.DataFrame:
    """Assign a state and candidate label without consulting activity ranks."""
    data = data.filter((pl.col("volume") > 0) & pl.col("product_id").is_not_null())
    if method == "fixed-products" or method == "daily-oracle":
        label = pl.col("product_id")
    elif method == "fixed-patterns":
        label = pl.col("pattern")
    elif data.height and data["category"][0] == "listed_spread":
        label = pl.concat_str([month_label("near_month"), month_label("far_month")], separator="/")
    else:
        label = month_label("delivery_month")
    state = (
        pl.col("date").dt.month().cast(pl.String)
        if method == "monthly-calendar"
        else pl.col("date").cast(pl.String)
        if method == "daily-oracle"
        else pl.lit("all")
    )
    data = data.with_columns(label.alias("candidate"), state.alias("state")).filter(
        pl.col("candidate").is_not_null()
    )
    # A slot is one economic product. Aliases must be resolved before this engine.
    if data.select("date", "candidate").is_duplicated().any():
        raise ValueError("Multiple products occupy one date/pattern; canonicalize aliases upstream")
    return data


def month_label(column: str) -> pl.Expr:
    return pl.concat_str(
        [
            pl.lit("Y"),
            (pl.col(column).str.slice(0, 4).cast(pl.Int32) - pl.col("date").dt.year()).cast(
                pl.String
            ),
            pl.lit("-M"),
            pl.col(column).str.slice(5, 2),
        ]
    )


def curve_for(eligible: pl.DataFrame, total: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    weights = (
        eligible.group_by("state", "candidate")
        .agg(pl.col("volume").sum())
        .sort(["state", "volume", "candidate"], descending=[False, True, False])
        .with_columns(pl.col("candidate").cum_count().over("state").alias("priority"))
    )
    margins = weights.group_by("priority").agg(pl.col("volume").sum()).sort("priority")
    rows = [{"capacity": 0, "captured": 0, "known_volume": total}]
    captured = 0
    for priority, volume in margins.iter_rows():
        captured += int(volume)
        rows.append({"capacity": priority, "captured": captured, "known_volume": total})
    return weights, pl.DataFrame(
        rows, schema_overrides={"captured": pl.Int128, "known_volume": pl.Int128}
    )


def audit(
    data: pl.DataFrame, eligible: pl.DataFrame, rules: pl.DataFrame, target: Fraction
) -> pl.DataFrame:
    totals = data.group_by("date").agg(
        pl.col("volume").sum().alias("known_volume"),
        pl.col("volume").null_count().alias("missing_volume_rows"),
    )
    selected = eligible.join(rules.select("state", "candidate"), on=["state", "candidate"])
    daily = (
        totals.join(
            selected.group_by("date").agg(
                pl.col("volume").sum().alias("captured"), pl.len().alias("selected_products")
            ),
            on="date",
            how="left",
        )
        .with_columns(pl.col("captured", "selected_products").fill_null(0))
        .sort("date")
    )
    # Python integer comparisons keep even very large decimal targets exact.
    daily = daily.with_columns(
        pl.Series(
            "meets_target",
            [
                int(captured) * target.denominator >= int(total) * target.numerator
                if total > 0
                else None
                for total, captured in daily.select("known_volume", "captured").iter_rows()
            ],
            dtype=pl.Boolean,
        )
    )
    return daily


def summarize(
    data: pl.DataFrame,
    eligible: pl.DataFrame,
    rules: pl.DataFrame,
    daily: pl.DataFrame,
    method: str,
    target: Fraction,
    capacity: int | None,
    before: int | None,
    universe_complete: bool,
) -> dict:
    total, captured = int(data["volume"].sum()), int(daily["captured"].sum())
    missing = data["volume"].null_count()
    achieved = total > 0 and captured * target.denominator >= total * target.numerator
    trading = daily.filter(pl.col("known_volume") > 0)
    return {
        **data.select(*GROUP, *ACCOUNTING).row(0, named=True),
        "method": method,
        "start": str(data["date"].min()),
        "end": str(data["date"].max()),
        "target": str(target),
        "minimum_capacity": capacity,
        "target_met": achieved,
        "known_volume": total,
        "captured": captured,
        "capture_fraction": captured / total if total else None,
        "capacity_minus_one_captured": before,
        "unselectable_known_volume": total - int(eligible["volume"].sum()),
        "missing_volume_rows": missing,
        "unidentified_rows": data["product_id"].null_count(),
        "universe_complete_asserted": universe_complete,
        "denominator_status": "complete_asserted"
        if universe_complete and not missing
        else "known_only",
        "full_universe_target_verified": achieved and universe_complete and not missing,
        "selected_rules": rules.height,
        "observed_dates": daily.height,
        "positive_volume_dates": trading.height,
        "days_meeting_target": int(trading["meets_target"].sum()),
        "minimum_daily_capture": (
            trading.select((pl.col("captured") / pl.col("known_volume")).min()).item()
            if trading.height
            else None
        ),
        "peak_observed_selected_products": int(daily["selected_products"].max()),
    }


def analyze(
    query: pl.LazyFrame,
    *,
    method: str = "monthly-calendar",
    target: str = "0.99",
    start: date | None = None,
    end: date | None = None,
    universe_complete: bool = False,
) -> Iterator[AnalysisResult]:
    """Yield one exact in-sample result per venue/family/category, with no root limit."""
    if method not in METHODS:
        raise ValueError(f"Unknown method: {method}")
    fraction = target_fraction(target)
    for data in groups(query, start, end):
        total = int(data["volume"].sum())
        eligible = candidates(data, method)
        weights, curve = curve_for(eligible, total)
        chosen = next(
            (
                row
                for row in curve.iter_rows(named=True)
                if total > 0
                and int(row["captured"]) * fraction.denominator >= total * fraction.numerator
            ),
            None,
        )
        capacity = int(chosen["capacity"]) if chosen else None
        # Unresolved objectives produce a curve and diagnostics, never a recommendation.
        rules = weights.filter(pl.col("priority") <= capacity) if capacity else weights.head(0)
        daily = audit(data, eligible, rules, fraction)
        before = int(curve["captured"][capacity - 1]) if capacity else None
        summary = summarize(
            data, eligible, rules, daily, method, fraction, capacity, before, universe_complete
        )
        summary["optimality"] = "exact_within_stated_policy" if chosen else "unresolved"
        yield AnalysisResult(summary, rules, curve, daily)


def evaluate(
    query: pl.LazyFrame,
    policy: list[AnalysisResult],
    *,
    start: date,
    end: date | None = None,
    universe_complete: bool = False,
) -> Iterator[AnalysisResult]:
    """Apply frozen rules to a strictly later window; never retune on test volume."""
    lookup = {}
    for fitted in policy:
        summary = fitted.summary
        rules = validate_policy(fitted)
        if start <= date.fromisoformat(summary["end"]):
            raise ValueError("Evaluation must start after every policy training end")
        key = tuple(summary[c] for c in GROUP)
        if key in lookup:
            raise ValueError("Duplicate policy group")
        lookup[key] = AnalysisResult(summary, rules, fitted.curve, fitted.daily)
    if not lookup:
        raise ValueError("Empty policy")
    seen = set()
    for data in groups(query, start, end):
        key = tuple(data[c][0] for c in GROUP)
        if key not in lookup:
            raise ValueError(f"No frozen policy for group {key}; fit the new family explicitly")
        fitted = lookup[key]
        seen.add(key)
        if any(data[c][0] != fitted.summary[c] for c in ACCOUNTING):
            raise ValueError("Evaluation accounting differs from training")
        method, target = fitted.summary["method"], target_fraction(fitted.summary["target"])
        eligible = candidates(data, method)
        daily = audit(data, eligible, fitted.rules, target)
        if daily.filter(pl.col("selected_products") > fitted.summary["minimum_capacity"]).height:
            raise ValueError("Policy evaluation exceeds declared capacity")
        summary = summarize(
            data, eligible, fitted.rules, daily, method, target, None, None, universe_complete
        )
        summary.update(
            optimality="frozen_policy_evaluation",
            policy_capacity=fitted.summary["minimum_capacity"],
            training_start=fitted.summary["start"],
            training_end=fitted.summary["end"],
        )
        yield AnalysisResult(summary, fitted.rules, fitted.curve.head(0), daily)
    if set(lookup) - seen:
        raise ValueError(
            "Training groups have no evaluation observations; supply all policy groups"
        )
