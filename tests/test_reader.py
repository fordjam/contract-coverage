from datetime import date

import pytest

from futures_ts.model import BuildError, EVENT, frame
from futures_ts.pipeline import build
from futures_ts.reader import scan_daily
from futures_ts.transforms import reduce_statistics
from tests.generate_fixtures import batch, definition, statistic
from tests.test_ingestion import event


def test_reader_prunes_unneeded_year_and_projects_columns(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    batch(
        src,
        "defs",
        "definition",
        {"2024-01-01": [definition(1, "A", day="2024-01-01", expiry="2026-06-20")]},
    )
    batch(
        src,
        "stats",
        "statistics",
        {
            "2024-01-02": [statistic(1, 6, 100, day="2024-01-02")],
            "2025-01-02": [statistic(1, 6, 200)],
        },
    )
    build(src, out, roots=("CL",))
    # The irrelevant year's file must not be opened for this bounded query.
    (out / "daily_outright/root=CL/year=2024/data.parquet").unlink()
    result = scan_daily(
        out, "CL", start=date(2025, 1, 1), columns=["date", "cleared_volume"]
    ).collect()
    assert result.columns == ["date", "cleared_volume"]
    assert result.row(0) == (date(2025, 1, 2), 200)
    with pytest.raises(BuildError, match="columns"):
        scan_daily(out, "CL", columns=["unrecognized"])


def test_only_final_ordering_ambiguity_affects_statistic():
    rows = [event(70), event(71), event(72, sequence=1)]
    result = reduce_statistics(frame(rows, EVENT).lazy()).row(0, named=True)
    assert result["settlement"] == 72
