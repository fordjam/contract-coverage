from datetime import date

import pytest

from futures_ts.continuous import Continuous
from tests.generate_fixtures import ns


def chain():
    return [
        {
            "contract_key": chr(65 + n),
            "raw_symbol": chr(65 + n),
            "expiration": date(2025, 2 + n, 20),
        }
        for n in range(7)
    ]


def quote(value, oi=None, available=None):
    return {
        "settlement": value,
        "open_interest": oi,
        "open_interest_available_at": ns(available) if available else None,
    }


def test_oi_crossover_is_lagged_and_adjustment_has_expected_sign():
    series = Continuous("CL", 5)
    series.step(
        date(2025, 1, 2),
        chain(),
        {"A": quote(70, 100, "2025-01-02T18:00:00Z"), "B": quote(73, 200, "2025-01-02T18:00:00Z")},
    )
    assert not series.rolls
    series.step(date(2025, 1, 3), chain(), {"A": quote(71), "B": quote(75)})
    rows, rolls = series.adjusted()
    first = next(r for r in rows if r["rank"] == 1 and r["date"] == date(2025, 1, 2))
    second = next(r for r in rows if r["rank"] == 1 and r["date"] == date(2025, 1, 3))
    assert first["contract_key"] == "A" and second["contract_key"] == "B"
    assert first["difference_adjusted"] == 74
    assert first["ratio_adjusted"] == pytest.approx(70 * 75 / 71)
    assert second["difference_adjusted"] == second["ratio_adjusted"] == 75
    assert rolls[0]["reason"] == "oi_crossover"
    assert len({r["contract_key"] for r in rows if r["date"] == date(2025, 1, 3)}) == 6


def test_oi_published_after_open_cannot_roll_early():
    series = Continuous("CL", 5)
    series.step(
        date(2025, 1, 2),
        chain(),
        {"A": quote(70, 100, "2025-01-03T00:30:00Z"), "B": quote(73, 200, "2025-01-03T00:30:00Z")},
    )
    series.step(date(2025, 1, 3), chain(), {"A": quote(71), "B": quote(75)})
    assert not series.rolls
    series.step(date(2025, 1, 6), chain(), {"A": quote(72), "B": quote(76)})
    assert series.rolls[0]["date"] == date(2025, 1, 6)


def test_expiry_fallback_without_oi():
    series = Continuous("CL", 5)
    series.step(date(2025, 2, 14), chain(), {"A": quote(70), "B": quote(73)})
    series.step(date(2025, 2, 15), chain(), {"A": quote(71), "B": quote(74)})
    assert series.rolls[0]["reason"] == "expiry_fallback"
    assert series.rolls[0]["new_key"] == "B"


def test_zero_negative_and_missing_bridges_do_not_invent_ratios():
    for old_price in [0, -10, None]:
        series = Continuous("CL", 5)
        series.step(date(2025, 2, 14), chain(), {"A": quote(-20), "B": quote(2)})
        series.step(date(2025, 2, 15), chain(), {"A": quote(old_price), "B": quote(3)})
        rows, _ = series.adjusted()
        first = next(r for r in rows if r["rank"] == 1 and r["date"] == date(2025, 2, 14))
        assert first["ratio_adjusted"] is None and not first["ratio_valid"]
        if old_price is None:
            assert first["difference_adjusted"] is None
        else:
            assert first["difference_adjusted"] == -20 + 3 - old_price


def test_roll_schedule_never_reverts_on_later_oi_changes():
    series = Continuous("CL", 5)
    series.step(
        date(2025, 1, 2),
        chain(),
        {"A": quote(70, 100, "2025-01-02T18:00:00Z"), "B": quote(73, 200, "2025-01-02T18:00:00Z")},
    )
    series.step(
        date(2025, 1, 3),
        chain(),
        {"A": quote(71, 1000, "2025-01-03T18:00:00Z"), "B": quote(75, 10, "2025-01-03T18:00:00Z")},
    )
    series.step(date(2025, 1, 6), chain(), {"A": quote(72), "B": quote(76)})
    assert (
        next(r for r in series.rows if r["rank"] == 1 and r["date"] == date(2025, 1, 6))[
            "contract_key"
        ]
        == "B"
    )
