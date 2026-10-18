from datetime import date

import pytest

from futures_ts.definitions import Definitions
from futures_ts.model import BuildError, frame, EVENT, session_date
from futures_ts.sources import arrays, discover
from futures_ts.transforms import reduce_statistics
from tests.generate_fixtures import batch, definition, ns, statistic, UNDEF_TS


def test_encoded_dbn_and_asof_id_reuse(tmp_path):
    batch(
        tmp_path,
        "old",
        "definition",
        {
            "2020-01-01": [
                definition(
                    1, "CL-OLD", day="2020-01-01", activation="2019-01-01", expiry="2020-06-20"
                )
            ]
        },
    )
    batch(tmp_path, "new", "definition", {"2025-01-01": [definition(1, "CL-NEW")]})
    sources = discover(tmp_path, ("CL",))
    assert sum(len(a) for a in arrays(sources.files[0])) == 1
    definitions = Definitions(sources)
    assert definitions.catalog(date(2018, 1, 1)) == {}
    old = definitions.catalog(date(2020, 1, 2))[1, 1]
    new = definitions.catalog(date(2025, 1, 2))[1, 1]
    assert old["contract_key"] != new["contract_key"]
    assert old["raw_symbol"] == "CL-OLD"
    assert definitions.catalog(date(2020, 6, 21)) == {}


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2025-01-02T22:59:59Z", "2025-01-02"),
        ("2025-01-02T23:00:00Z", "2025-01-03"),
        ("2025-07-02T21:59:59Z", "2025-07-02"),
        ("2025-07-02T22:00:00Z", "2025-07-03"),
    ],
)
def test_session_boundary(stamp, expected):
    assert session_date(UNDEF_TS, ns(stamp)) == (date.fromisoformat(expected), "event_17ct")
    assert session_date(ns("2025-01-01"), ns(stamp)) == (date(2025, 1, 1), "ts_ref")


def test_spread_legs_are_rows_not_duplicate_instruments(tmp_path):
    rows = [definition(1, "A", expiry="2025-03-20"), definition(2, "B")]
    rows += [
        definition(3, "A-B", leg_count=2, leg=(0, 1, "A", "B")),
        definition(3, "A-B", leg_count=2, leg=(1, 2, "B", "A")),
    ]
    batch(tmp_path, "defs", "definition", {"2025-01-01": rows})
    resolver = Definitions(discover(tmp_path, ("CL",)))
    catalog = resolver.catalog(date(2025, 1, 2))
    assert len(catalog) == 3
    assert catalog[1, 3]["calendar_spread"]
    assert catalog[1, 3]["near_key"] == catalog[1, 1]["contract_key"]
    assert catalog[1, 3]["listed_price_sign"] == 1


@pytest.mark.parametrize("sides,eligible,sign", [("AB", True, -1), ("AA", False, None)])
def test_reversed_calendar_and_ambiguous_sides(tmp_path, sides, eligible, sign):
    rows = [definition(1, "A", expiry="2025-03-20"), definition(2, "B")]
    rows += [
        definition(3, "B-A", leg_count=2, leg=(0, 1, "A", sides[0])),
        definition(3, "B-A", leg_count=2, leg=(1, 2, "B", sides[1])),
    ]
    batch(tmp_path, "defs", "definition", {"2025-01-01": rows})
    row = Definitions(discover(tmp_path, ("CL",))).catalog(date(2025, 1, 2))[1, 3]
    assert row["calendar_spread"] is eligible
    assert row["listed_price_sign"] == sign
    if eligible:
        assert row["near_key"] != row["far_key"]
        assert row["exclusion_reason"] is None
    else:
        assert row["exclusion_reason"] == "unsupported_leg_orientation"


def test_missing_definition_retains_identity(tmp_path):
    batch(tmp_path, "stats", "statistics", {"2025-01-02": [statistic(999, 9, 50)]})
    resolver = Definitions(discover(tmp_path, ("CL",)))
    row = resolver.identity({}, date(2025, 1, 2), 1, 999)
    assert row["instrument_id"] == 999
    assert row["root"] is None and row["contract_key"] is None
    assert row["mapping_reason"]


def test_subset_batch_not_double_counted(tmp_path):
    batch(tmp_path, "full", "statistics", {"2025-01-02": [statistic(1, 9, 10)]}, roots=("CL", "NG"))
    batch(tmp_path, "subset", "statistics", {"2025-01-02": [statistic(1, 9, 10)]})
    sources = discover(tmp_path)
    assert len(sources.files) == 1
    assert sources.skipped_subsets == 1


def event(value=70, action=1, flags=3, sequence=0, stat_type=3):
    return {
        "date": date(2025, 1, 2),
        "publisher_id": 1,
        "instrument_id": 1,
        "contract_key": "A",
        "root": "CL",
        "stat_type": stat_type,
        "price_nanos": int(value * 1e9),
        "quantity": int(value),
        "ts_event": ns("2025-01-02T18:00:00Z"),
        "ts_recv": ns("2025-01-02T18:00:00Z"),
        "sequence": sequence,
        "channel_id": 1,
        "update_action": action,
        "stat_flags": flags,
        "date_basis": "ts_ref",
    }


def test_statistics_update_delete_and_independent_fields():
    rows = [
        event(70),
        event(72, sequence=1),
        event(72, action=2, sequence=2),
        event(100, stat_type=9),
    ]
    result = reduce_statistics(frame(rows, EVENT).lazy()).row(0, named=True)
    assert result["settlement"] is None
    assert result["settlement_deleted"]
    assert result["open_interest"] == 100
    assert result["cleared_volume"] is None


@pytest.mark.parametrize(
    "flags,value,final,actual",
    [(3, 70, True, True), (0, 70, False, False), (128, None, None, None), (100, None, None, None)],
)
def test_settlement_flags(flags, value, final, actual):
    result = reduce_statistics(frame([event(flags=flags)], EVENT).lazy()).row(0, named=True)
    assert result["settlement"] == value
    assert result["settlement_final"] is final
    assert result["settlement_actual"] is actual


def test_conflicting_statistics_abstain():
    with pytest.raises(BuildError, match="Conflicting statistic"):
        reduce_statistics(frame([event(70), event(71)], EVENT).lazy())
