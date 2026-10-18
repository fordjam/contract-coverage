from __future__ import annotations

import json

import polars as pl
import pytest

from futures_ts.cli import main
from futures_ts.model import BuildError
from futures_ts.pipeline import build
from futures_ts.storage import scan
from futures_ts.validation import check
from tests.generate_fixtures import batch, definition, example, statistic, bar


def test_complete_cli_build_coverage_and_check(tmp_path, capsys):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    assert main(["build", "--src", str(src), "--out", str(out), "--roots", "CL"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert main(["coverage", "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["unmapped_ids"] == 1
    assert summary["summary"]["listed_spreads"]["open_interest"] == 0
    assert main(["check", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"]["daily_outright"] == 28
    synthetic = scan(out, "daily_spread").filter(pl.col("kind") == "synthetic").collect()
    assert synthetic.height == 20
    assert synthetic["settlement"].to_list() == [-1.0] * 20
    assert synthetic["open_interest"].null_count() == synthetic.height
    assert "degraded" in (out / "coverage.csv").read_text()
    assert (out / "daily_outright/root=CL/year=2025/data.parquet").exists()


def test_publication_refuses_existing_output(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    out.mkdir()
    (out / "keep.txt").write_text("keep")
    with pytest.raises(BuildError, match="never overwritten"):
        build(src, out, roots=("CL",))
    assert (out / "keep.txt").read_text() == "keep"


def test_deletes_in_later_source_file_and_stat_only_rows(tmp_path):
    src = tmp_path / "src"
    batch(
        src, "definitions", "definition", {"2025-01-01": [definition(1, "A"), definition(2, "B")]}
    )
    batch(
        src,
        "statistics",
        "statistics",
        {
            "2025-01-02": [statistic(1, 3, 70), statistic(2, 9, 500)],
            "2025-01-03": [statistic(1, 3, 71, day="2025-01-03", ref="2025-01-02", action=2)],
        },
    )
    out = tmp_path / "out"
    build(src, out, roots=("CL",))
    rows = scan(out, "daily_outright").collect().sort("instrument_id").to_dicts()
    assert len(rows) == 2
    assert rows[0]["settlement"] is None and rows[0]["settlement_deleted"]
    assert rows[1]["open_interest"] == 500 and rows[1]["settlement"] is None


def test_adv_holiday_gap_and_missing_observation(tmp_path):
    src = tmp_path / "src"
    batch(src, "defs", "definition", {"2025-01-01": [definition(1, "A"), definition(2, "B")]})
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    batch(
        src,
        "stats",
        "statistics",
        {
            days[0]: [statistic(1, 6, 100, day=days[0])],
            days[1]: [statistic(2, 6, 10, day=days[1])],
            days[2]: [statistic(1, 6, 200, day=days[2])],
            days[3]: [statistic(1, 9, 1000, day=days[3])],
        },
    )
    batch(src, "bars", "ohlcv-1d", {days[3]: [bar(1, volume=300, day=days[3])]})
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("root,date\n" + "".join(f"CL,{d}\n" for d in days))
    out = tmp_path / "out"
    build(src, out, roots=("CL",), calendar=calendar)
    rows = scan(out, "daily_outright").filter(pl.col("instrument_id") == 1).collect().sort("date")
    last = rows.row(-1, named=True)
    assert last["adv20"] == 200
    assert last["adv20_samples"] == 3 and last["adv20_sessions"] == 4
    assert last["adv_volume_basis"] == "ohlcv_utc_fallback"
    assert last["adv20_partial"]


def test_output_checksum_detects_corruption(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    path = next((out / "daily_outright").rglob("*.parquet"))
    with path.open("ab") as handle:
        handle.write(b"corrupt")
    with pytest.raises(BuildError, match="checksum"):
        check(out)
