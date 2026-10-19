"""Bridge existing futures-ts outputs to the generic daily activity contract.

This is an offline Parquet adapter, not a Databento service client.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import polars as pl

from .schema import normalize


def from_futures_ts(base: Path, start: date, end: date) -> Iterator[pl.DataFrame]:
    # Vendor-specific imports are confined to this adapter; core has no root allowlist.
    from futures_ts.reader import scan_daily
    from futures_ts.storage import manifest, scan

    if start > end:
        raise ValueError("start must not follow end")
    for root in manifest(base)["roots"]:
        for category, table, metric in (
            ("outright", "daily_outright", "cleared_volume"),
            ("listed_spread", "daily_spread", "volume"),
        ):
            columns = ["date", "contract_key", metric]
            columns += (
                ["delivery_month"] if category == "outright" else ["kind", "near_key", "far_key"]
            )
            query = scan_daily(base, root, table=table, start=start, end=end, columns=columns)
            if category == "listed_spread":
                query = query.filter(pl.col("kind") == "listed")
            data = query.collect(engine="streaming")
            if data.is_empty():
                continue
            if category == "outright":
                aliases = (
                    data.filter(pl.col(metric) > 0)
                    .group_by("date", "delivery_month")
                    .agg(pl.col("contract_key").n_unique().alias("identities"))
                )
                if aliases.filter(
                    pl.col("delivery_month").is_not_null() & (pl.col("identities") > 1)
                ).height:
                    raise ValueError(
                        f"{root}: concurrent outright aliases need explicit resolution"
                    )
                data = data.with_columns(
                    pl.coalesce(pl.col("delivery_month"), pl.col("contract_key")).alias(
                        "product_id"
                    )
                )
                identities = ["product_id", "delivery_month"]
            else:
                catalog_query = scan(base, "instruments", root)
                baseline = (
                    catalog_query.filter(pl.col("date") <= start)
                    .select(pl.col("date").max())
                    .collect()
                    .item()
                )
                catalog = (
                    catalog_query.filter(
                        (pl.col("instrument_class") == "F")
                        & pl.col("date").is_between(baseline or start, end)
                    )
                    .select(
                        "date",
                        "contract_key",
                        "delivery_month",
                        "activation",
                        "expiration",
                        "deleted",
                    )
                    .unique()
                    .collect(engine="streaming")
                )
                for side in ("near", "far"):
                    right = catalog.rename(
                        {
                            "date": f"{side}_definition_day",
                            "contract_key": f"{side}_key",
                            "delivery_month": f"{side}_month",
                            "activation": f"{side}_activation",
                            "expiration": f"{side}_expiration",
                            "deleted": f"{side}_deleted",
                        }
                    ).sort(f"{side}_definition_day")
                    if right.select(f"{side}_definition_day", f"{side}_key").is_duplicated().any():
                        raise ValueError(f"{root}: ambiguous as-of leg definitions")
                    data = (
                        data.sort("date")
                        .join_asof(
                            right,
                            left_on="date",
                            right_on=f"{side}_definition_day",
                            by=f"{side}_key",
                            strategy="backward",
                            check_sortedness=False,
                        )
                        .with_columns(
                            pl.when(
                                (pl.col(f"{side}_activation") <= pl.col("date"))
                                & (pl.col(f"{side}_expiration") >= pl.col("date"))
                                & ~pl.col(f"{side}_deleted")
                            )
                            .then(pl.col(f"{side}_month"))
                            .alias(f"{side}_month")
                        )
                    )
                data = data.with_columns(
                    pl.coalesce(
                        pl.concat_str(["near_month", "far_month"], separator="/"),
                        pl.col("contract_key"),
                    ).alias("product_id")
                )
                identities = ["product_id", "near_month", "far_month"]
            # Economic aliases/quote directions aggregate explicitly; all-null stays null.
            data = data.group_by("date", *identities).agg(
                pl.when(pl.col(metric).count() > 0)
                .then(pl.col(metric).cast(pl.Int128).sum())
                .alias("volume")
            )
            constants = {
                "venue": "GLBX",
                "family": root,
                "category": category,
                "source": "databento:GLBX.MDP3",
                "metric": metric,
                "unit": "contracts",
                "date_basis": "reference_session" if category == "outright" else "UTC_day",
            }
            yield normalize(data.lazy(), {"version": 1, "constants": constants}).collect()
