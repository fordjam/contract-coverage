"""A monotone front roll schedule with six distinct, backward-adjusted ladders."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from .model import session_open


class Continuous:
    def __init__(self, root: str, roll_days: int):
        self.root, self.roll_days = root, roll_days
        self.front = None
        self.front_expiration = None
        self.previous = {}
        self.oi = defaultdict(list)
        self.rows, self.rolls = [], []

    def _known_oi(self, key: str, day: date, cutoff: int) -> dict | None:
        eligible = [r for r in self.oi[key] if r["date"] < day and r["available"] <= cutoff]
        known = max(eligible, key=lambda r: (r["date"], r["available"])) if eligible else None
        self.oi[key] = ([known] if known else []) + [
            r for r in self.oi[key] if r["available"] > cutoff
        ]
        return known

    def step(self, day: date, chain: list[dict], quotes: dict[str, dict]) -> None:
        cutoff = session_open(day)
        chain = sorted(chain, key=lambda r: (r["expiration"], r["raw_symbol"], r["contract_key"]))
        positions = {r["contract_key"]: n for n, r in enumerate(chain)}
        reason, current_oi, next_oi = None, None, None
        oi_evidence = {}
        if self.front is None and chain:
            self.front = chain[0]["contract_key"]
            self.front_expiration = chain[0]["expiration"]
        elif self.front not in positions:
            later = [
                r
                for r in chain
                if self.front_expiration is None or r["expiration"] > self.front_expiration
            ]
            if later:
                self.front = later[0]["contract_key"]
                self.front_expiration = later[0]["expiration"]
                reason = "expired_or_unlisted"
        index = positions.get(self.front)
        if index is not None and index + 1 < len(chain) and reason is None:
            current, following = chain[index], chain[index + 1]
            left = self._known_oi(current["contract_key"], day, cutoff)
            right = self._known_oi(following["contract_key"], day, cutoff)
            if left and right and left["date"] == right["date"]:
                current_oi, next_oi = left["value"], right["value"]
                oi_evidence = {
                    "oi_current_key": current["contract_key"],
                    "oi_next_key": following["contract_key"],
                    "oi_reference_date": left["date"],
                    "oi_current_available_at": left["available"],
                    "oi_next_available_at": right["available"],
                }
            if current_oi is not None and next_oi is not None and next_oi > current_oi:
                reason = "oi_crossover"
            elif (current["expiration"] - day).days <= self.roll_days:
                reason = "expiry_fallback"
            if reason:
                self.front = following["contract_key"]
                self.front_expiration = following["expiration"]
                index += 1
        for rank in range(1, 7):
            instrument = (
                chain[index + rank - 1]
                if index is not None and index + rank - 1 < len(chain)
                else None
            )
            key = instrument["contract_key"] if instrument else None
            quote = quotes.get(key, {})
            old = self.previous.get(rank)
            if old is not None and old != key:
                old_price, new_price = (
                    quotes.get(old, {}).get("settlement"),
                    quote.get("settlement"),
                )
                gap = (
                    new_price - old_price
                    if new_price is not None and old_price is not None
                    else None
                )
                ratio = (
                    new_price / old_price
                    if new_price is not None
                    and old_price is not None
                    and min(old_price, new_price) > 0
                    else None
                )
                self.rolls.append(
                    {
                        "date": day,
                        "root": self.root,
                        "rank": rank,
                        "old_key": old,
                        "new_key": key,
                        "reason": reason or "catalog_change",
                        "old_settlement": old_price,
                        "new_settlement": new_price,
                        "difference_gap": gap,
                        "ratio": ratio,
                        "oi_cutoff": cutoff,
                        "current_oi": current_oi,
                        "next_oi": next_oi,
                        **oi_evidence,
                    }
                )
            self.rows.append(
                {
                    "date": day,
                    "root": self.root,
                    "rank": rank,
                    "contract_key": key,
                    "raw_symbol": instrument["raw_symbol"] if instrument else None,
                    "settlement": quote.get("settlement"),
                    **{
                        c: quote.get(c)
                        for c in (
                            "settlement_final",
                            "settlement_actual",
                            "settlement_intraday",
                            "settlement_available_at",
                        )
                    },
                }
            )
            self.previous[rank] = key
        for key, row in quotes.items():
            available = row.get("open_interest_available_at")
            if available is not None:
                self.oi[key].append(
                    {"date": day, "available": available, "value": row.get("open_interest")}
                )
        for key in list(self.oi):
            self._known_oi(key, day, cutoff)

    def adjusted(self) -> tuple[list[dict], list[dict]]:
        bridges = {(row["date"], row["rank"]): row for row in self.rolls}
        by_rank = defaultdict(list)
        for row in self.rows:
            by_rank[row["rank"]].append(row)
        result = []
        for rank, rows in by_rank.items():
            difference, ratio, difference_valid, ratio_valid = 0.0, 1.0, True, True
            for row in reversed(rows):
                value = row["settlement"]
                adjusted_difference = (
                    value + difference if value is not None and difference_valid else None
                )
                adjusted_ratio = value * ratio if value is not None and ratio_valid else None
                if adjusted_difference is not None and not math.isfinite(adjusted_difference):
                    adjusted_difference, difference_valid = None, False
                if adjusted_ratio is not None and not math.isfinite(adjusted_ratio):
                    adjusted_ratio, ratio_valid = None, False
                result.append(
                    {
                        **row,
                        "difference_adjusted": adjusted_difference,
                        "ratio_adjusted": adjusted_ratio,
                        "difference_valid": difference_valid,
                        "ratio_valid": ratio_valid,
                    }
                )
                bridge = bridges.get((row["date"], rank))
                if bridge:
                    if bridge["difference_gap"] is None:
                        difference_valid = False
                    else:
                        difference += bridge["difference_gap"]
                    if bridge["ratio"] is None:
                        ratio_valid = False
                    else:
                        ratio *= bridge["ratio"]
        return sorted(result, key=lambda r: (r["date"], r["rank"])), self.rolls
