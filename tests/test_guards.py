from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from futures_ts.model import BuildError
from futures_ts.pipeline import build
from futures_ts.selection import prepare_selection
from futures_ts.sources import arrays, discover
from futures_ts.storage import scan
from tests.generate_fixtures import batch, definition, example, statistic, bar


@pytest.mark.parametrize(
    "options",
    [
        {"workers": 0},
        {"workers": 3},
        {"roll_days": -1},
        {"start": date(2025, 2, 1), "end": date(2025, 1, 1)},
    ],
)
def test_invalid_build_options_fail_before_reading_sources(tmp_path, options):
    with pytest.raises(BuildError):
        build(tmp_path / "missing", tmp_path / "out", **options)


def test_nested_output_is_refused(tmp_path):
    example(tmp_path)
    with pytest.raises(BuildError, match="separate"):
        build(tmp_path, tmp_path / "derived", roots=("CL",))


def test_no_batches_is_explicit_error(tmp_path):
    with pytest.raises(BuildError, match="No matching"):
        discover(tmp_path)


def test_source_size_mismatch_is_not_silently_skipped(tmp_path):
    example(tmp_path)
    path = next(tmp_path.rglob("*.statistics.dbn.zst"))
    with path.open("ab") as handle:
        handle.write(b"extra")
    with pytest.raises(BuildError, match="size disagrees"):
        discover(tmp_path)


def test_decoder_has_bounded_record_chunks(tmp_path):
    folder = batch(
        tmp_path, "stats", "statistics", {"2025-01-02": [statistic(i, 9, 100) for i in range(7)]}
    )
    source = next(s for s in discover(tmp_path).files if s.path.parent == folder)
    assert [len(chunk) for chunk in arrays(source, count=2)] == [2, 2, 2, 1]


@pytest.mark.parametrize(
    "body",
    [
        "root,date\nCL,2025-01-02\nCL,2025-01-02\n",
        "root,date\nNG,2025-01-02\n",
        "date\n2025-01-02\n",
    ],
)
def test_invalid_supplied_calendar_abstains(tmp_path, body):
    src = tmp_path / "src"
    example(src)
    calendar = tmp_path / "calendar.csv"
    calendar.write_text(body)
    with pytest.raises(BuildError, match="[Cc]alendar"):
        build(src, tmp_path / "out", roots=("CL",), calendar=calendar)


def test_conflicting_bars_fail_without_publishing_partial_output(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(src, "defs", "definition", {"2025-01-01": [definition(1, "A")]})
    batch(src, "bars", "ohlcv-1d", {"2025-01-02": [bar(1), bar(1, 71)]})
    with pytest.raises(BuildError, match="Conflicting UTC"):
        build(src, out, roots=("CL",))
    assert not out.exists()
    assert not list(tmp_path.glob(".futures-ts-*"))


def test_future_leg_becomes_eligible_without_guessing_or_new_snapshot(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    rows = [definition(1, "A", expiry="2025-03-20"), definition(2, "B", activation="2025-01-03")]
    rows += [
        definition(3, "A-B", leg_count=2, leg=(0, 1, "A", "B")),
        definition(3, "A-B", leg_count=2, leg=(1, 2, "B", "A")),
    ]
    batch(src, "defs", "definition", {"2025-01-01": rows})
    batch(src, "stats", "statistics", {"2025-01-03": [statistic(3, 3, -3, day="2025-01-03")]})
    build(src, out, roots=("CL",))
    assert (
        scan(out, "daily_spread")
        .filter(pl.col("kind") == "listed")
        .select(pl.len())
        .collect()
        .item()
        == 1
    )
    assert (
        scan(out, "root_daily")
        .filter(pl.col("instrument_class") == "S")
        .select("expected_instruments")
        .collect()
        .item()
        == 1
    )


def test_root_totals_do_not_count_synthetic_spreads(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    total = (
        scan(out, "root_daily")
        .filter((pl.col("date") == date(2025, 1, 2)) & (pl.col("instrument_class") == "F"))
        .collect()
        .row(0, named=True)
    )
    assert total["cleared_volume"] == 700
    assert total["open_interest"] == sum(100 + i for i in range(1, 8))
    spread = scan(out, "root_daily").filter(pl.col("instrument_class") == "S").collect()
    assert spread["open_interest"].null_count() == spread.height
    assert spread["cleared_volume"].null_count() == spread.height


def test_same_date_utc_bar_and_session_statistic_keep_bases(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(src, "defs", "definition", {"2025-01-01": [definition(1, "A")]})
    batch(
        src,
        "stats",
        "statistics",
        {
            "2025-01-02": [
                statistic(1, 3, 70, day="2025-01-02", ref=None, event="2025-01-02T23:00:00Z")
            ]
        },
    )
    batch(src, "bars", "ohlcv-1d", {"2025-01-02": [bar(1)]})
    build(src, out, roots=("CL",))
    rows = scan(out, "daily_outright").sort("date").collect().to_dicts()
    assert len(rows) == 2
    assert rows[0]["bar_date_utc"] == date(2025, 1, 2) and rows[0]["settlement"] is None
    assert rows[1]["date"] == date(2025, 1, 3) and rows[1]["bar_date_utc"] is None
    assert rows[1]["settlement_date_basis"] == "event_17ct"


def test_selection_payload_budget_is_explicit(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    with pytest.raises(BuildError, match="max_instrument_sessions"):
        prepare_selection(
            out, "CL", lifecycle_basis="activation_expiration", max_instrument_sessions=1
        )


def test_unknown_output_manifest_root_is_rejected(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    path = out / "manifest.json"
    value = json.loads(path.read_text())
    value["roots"] = ["../../outside"]
    path.write_text(json.dumps(value))
    with pytest.raises(BuildError, match="Invalid roots"):
        prepare_selection(out, "CL", lifecycle_basis="activation_expiration")
