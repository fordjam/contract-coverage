"""Synthetic source-independent optimization, accounting and adapter regressions."""

from datetime import date
from itertools import combinations, product
import json
import random

import polars as pl
import pytest

from product_coverage import analyze, evaluate, normalize
from product_coverage.cli import main
from product_coverage.databento import from_futures_ts
from product_coverage.files import read_policy, scan_files, write_report


def row(product_id="A", volume=99, **kwargs):
    return {
        "date": "2024-01-03",
        "venue": "TEST",
        "family": "ArbitraryFamily",
        "category": "outright",
        "source": "synthetic",
        "metric": "trade_volume",
        "unit": "contracts",
        "date_basis": "exchange_session",
        "product_id": product_id,
        "delivery_month": "2024-02",
        "volume": volume,
        **kwargs,
    }


def run(rows, **kwargs):
    return list(analyze(pl.DataFrame(rows, infer_schema_length=None).lazy(), **kwargs))[0]


@pytest.mark.parametrize(
    "method", ["monthly-calendar", "fixed-products", "fixed-patterns", "daily-oracle"]
)
def test_exact_target_and_minimality_certificate(method):
    result = run(
        [row(pattern="C1"), row("B", 1, delivery_month="2024-03", pattern="C3")], method=method
    )
    assert result.summary["minimum_capacity"] == 1
    assert result.summary["captured"] == 99
    assert result.summary["capacity_minus_one_captured"] == 0
    assert result.summary["target_met"]
    assert not result.summary["full_universe_target_verified"]
    assert run([row()], universe_complete=True).summary["full_universe_target_verified"]


def test_more_than_35_families_and_multiple_venues():
    rows = [row(family=f"Synthetic{i}", venue=venue) for i in range(120) for venue in ("X", "Y")]
    results = list(analyze(pl.DataFrame(rows).lazy()))
    assert len(results) == 240
    assert sum(r.summary["minimum_capacity"] for r in results) == 240


@pytest.mark.parametrize("seed", range(8))
def test_monthly_minimum_matches_exhaustive_independent_subsets(seed):
    rng = random.Random(seed)
    weights = [[rng.randrange(1, 100) for _ in range(3)] for _ in range(2)]
    rows = [
        row(f"{month}-{i}", v, date=f"2024-{month:02d}-03", delivery_month=f"2024-{i + 6:02d}")
        for month, values in enumerate(weights, 1)
        for i, v in enumerate(values)
    ]
    total = sum(map(sum, weights))
    # Exhaust every allowed policy, independently of the sorted-prefix implementation.
    expected = None
    for capacity in range(4):
        choices = list(combinations(range(3), capacity))
        best = max(
            sum(weights[m][i] for m, ids in enumerate(policy) for i in ids)
            for policy in product(choices, repeat=2)
        )
        if best * 100 >= total * 83:
            expected = capacity
            break
    result = run(rows, target="0.83")
    assert result.summary["minimum_capacity"] == expected
    assert result.summary["captured"] * 100 >= total * 83
    assert result.summary["capacity_minus_one_captured"] * 100 < total * 83


def test_seasonal_gaps_and_spread_pairs_are_not_contiguous_chains():
    rows = [
        row("far-dec", 99, delivery_month="2026-12"),
        row("near", 1, delivery_month="2024-02"),
        row("far-jun", 99, date="2024-02-03", delivery_month="2026-06"),
        row("near2", 1, date="2024-02-03", delivery_month="2024-03"),
    ]
    result = run(rows)
    assert result.summary["minimum_capacity"] == 1
    assert set(result.rules["candidate"]) == {"Y2-M12", "Y2-M06"}
    spreads = [
        row("a", 99, category="listed_spread", near_month="2024-03", far_month="2026-12"),
        row("b", 1, category="listed_spread", near_month="2024-04", far_month="2024-05"),
    ]
    assert run(spreads).rules["candidate"].to_list() == ["Y0-M03/Y2-M12"]


def test_objectives_are_distinct_and_aggregate_is_not_every_day():
    rows = [
        row("a", 100, delivery_month="2024-02"),
        row("b", 1, date="2024-01-04", delivery_month="2024-03"),
    ]
    monthly = run(rows)
    assert monthly.summary["minimum_capacity"] == 1
    assert monthly.summary["days_meeting_target"] == 1
    assert monthly.summary["minimum_daily_capture"] == 0
    assert run(rows, method="daily-oracle", target="1").summary["minimum_capacity"] == 1
    assert run(rows, method="fixed-products", target="1").summary["minimum_capacity"] == 2


def test_null_and_unmapped_activity_and_holiday_gaps_remain_explicit():
    rows = [
        row(),
        row("B", None, delivery_month=None),
        row(None, 101, delivery_month=None, date="2024-01-08"),
    ]
    result = run(rows, universe_complete=True)
    assert result.summary["known_volume"] == 200
    assert result.summary["unselectable_known_volume"] == 101
    assert result.summary["minimum_capacity"] is None
    assert result.summary["missing_volume_rows"] == 1
    assert result.summary["denominator_status"] == "known_only"
    assert result.daily.height == 2  # No invented weekend/holiday rows or zeros.
    assert not result.summary["full_universe_target_verified"]


def test_missing_month_metadata_does_not_disappear_from_denominator():
    result = run([row(volume=50), row("B", 50, delivery_month=None)])
    assert result.summary["known_volume"] == 100
    assert result.summary["unselectable_known_volume"] == 50
    assert result.summary["minimum_capacity"] is None
    assert run([row(delivery_month=None)], method="fixed-products").summary["minimum_capacity"] == 1


@pytest.mark.parametrize("volume", [None, 0])
def test_no_known_positive_volume_is_unresolved(volume):
    result = run([row(volume=volume)])
    assert result.summary["minimum_capacity"] is None
    assert result.summary["capture_fraction"] is None
    assert result.summary["positive_volume_dates"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("source", "other"), ("metric", "OI"), ("unit", "notional"), ("date_basis", "UTC_day")],
)
def test_mixed_accounting_is_rejected(field, value):
    with pytest.raises(ValueError, match="Mixed"):
        run([row(), row("B", 1, delivery_month="2024-03", **{field: value})])


@pytest.mark.parametrize("volume", [-1, "NaN", "1.5", "1e4", 2**63, True])
def test_invalid_volume_rejected(volume):
    with pytest.raises((ValueError, pl.exceptions.PolarsError)):
        run([row(volume=volume)])


@pytest.mark.parametrize("target", ["0", "1.01", "NaN", "Infinity"])
def test_invalid_targets_rejected(target):
    with pytest.raises(ValueError):
        run([row()], target=target)


def test_large_integer_sums_and_exact_decimal_threshold():
    large = 2**63 - 1
    rows = [
        row("a", large),
        row("b", large, delivery_month="2024-03"),
        row("c", 1, delivery_month="2024-04"),
    ]
    assert run(rows, target="1").summary["known_volume"] == 2 * large + 1
    assert run(rows, target="1").summary["minimum_capacity"] == 3
    assert run(rows, target="0.999999999999999999").summary["minimum_capacity"] == 2


def test_duplicate_updates_aliases_and_reused_ids_are_rejected():
    with pytest.raises(ValueError, match="Duplicate date/product_id"):
        run([row(), row()])
    with pytest.raises(ValueError, match="Multiple products"):
        run([row(), row("alias", 1)])
    with pytest.raises(ValueError, match="Reused product_id"):
        run([row(), row(date="2025-01-03", delivery_month="2025-02")])
    with pytest.raises(ValueError, match="Multiple products"):
        run(
            [row(pattern="C1"), row("b", 1, delivery_month="2024-03", pattern="C1")],
            method="fixed-patterns",
        )


@pytest.mark.parametrize(
    "column,value",
    [
        ("delivery_month", "2024-13"),
        ("near_month", "2024/01"),
        ("family", " "),
        ("date", None),
        ("category", "synthetic_spread"),
    ],
)
def test_invalid_contract_fields(column, value):
    with pytest.raises(ValueError):
        run([row(**{column: value})])
    with pytest.raises(ValueError, match="precede"):
        run([row(category="listed_spread", near_month="2024-03", far_month="2024-02")])


def test_deterministic_ties():
    rows = [row("b", 50, delivery_month="2024-03"), row("a", 50)]
    assert run(rows, target=".5").rules.equals(run(rows[::-1], target=".5").rules)


def test_frozen_month_rules_roll_year_and_do_not_refit_on_future_volume(tmp_path):
    training = [row(volume=99), row("b", 1, delivery_month="2024-03")]
    fitted = list(analyze(pl.DataFrame(training).lazy()))
    write_report(fitted, tmp_path / "policy", {})
    policy = read_policy(tmp_path / "policy")
    future = [
        row("future-a", 1, date="2025-01-03", delivery_month="2025-02"),
        row("future-b", 99, date="2025-01-03", delivery_month="2025-03"),
    ]
    result = list(evaluate(pl.DataFrame(future).lazy(), policy, start=date(2025, 1, 1)))[0]
    assert result.summary["captured"] == 1
    assert not result.summary["target_met"]
    assert result.summary["optimality"] == "frozen_policy_evaluation"
    assert result.summary["policy_capacity"] == 1
    with pytest.raises(ValueError, match="after"):
        list(evaluate(pl.DataFrame(future).lazy(), policy, start=date(2024, 1, 1)))
    oracle = list(analyze(pl.DataFrame(training).lazy(), method="daily-oracle"))
    with pytest.raises(ValueError, match="repeatable"):
        list(evaluate(pl.DataFrame(future).lazy(), oracle, start=date(2025, 1, 1)))
    with pytest.raises(ValueError, match="accounting"):
        list(
            evaluate(
                pl.DataFrame([row(date="2025-01-03", metric="OI")]).lazy(),
                policy,
                start=date(2025, 1, 1),
            )
        )


def test_unseen_evaluation_family_requires_policy():
    policy = list(analyze(pl.DataFrame([row()]).lazy()))
    with pytest.raises(ValueError, match="No frozen policy"):
        list(
            evaluate(
                pl.DataFrame([row(family="NEW", date="2025-01-03")]).lazy(),
                policy,
                start=date(2025, 1, 1),
            )
        )


def test_missing_evaluation_family_is_not_silently_removed():
    policy = list(analyze(pl.DataFrame([row(), row(family="Other")]).lazy()))
    with pytest.raises(ValueError, match="no evaluation observations"):
        list(
            evaluate(pl.DataFrame([row(date="2025-01-03")]).lazy(), policy, start=date(2025, 1, 1))
        )


def test_csv_mapping_normalize_cli_and_report_roundtrip(tmp_path, capsys):
    export = tmp_path / "approved-export.csv"
    export.write_text(
        "DAY,CONTRACT,MONTH,ACTIVITY\n2024-01-03,ABC-Feb,2024-02,99\n2024-01-03,ABC-Mar,2024-03,1\n"
    )
    constants = {
        c: row()[c]
        for c in ("venue", "family", "category", "source", "metric", "unit", "date_basis")
    }
    mapping = {
        "version": 1,
        "columns": {
            "date": "DAY",
            "product_id": "CONTRACT",
            "delivery_month": "MONTH",
            "volume": "ACTIVITY",
        },
        "constants": constants,
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping))
    parquet = tmp_path / "normalized.parquet"
    assert (
        main(["normalize", "--input", str(export), "--mapping", str(path), "--out", str(parquet)])
        == 0
    )
    out = tmp_path / "report"
    assert main(["analyze", "--input", str(parquet), "--out", str(out)]) == 0
    report = json.loads((out / "report.json").read_text())
    assert report["groups"][0]["minimum_capacity"] == 1
    assert report["provenance"]["inputs"][0]["sha256"]
    assert read_policy(out)[0].rules["candidate"].to_list() == ["Y0-M02"]
    assert (
        scan_files([parquet]).collect().equals(normalize(scan_files([export], mapping)).collect())
    )
    assert main(["analyze", "--input", str(parquet), "--out", str(out)]) == 2
    assert "already exists" in capsys.readouterr().err
    future = tmp_path / "future.csv"
    pl.DataFrame([row("future", 100, date="2025-01-03", delivery_month="2025-02")]).write_csv(
        future
    )
    assert (
        main(
            [
                "evaluate",
                "--input",
                str(future),
                "--policy",
                str(out),
                "--out",
                str(tmp_path / "evaluated"),
                "--start",
                "2025-01-01",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        [],
        {"version": 2},
        {"version": 1, "columns": {"bad": "volume"}},
        {"version": 1, "columns": {"date": "NOT_HERE"}},
        {"version": 1, "constants": {"unit": 1}},
    ],
)
def test_invalid_mapping(mapping):
    with pytest.raises(ValueError):
        normalize(pl.DataFrame([row()]).lazy(), mapping)


def test_multiple_parquet_schemas_preserve_null_columns_and_date_pruning(tmp_path):
    one, two = tmp_path / "one.parquet", tmp_path / "two.parquet"
    pl.DataFrame([row(delivery_month=None)]).with_columns(
        pl.col("date").str.to_date()
    ).write_parquet(one)
    pl.DataFrame([row(family="Second", date="2025-01-03")]).with_columns(
        pl.col("date").str.to_date()
    ).write_parquet(two)
    query = scan_files([one, two])
    assert query.collect_schema()["date"] == pl.Date
    results = list(analyze(query, start=date(2025, 1, 1)))
    assert len(results) == 1
    assert results[0].summary["family"] == "Second"


def test_futures_ts_bridge_retains_missing_volume_and_excludes_synthetic(tmp_path):
    from futures_ts.pipeline import build
    from futures_ts.storage import scan
    from tests.generate_fixtures import batch, example, statistic

    src, out = tmp_path / "src", tmp_path / "built"
    example(src)
    batch(
        src,
        "settlement-only",
        "statistics",
        {"2025-01-15": [statistic(1, 3, 70, day="2025-01-15")]},
    )
    build(src, out, roots=("CL",))
    converted = list(from_futures_ts(out, date(2020, 1, 1), date(2026, 1, 1)))
    assert {frame["category"][0] for frame in converted} == {"outright", "listed_spread"}
    assert converted[0]["volume"].null_count() > 0
    spread = next(frame for frame in converted if frame["category"][0] == "listed_spread")
    expected = (
        scan(out, "daily_spread")
        .filter(pl.col("kind") == "listed")
        .select(pl.col("volume").sum())
        .collect()
        .item()
    )
    assert spread["volume"].cast(pl.Int128).sum() == expected
    results = list(analyze(pl.concat(converted).lazy()))
    assert len(results) == 2
    exported = tmp_path / "canonical"
    assert (
        main(
            [
                "from-futures-ts",
                "--src",
                str(out),
                "--out",
                str(exported),
                "--start",
                "2020-01-01",
                "--end",
                "2026-01-01",
            ]
        )
        == 0
    )
    assert json.loads((exported / "adapter.json").read_text())["source_manifest"][0]["sha256"]
    for path in exported.glob("*.parquet"):
        assert pl.read_parquet_schema(path)["volume"] == pl.Int64
        assert pl.read_parquet_schema(path)["date"] == pl.Date
