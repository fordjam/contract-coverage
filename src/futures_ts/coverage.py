"""Coverage evidence without treating unknown instruments or days as zeros."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import polars as pl

from .storage import manifest, scan


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def coverage(base: Path) -> dict:
    metadata = manifest(base)
    quality = scan(base, "source_quality").collect()
    quality_groups = {
        key: group
        for key, group in quality.with_columns(pl.col("date").dt.year().alias("year"))
        .partition_by(["root", "year"], as_dict=True)
        .items()
    }
    years = sorted(set(quality["date"].dt.year().to_list()))
    if not years:
        years = sorted(
            {
                int(p.name[5:])
                for table in ("daily_outright", "daily_spread", "daily_unmapped")
                for p in (base / table).glob("root=*/year=*")
            }
        )
    rows = []
    summaries = {
        kind: {"rows": 0, "settlement": 0, "cleared_volume": 0, "open_interest": 0}
        for kind in ("outrights", "listed_spreads")
    }
    for root in metadata["roots"]:
        for year in years:
            row = {"root": root, "year": year, "calendar_basis": metadata["calendar_basis"]}
            stats = scan(base, "daily_statistics", root, year)
            row["observed_session_days"] = stats.select(pl.col("date").n_unique()).collect().item()
            row["utc_bar_days"] = (
                scan(base, "bars_utc", root, year)
                .select(pl.col("date").n_unique())
                .collect()
                .item()
            )
            for kind, table in [
                ("outrights", "daily_outright"),
                ("listed_spreads", "daily_spread"),
            ]:
                data = scan(base, table, root, year)
                if kind == "listed_spreads":
                    data = data.filter(pl.col("kind") == "listed")
                counts = (
                    data.select(
                        pl.len().alias("rows"),
                        pl.col("contract_key").n_unique().alias("contracts"),
                        *[
                            pl.col(m).count().alias(m)
                            for m in ("settlement", "cleared_volume", "open_interest")
                        ],
                    )
                    .collect()
                    .row(0, named=True)
                )
                row[kind + "_rows"], row[kind + "_contracts"] = counts["rows"], counts["contracts"]
                summaries[kind]["rows"] += counts["rows"]
                for metric in ("settlement", "cleared_volume", "open_interest"):
                    summaries[kind][metric] += counts[metric]
                    row[kind + "_" + metric + "_pct"] = (
                        round(100 * counts[metric] / counts["rows"], 2) if counts["rows"] else None
                    )
            row["synthetic_spread_rows"] = (
                scan(base, "daily_spread", root, year)
                .filter(pl.col("kind") == "synthetic")
                .select(pl.len())
                .collect()
                .item()
            )
            row["excluded_rows"] = (
                scan(base, "daily_excluded", root, year).select(pl.len()).collect().item()
            )
            row["excluded_definition_versions"] = (
                scan(base, "instruments", root, year)
                .filter(
                    pl.col("exclusion_reason").is_not_null()
                    & ~pl.col("calendar_structure").fill_null(False)
                )
                .select(pl.len())
                .collect()
                .item()
            )
            row["unsupported_settlement_flag_rows"] = (
                stats.filter(pl.col("settlement_flags_supported").not_())
                .select(pl.len())
                .collect()
                .item()
            )
            activity = pl.concat(
                [
                    scan(base, "daily_outright", root, year).select("adv_volume_basis"),
                    scan(base, "daily_spread", root, year)
                    .filter(pl.col("kind") == "listed")
                    .select("adv_volume_basis"),
                ]
            )
            for value, label in [
                ("cleared_volume", "adv_cleared_volume_rows"),
                ("ohlcv_utc_fallback", "adv_ohlcv_fallback_rows"),
                ("missing", "adv_missing_rows"),
            ]:
                row[label] = (
                    activity.filter(pl.col("adv_volume_basis") == value)
                    .select(pl.len())
                    .collect()
                    .item()
                )
            q = quality_groups.get((root, year), quality.head(0))
            row["degraded_source_days"] = q.filter(pl.col("condition") == "degraded")[
                "date"
            ].n_unique()
            row["missing_condition_days"] = q.filter(pl.col("condition") == "missing")[
                "date"
            ].n_unique()
            row["missing_expected_file_days"] = q.filter(
                pl.col("file_expected") & ~pl.col("file_present")
            )["date"].n_unique()
            row["partial_source_days"] = q.filter(pl.col("partial_day"))["date"].n_unique()
            row["unmapped_ids"] = None
            row["source_status"] = "present" if q.height else "no_selected_source"
            rows.append(row)
    unmapped = (
        scan(base, "daily_unmapped")
        .group_by("publisher_id", "instrument_id")
        .agg(
            pl.col("date").min().alias("first_seen"),
            pl.col("date").max().alias("last_seen"),
            pl.len().alias("rows"),
            pl.col("mapping_reason").first(),
        )
        .sort("publisher_id", "instrument_id")
        .collect()
    )
    for (year,), group in (
        scan(base, "daily_unmapped")
        .with_columns(pl.col("date").dt.year().alias("year"))
        .collect()
        .partition_by("year", as_dict=True)
        .items()
    ):
        rows.append(
            {
                "root": "__unmapped__",
                "year": year,
                "unmapped_ids": group.select(
                    pl.struct("publisher_id", "instrument_id").n_unique()
                ).item(),
                "source_status": "unattributed",
            }
        )
    fields = list(rows[0]) if rows else ["root", "year"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(base / "coverage.csv", buffer.getvalue())
    atomic_text(base / "unmapped.csv", unmapped.write_csv())
    lines = [
        "# Futures data coverage",
        "",
        "Counts describe retained observations, not exchange-wide completeness.",
        "",
    ]
    for kind, values in summaries.items():
        lines.append(
            f"- {kind.replace('_', ' ').capitalize()}: {values['rows']} observed rows; "
            f"OI on {values['open_interest']}, cleared volume on {values['cleared_volume']}, "
            f"settlement on {values['settlement']}."
        )
    cleared = sum(r.get("adv_cleared_volume_rows", 0) for r in rows)
    fallback = sum(r.get("adv_ohlcv_fallback_rows", 0) for r in rows)
    missing = sum(r.get("adv_missing_rows", 0) for r in rows)
    unsupported = sum(r.get("unsupported_settlement_flag_rows", 0) for r in rows)
    lines += [
        f"- Unmapped vendor IDs: {unmapped.height}. "
        "They cannot be attributed to roots; see unmapped.csv.",
        "- ADV: cleared volume first; explicitly flagged UTC OHLCV volume fallback. "
        "Known observations only; missing days are not zero.",
        f"- ADV samples: {cleared} cleared; {fallback} UTC fallback; {missing} missing.",
        f"- Unsupported settlement flags: {unsupported} rows; "
        "prices remain null and raw flags are retained.",
        f"- Session calendar: {metadata['calendar_basis']}. "
        "Observed calendars do not establish full trading-day coverage.",
        "- UTC bars and session statistics have different intervals. "
        "Source quality dates are UTC file dates.",
        "- Root totals separate outrights from listed spreads; "
        "synthetic spreads contribute no OI or volume.",
        "",
        "| Root | Year | Session dates observed | Outright rows | Listed spread rows "
        "| Degraded source days | Partial source days |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                str(row.get(k, ""))
                for k in (
                    "root",
                    "year",
                    "observed_session_days",
                    "outrights_rows",
                    "listed_spreads_rows",
                    "degraded_source_days",
                    "partial_source_days",
                )
            )
            + " |"
        )
    excluded = (
        scan(base, "daily_excluded")
        .group_by("exclusion_reason")
        .agg(pl.len().alias("rows"))
        .collect()
    )
    if excluded.height:
        lines += ["", "Excluded observations:"]
        lines.extend(f"- {r['exclusion_reason']}: {r['rows']} rows" for r in excluded.to_dicts())
    definitions = (
        scan(base, "instruments")
        .filter(
            pl.col("exclusion_reason").is_not_null()
            & ~pl.col("calendar_structure").fill_null(False)
        )
        .group_by("exclusion_reason")
        .agg(pl.len().alias("versions"))
        .collect()
    )
    if definitions.height:
        lines += ["", "Excluded definition versions (including instruments with no observations):"]
        lines.extend(
            f"- {r['exclusion_reason']}: {r['versions']} versions" for r in definitions.to_dicts()
        )
    atomic_text(base / "coverage.md", "\n".join(lines) + "\n")
    return {"summary": summaries, "unmapped_ids": unmapped.height, "root_years": len(rows)}
