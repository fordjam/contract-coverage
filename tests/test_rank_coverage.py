from datetime import date
import json

import polars as pl
import pytest

from futures_ts.cli import main
from futures_ts.model import SCHEMAS, frame
from futures_ts.pipeline import build
from futures_ts.rank_coverage import (
    label_activity,
    front_chain_curve,
    rank_catalog,
    series_availability,
    summarize_activity,
)
from futures_ts.rank_report import render_report
from tests.generate_fixtures import bar, batch, definition, example, statistic


def test_aggregate_selection_and_daily_shortfalls():
    data = pl.DataFrame(
        {
            "date": [date(2025, 1, 2)] * 2 + [date(2025, 1, 3)] * 2,
            "label": ["C1", "C2", "C1", "C2"],
            "volume": [90, 10, 0, 10],
        }
    )
    rows, recommendations = summarize_activity(data, "CL", "outrights", "volume", (0.8, 0.95))
    assert rows[0]["label"] == "C1"
    assert recommendations[0]["labels"] == ["C1"]
    assert recommendations[0]["aggregate_share"] == pytest.approx(90 / 110)
    assert recommendations[0]["days_meeting_target_fraction"] == 0.5
    assert recommendations[0]["daily_min_share"] == 0
    assert recommendations[1]["labels"] == ["C1", "C2"]


def test_unranked_activity_cannot_be_silently_removed():
    data = pl.DataFrame({"date": [date(2025, 1, 2)] * 2, "label": ["C1", None], "volume": [50, 50]})
    _, chosen = summarize_activity(data, "CL", "outrights", "volume", (0.95,))
    assert chosen[0]["aggregate_share"] == 0.5
    assert not chosen[0]["target_met"]
    assert chosen[0]["unranked_activity"] == 50


def test_missing_metric_is_unknown_and_zero_is_observed():
    data = pl.DataFrame(
        {"date": [date(2025, 1, 2), date(2025, 1, 3)], "label": ["C1", "C1"], "volume": [None, 0]},
        schema_overrides={"volume": pl.UInt64},
    )
    assert summarize_activity(data, "CL", "outrights", "volume", (0.95,)) == ([], [])


def test_rank_catalog_includes_unobserved_and_uses_asof_lifecycle():
    data = frame(
        [
            {
                "date": date(2025, 1, 1),
                "contract_key": "a",
                "raw_symbol": "A",
                "instrument_class": "F",
                "activation": date(2024, 1, 1),
                "expiration": date(2025, 1, 2),
                "deleted": False,
            },
            {
                "date": date(2025, 1, 1),
                "contract_key": "b",
                "raw_symbol": "B",
                "instrument_class": "F",
                "activation": date(2024, 1, 1),
                "expiration": date(2025, 2, 2),
                "deleted": False,
            },
            {
                "date": date(2025, 2, 1),
                "contract_key": "c",
                "raw_symbol": "C",
                "instrument_class": "F",
                "activation": date(2024, 1, 1),
                "expiration": date(2025, 3, 2),
                "deleted": False,
            },
        ],
        SCHEMAS["instruments"],
    )
    ranked = rank_catalog(data, [date(2025, 1, 2), date(2025, 1, 3)])
    assert ranked.filter(pl.col("date") == date(2025, 1, 2))["symbol"].to_list() == ["A", "B"]
    assert ranked.filter(pl.col("date") == date(2025, 1, 3))["symbol"].to_list() == ["B"]
    assert ranked.filter(pl.col("date") == date(2025, 1, 3))["rank"].to_list() == [1]


@pytest.mark.parametrize("sign,settlement", [(1, -2.0), (-1, 2.0)])
def test_c3_minus_c1_sign_and_no_synthetic_activity(sign, settlement):
    day = date(2025, 1, 2)
    ranks = pl.DataFrame(
        {"date": [day, day], "contract_key": ["a", "c"], "rank": [1, 3], "symbol": ["A", "C"]}
    )
    spread = frame(
        [
            {
                "date": day,
                "kind": "listed",
                "near_key": "a",
                "far_key": "c",
                "listed_price_sign": sign,
                "settlement": settlement,
                "volume": 10,
            },
            {
                "date": day,
                "kind": "synthetic",
                "near_key": "a",
                "far_key": "c",
                "listed_price_sign": 1,
                "settlement": -2.0,
                "volume": None,
            },
        ],
        SCHEMAS["daily_spread"],
    )
    labeled = label_activity(spread, ranks, "listed_spreads")
    assert labeled.height == 1
    assert labeled["label"].item() == "C3-C1"
    assert labeled["label_settlement"].item() == 2


def test_analysis_cli_writes_offline_report_and_machine_readable_selection(tmp_path, capsys):
    src, out = tmp_path / "src", tmp_path / "out"
    example(src)
    build(src, out, roots=("CL",))
    assert main(["analyze", "--out", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["roots"] == 1
    content = json.loads((out / "analysis/analysis.json").read_text())
    assert content["unmapped_rows"] == 1
    assert any(r["label"] == "C1" for r in content["ranked"])
    assert any(r["symbol"] == "CL-SYN-1" for r in content["contracts"])
    assert (out / "analysis/recommendations.csv").exists()
    assert "C3-C1" in (out / "analysis/summary.md").read_text()
    html = (out / "analysis/report.html").read_text()
    assert "fetch(" not in html
    assert "__ANALYSIS_DATA__" not in html


def test_report_escapes_script_terminator():
    html = render_report({"symbol": "</script><script>alert(1)</script>"})
    assert "</script><script>alert(1)" not in html


def test_synthetic_price_completeness_requires_both_legs():
    days = [date(2025, 1, 2), date(2025, 1, 3)]
    ranks = pl.DataFrame(
        {"date": days * 2, "contract_key": ["a", "a", "b", "b"], "rank": [1, 1, 2, 2]}
    )
    quotes = frame(
        [
            {"date": days[0], "contract_key": "a", "settlement": 70},
            {"date": days[1], "contract_key": "a", "settlement": 71},
            {"date": days[0], "contract_key": "b", "settlement": 72},
        ],
        SCHEMAS["daily_outright"],
    )
    rows = series_availability(quotes, ranks, "CL", days)
    spread = next(r for r in rows if r["category"] == "synthetic_spreads")
    assert spread["settlement_fraction"] == 0.5
    assert spread["expected_dates"] == 2
    assert spread["open_interest_fraction"] is None


def test_price_coverage_keeps_dates_without_an_eligible_definition():
    day = date(2025, 1, 2)
    ranks = pl.DataFrame({"date": [day], "contract_key": ["a"], "rank": [1]})
    quotes = frame(
        [{"date": day, "contract_key": "a", "settlement": 70}], SCHEMAS["daily_outright"]
    )
    rows = series_availability(quotes, ranks, "CL", [date(2024, 12, 31), day])
    assert rows[0]["settlement_fraction"] == 0.5
    assert rows[0]["eligible_dates"] == 1
    assert rows[0]["expected_dates"] == 2


def test_front_chain_includes_only_pairs_with_both_legs_inside_chain():
    data = pl.DataFrame(
        {
            "date": [date(2025, 1, 2)] * 3,
            "label": ["C2-C1", "C3-C1", "C4-C3"],
            "volume": [60, 30, 10],
        }
    )
    rows, _ = summarize_activity(data, "CL", "listed_spreads", "volume", (0.95,))
    curve = front_chain_curve(rows)
    assert [r["share"] for r in curve] == pytest.approx([0, 0.6, 0.9, 1])


def test_analysis_default_ends_before_partial_activity_source(tmp_path, capsys):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(src, "defs", "definition", {"2025-01-01": [definition(1, "A")]})
    days = ["2025-01-02", "2025-01-03"]
    batch(
        src,
        "stats",
        "statistics",
        {d: [statistic(1, 6, 100, day=d)] for d in days},
        end="2025-01-03T12:00:00Z",
    )
    batch(src, "bars", "ohlcv-1d", {d: [bar(1, day=d)] for d in days}, end="2025-01-03T12:00:00Z")
    build(src, out, roots=("CL",))
    assert main(["analyze", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["end"] == "2025-01-02"
    data = json.loads((out / "analysis/analysis.json").read_text())
    assert data["archive_partial_source_dates"] == ["2025-01-03"]
