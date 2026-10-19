"""Validate reusable policies independently of their storage or caller."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
import re
from typing import TYPE_CHECKING

import polars as pl

from .schema import ACCOUNTING, GROUP

if TYPE_CHECKING:
    from .engine import AnalysisResult


def integer(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"Policy {name} must be an integer")
    if not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(f"Policy {name} must be a nonnegative integer")
    result = int(value)
    if positive and result == 0:
        raise ValueError(f"Policy {name} must be positive")
    return result


def validate_policy(fitted: AnalysisResult) -> pl.DataFrame:
    """Return canonical rules only after checking capacity and its certificate."""
    summary, rules = fitted.summary, fitted.rules
    try:
        method = summary["method"]
        if method not in {"monthly-calendar", "fixed-products", "fixed-patterns"}:
            raise ValueError("Only resolved, repeatable policies can be evaluated")
        if summary["minimum_capacity"] is None:
            raise ValueError("Only resolved, repeatable policies can be evaluated")
        if type(summary["minimum_capacity"]) is not int:
            raise ValueError("Policy capacity must be an integer")
        capacity = integer(summary["minimum_capacity"], "capacity", positive=True)
        if (
            summary["optimality"] != "exact_within_stated_policy"
            or summary["target_met"] is not True
        ):
            raise ValueError("Policy must describe a resolved training result")
        for field in [*GROUP, *ACCOUNTING]:
            if not isinstance(summary[field], str) or not summary[field].strip():
                raise ValueError(f"Invalid policy {field}")
        if summary["category"] not in {"outright", "listed_spread"}:
            raise ValueError("Invalid policy category")
        if date.fromisoformat(summary["start"]) > date.fromisoformat(summary["end"]):
            raise ValueError("Policy training dates are reversed")
        if not isinstance(summary["target"], str):
            raise ValueError("Policy target must be an exact fraction string")
        target = Fraction(summary["target"])
        if not 0 < target <= 1:
            raise ValueError("Invalid policy target")
        if not isinstance(rules, pl.DataFrame) or set(rules.columns) != {
            "state",
            "candidate",
            "volume",
            "priority",
        }:
            raise ValueError("Policy rules have an invalid schema")
        if integer(summary["selected_rules"], "selected_rules") != rules.height or not rules.height:
            raise ValueError("Policy rule count disagrees with training summary")
        states = defaultdict(list)
        canonical = []
        for row in rules.iter_rows(named=True):
            state, candidate = row["state"], row["candidate"]
            if (
                not isinstance(state, str)
                or not isinstance(candidate, str)
                or not candidate.strip()
            ):
                raise ValueError("Policy state and candidate must be nonempty strings")
            if method == "monthly-calendar":
                if not re.fullmatch(r"[1-9]|1[0-2]", state):
                    raise ValueError("Invalid policy observation month")
                legs = candidate.split("/")
                if len(legs) != (2 if summary["category"] == "listed_spread" else 1):
                    raise ValueError("Invalid policy calendar candidate")
                months = []
                for leg in legs:
                    match = re.fullmatch(r"Y(0|[1-9][0-9]*|-[1-9][0-9]*)-M(0[1-9]|1[0-2])", leg)
                    if not match:
                        raise ValueError("Invalid policy calendar candidate")
                    months.append(tuple(map(int, match.groups())))
                if len(months) == 2 and months[0] >= months[1]:
                    raise ValueError("Policy calendar legs must be ordered near/far")
            elif state != "all":
                raise ValueError("Fixed policies require state all")
            priority = integer(row["priority"], "priority", positive=True)
            volume = integer(row["volume"], "volume", positive=True)
            states[state].append((priority, candidate))
            canonical.append(
                {"state": state, "candidate": candidate, "volume": volume, "priority": priority}
            )
        for items in states.values():
            if len(items) > capacity or any(priority > capacity for priority, _ in items):
                raise ValueError("Policy rules exceed declared capacity")
            if sorted(priority for priority, _ in items) != list(range(1, len(items) + 1)):
                raise ValueError(
                    "Policy priorities must be unique and contiguous within each state"
                )
            if len({candidate for _, candidate in items}) != len(items):
                raise ValueError("Duplicate policy candidate")
        captured = sum(row["volume"] for row in canonical)
        before = sum(row["volume"] for row in canonical if row["priority"] < capacity)
        total = integer(summary["known_volume"], "known_volume", positive=True)
        if (
            captured != integer(summary["captured"], "captured")
            or captured > total
            or before != integer(summary["capacity_minus_one_captured"], "K-minus-one")
        ):
            raise ValueError("Policy weights disagree with training certificate")
        if (
            captured * target.denominator < total * target.numerator
            or before * target.denominator >= total * target.numerator
        ):
            raise ValueError("Policy minimality certificate does not meet its target")
        return pl.DataFrame(
            canonical, schema_overrides={"volume": pl.Int128, "priority": pl.UInt64}
        )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"Malformed policy: {exc}") from exc
