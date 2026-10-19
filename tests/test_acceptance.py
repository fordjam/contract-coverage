from __future__ import annotations

import csv
import json
from datetime import date, timedelta

import polars as pl
import pytest

from contract_coverage import analyze
from futures_ts.cli import main
from futures_ts.definitions import Definitions
from futures_ts.model import BuildError
from futures_ts.pipeline import build
from futures_ts.selection import prepare_selection
from futures_ts.sources import discover
from futures_ts.storage import scan
from tests.generate_fixtures import batch, definition, example, statistic


def test_id_reuse_survives_selection_adapter_without_pooling(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(
        src,
        "old-def",
        "definition",
        {
            "2020-01-01": [
                definition(
                    1, "CL-OLD", day="2020-01-01", activation="2019-01-01", expiry="2020-06-20"
                )
            ]
        },
    )
    batch(src, "new-def", "definition", {"2025-01-01": [definition(1, "CL-NEW")]})
    batch(src, "old-stat", "statistics", {"2020-01-02": [statistic(1, 6, 100, day="2020-01-02")]})
    batch(src, "new-stat", "statistics", {"2025-01-02": [statistic(1, 6, 200)]})
    build(src, out, roots=("CL",))
    with pytest.raises(BuildError, match="lifecycle_basis"):
        prepare_selection(out, "CL")
    payload = prepare_selection(out, "CL", lifecycle_basis="activation_expiration")
    assert len({row["instrument_id"] for row in payload["daily"]}) == 2
    result = analyze(payload)
    assert sorted(row["adv"] for row in result["instrument_metrics"].values()) == [100, 200]
    assert not payload["metadata"]["catalog_complete"]


def test_selection_does_not_substitute_utc_volume_for_spread_cleared_volume(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    payload = prepare_selection(out, "CL", lifecycle_basis="activation_expiration")
    result = analyze(payload)
    assert result["category_denominators"]["spreads"]["known_instrument_sessions"] == 0
    assert payload["metadata"]["upstream_unattributed_rows"] == 1


def test_root_filter_does_not_turn_known_other_roots_into_unknowns(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(
        src,
        "defs",
        "definition",
        {"2025-01-01": [definition(1, "CL-A"), definition(2, "NG-A", root="NG")]},
        roots=("CL", "NG"),
    )
    batch(
        src,
        "stats",
        "statistics",
        {"2025-01-02": [statistic(1, 6, 100), statistic(2, 6, 200)]},
        roots=("CL", "NG"),
    )
    build(src, out, roots=("CL",))
    assert scan(out, "daily_outright").select(pl.len()).collect().item() == 1
    assert scan(out, "daily_unmapped").select(pl.len()).collect().item() == 0


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("uds", "user_defined_or_unknown"),
        ("butterfly", "not_two_complete_legs"),
        ("cross", "cross_root_or_nonfuture_leg"),
        ("ratio", "not_one_for_one"),
    ],
)
def test_spread_exclusion_reasons(tmp_path, kind, reason):
    rows = [
        definition(1, "A", expiry="2025-03-20"),
        definition(2, "B", root="NG" if kind == "cross" else "CL"),
    ]
    for index, instrument, symbol, side in [(0, 1, "A", "B"), (1, 2, "B", "A")]:
        rows.append(
            definition(
                3,
                "A-B",
                leg_count=3 if kind == "butterfly" else 2,
                leg=(index, instrument, symbol, side),
                uds=kind == "uds",
                ratio=2 if kind == "ratio" else 1,
            )
        )
    batch(tmp_path, "defs", "definition", {"2025-01-01": rows}, roots=("CL", "NG"))
    catalog = Definitions(discover(tmp_path, ("CL", "NG"))).catalog(date(2025, 1, 2))
    assert not catalog[1, 3]["calendar_spread"]
    assert catalog[1, 3]["exclusion_reason"] == reason


def test_future_and_deleted_definitions_do_not_map_backwards(tmp_path):
    batch(tmp_path, "defs", "definition", {"2025-01-03": [definition(1, "A", day="2025-01-03")]})
    resolver = Definitions(discover(tmp_path, ("CL",)))
    assert not resolver.catalog(date(2025, 1, 2))
    batch(
        tmp_path,
        "deleted",
        "definition",
        {"2025-01-06": [definition(1, "A", day="2025-01-06", action="D")]},
    )
    resolver = Definitions(discover(tmp_path, ("CL",)))
    assert not resolver.catalog(date(2025, 1, 6))


def test_adv_window_and_history_cross_year_without_filling_holidays(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(src, "defs", "definition", {"2024-12-01": [definition(1, "A", day="2024-12-01")]})
    days = [date(2024, 12, 10) + timedelta(days=n) for n in range(43)]
    days = [
        d for d in days if d.weekday() < 5 and d.isoformat() not in ("2024-12-25", "2025-01-01")
    ]
    stats = {}
    missing = days[-10]
    for n, day in enumerate(days):
        label = day.isoformat()
        stats[label] = [statistic(1, 3, 70, day=label)]
        if day != missing:
            stats[label].append(statistic(1, 6, n * 10, day=label))
    batch(src, "stats", "statistics", stats)
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("root,date\n" + "".join(f"CL,{d}\n" for d in days))
    build(src, out, roots=("CL",), calendar=calendar, start=date(2025, 1, 1))
    last = scan(out, "daily_outright").sort("date").collect().row(-1, named=True)
    expected = [n * 10 for n, day in enumerate(days) if day in days[-20:] and day != missing]
    assert last["adv20"] == pytest.approx(sum(expected) / len(expected))
    assert last["adv20_samples"] == 19 and last["adv20_sessions"] == 20
    assert last["adv20_partial"]


def test_workers_produce_identical_logical_tables_and_leave_sources_unchanged(tmp_path):
    src, first, second = tmp_path / "src", tmp_path / "first", tmp_path / "second"
    example(src)
    before = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    build(src, first, roots=("CL",), workers=1)
    build(src, second, roots=("CL",), workers=2)
    for table in [p.name for p in first.iterdir() if p.is_dir()]:
        left, right = scan(first, table).collect(), scan(second, table).collect()
        assert left.equals(right), table
    assert before == {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    assert (first / "coverage.csv").read_text() == (second / "coverage.csv").read_text()


def test_conditions_missing_file_and_partial_final_day(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(src, "defs", "definition", {"2025-01-01": [definition(1, "A")]})
    folder = batch(
        src,
        "stats",
        "statistics",
        {"2025-01-02": [statistic(1, 9, 10)]},
        conditions={"2025-01-03": "missing", "2025-01-02": "degraded"},
        end="2025-01-03T17:00:00Z",
    )
    # A missing manifest-listed download is distinct from an available zero-record day.
    next(folder.glob("*.dbn.zst")).unlink()
    build(src, out, roots=("CL",))
    with (out / "coverage.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert row["missing_condition_days"] == "1"
    assert row["missing_expected_file_days"] == "1"
    assert row["degraded_source_days"] == "1"
    assert row["partial_source_days"] == "1"


def test_unsafe_manifest_and_failed_build_leave_no_partial_output(tmp_path, capsys):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    path = src / "synthetic-statistics/manifest.json"
    value = json.loads(path.read_text())
    value["files"][0]["filename"] = "../escape.dbn.zst"
    path.write_text(json.dumps(value))
    assert main(["build", "--src", str(src), "--out", str(out), "--roots", "CL"]) == 2
    assert "unsafe manifest" in capsys.readouterr().err
    assert not out.exists()


def test_ranks_include_untraded_active_contracts(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(
        src,
        "defs",
        "definition",
        {"2025-01-01": [definition(1, "A", expiry="2025-03-20"), definition(2, "B")]},
    )
    batch(src, "stats", "statistics", {"2025-01-02": [statistic(2, 3, 75)]})
    build(src, out, roots=("CL",))
    assert scan(out, "daily_outright").select("expiry_rank").collect().item() == 2
    assert (
        scan(out, "continuous").filter(pl.col("rank") == 1).select("settlement").collect().item()
        is None
    )


def test_observed_calendar_is_explicitly_unverified(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    row = scan(out, "daily_outright").collect().row(0, named=True)
    assert row["calendar_basis"] == "observed_statistics_sessions"
    assert row["adv20_partial"] and row["adv60_partial"]
