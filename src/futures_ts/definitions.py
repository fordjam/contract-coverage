"""Dated contract versions and explicit exchange-listed spread legs."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date

from .model import IDENTITY, BuildError, digest, optional_date, text
from .sources import SourceFile, Sources, arrays


def read_snapshot(source: SourceFile) -> tuple[dict[tuple[int, int], dict], list[dict]]:
    groups = defaultdict(list)
    fields = (
        "publisher_id instrument_id ts_event ts_recv raw_symbol asset expiration activation "
        "instrument_class security_update_action leg_index leg_count leg_instrument_id "
        "leg_raw_symbol leg_side leg_ratio_qty_numerator leg_ratio_qty_denominator "
        "user_defined_instrument maturity_year maturity_month"
    ).split()
    for chunk in arrays(source):
        for record in chunk:
            key = (int(record["publisher_id"]), int(record["instrument_id"]))
            # Leg rows share exchange time; receive times may differ by leg.
            groups[key].append({name: record[name].item() for name in fields})
    result, legs_out = {}, []
    for (publisher, instrument), records in groups.items():
        latest_time = max(int(r["ts_event"]) for r in records)
        current = [r for r in records if int(r["ts_event"]) == latest_time]
        row = max(current, key=lambda r: int(r["ts_recv"]))
        raw_symbol, root = text(row["raw_symbol"]), text(row["asset"])
        expiration, activation = (
            optional_date(int(row["expiration"])),
            optional_date(int(row["activation"])),
        )
        instrument_class = text(row["instrument_class"])
        contract_key = digest(
            ["GLBX.MDP3", publisher, root, instrument_class, raw_symbol, activation, expiration]
        )
        year, month = int(row["maturity_year"]), int(row["maturity_month"])
        delivery_month = f"{year:04}-{month:02}" if 1 <= year <= 9999 and 1 <= month <= 12 else None
        deleted = text(row["security_update_action"]) == "D"
        by_index = {}
        for r in current:
            if text(r["raw_symbol"]) != raw_symbol or text(r["asset"]) != root:
                raise BuildError(f"{source.path}: conflicting definition identity at one timestamp")
            index = int(r["leg_index"])
            if (
                index in by_index
                and int(r["ts_recv"]) == int(by_index[index]["ts_recv"])
                and r != by_index[index]
            ):
                raise BuildError(f"{source.path}: conflicting definition leg records")
            if index not in by_index or int(r["ts_recv"]) > int(by_index[index]["ts_recv"]):
                by_index[index] = r
        leg_rows = []
        for index, r in sorted(by_index.items()):
            if int(r["leg_count"]) > 0:
                leg_rows.append(
                    {
                        "date": source.day,
                        "root": root,
                        "contract_key": contract_key,
                        "leg_index": index,
                        "leg_instrument_id": int(r["leg_instrument_id"]),
                        "leg_raw_symbol": text(r["leg_raw_symbol"]),
                        "leg_side": text(r["leg_side"]),
                        "ratio_numerator": int(r["leg_ratio_qty_numerator"]),
                        "ratio_denominator": int(r["leg_ratio_qty_denominator"]),
                    }
                )
        reason = None
        if instrument_class not in ("F", "S"):
            reason = "unsupported_class"
        elif instrument_class == "S":
            if text(row["user_defined_instrument"]) != "N":
                reason = "user_defined_or_unknown"
            elif int(row["leg_count"]) != 2 or len(leg_rows) != 2:
                reason = "not_two_complete_legs"
        version = digest(
            [contract_key, instrument, source.day, delivery_month, deleted, reason, leg_rows]
        )
        result[publisher, instrument] = {
            **dict.fromkeys(IDENTITY),
            "date": source.day,
            "root": root,
            "contract_key": contract_key,
            "publisher_id": publisher,
            "instrument_id": instrument,
            "raw_symbol": raw_symbol,
            "instrument_class": instrument_class,
            "expiration": expiration,
            "activation": activation,
            "definition_date": source.day,
            "definition_version": version,
            "calendar_spread": False,
            "calendar_structure": False,
            "delivery_month": delivery_month,
            "exclusion_reason": reason,
            "deleted": deleted,
        }
        for leg in leg_rows:
            leg["definition_version"] = version
            legs_out.append(leg)
    return result, legs_out


class Definitions:
    def __init__(self, sources: Sources):
        self.files = [s for s in sources.files if s.schema == "definition"]
        # Resolve known out-of-scope instruments before filtering them. Otherwise
        # a --roots subset would misreport other parents as unmapped IDs.
        self.roots = tuple(sorted({root for source in sources.files for root in source.roots}))
        self.cache: OrderedDict[SourceFile, tuple[dict, list]] = OrderedDict()

    def load(self, source: SourceFile) -> tuple[dict, list]:
        if source not in self.cache:
            self.cache[source] = read_snapshot(source)
        self.cache.move_to_end(source)
        while len(self.cache) > 3:
            self.cache.popitem(last=False)
        return self.cache[source]

    def catalog(self, day: date, *, include_inactive: bool = False) -> dict[tuple[int, int], dict]:
        selected = {}
        selected_legs = []
        loaded = {}
        for root in self.roots:
            eligible = [s for s in self.files if s.day <= day and root in s.roots]
            if not eligible:
                continue
            latest = max(s.day for s in eligible)
            for source in eligible:
                if source.day != latest:
                    continue
                if source not in loaded:
                    loaded[source] = self.load(source)
                    selected_legs.extend(loaded[source][1])
                rows, _ = loaded[source]
                for key, row in rows.items():
                    if row["root"] != root:
                        continue
                    existing = selected.get(key)
                    if existing and existing != row:
                        raise BuildError(f"Conflicting definitions for {key} on {day}")
                    selected[key] = row
        return self._resolve_legs(selected, selected_legs, day, include_inactive)

    def _resolve_legs(
        self, rows: dict, all_legs: list[dict], day: date, include_inactive: bool
    ) -> dict:
        resolved = {key: dict(row) for key, row in rows.items()}

        def active(row):
            return (
                not row["deleted"]
                and row["activation"] is not None
                and row["expiration"] is not None
                and row["activation"] <= day <= row["expiration"]
            )

        leg_versions = defaultdict(list)
        for leg in all_legs:
            leg_versions[leg["definition_version"]].append(leg)
        for key, row in resolved.items():
            if row["instrument_class"] != "S" or row["exclusion_reason"]:
                continue
            legs = {leg["leg_index"]: leg for leg in leg_versions[row["definition_version"]]}
            values = [legs[index] for index in sorted(legs)]
            instruments = [resolved.get((key[0], leg["leg_instrument_id"])) for leg in values]
            if len(values) != 2 or any(i is None for i in instruments):
                row["exclusion_reason"] = "missing_leg_definition"
            elif any(i["root"] != row["root"] or i["instrument_class"] != "F" for i in instruments):
                row["exclusion_reason"] = "cross_root_or_nonfuture_leg"
            elif any(
                leg["leg_raw_symbol"] != i["raw_symbol"]
                for leg, i in zip(values, instruments, strict=True)
            ):
                row["exclusion_reason"] = "leg_symbol_mismatch"
            elif len({i["contract_key"] for i in instruments}) != 2 or any(
                leg["ratio_numerator"] != 1 or leg["ratio_denominator"] != 1 for leg in values
            ):
                row["exclusion_reason"] = "not_one_for_one"
            elif any(i["delivery_month"] is None for i in instruments):
                row["exclusion_reason"] = "missing_leg_delivery_month"
            elif any(i["activation"] is None or i["expiration"] is None for i in instruments):
                row["exclusion_reason"] = "missing_leg_lifecycle"
            elif len({i["delivery_month"] for i in instruments}) != 2:
                row["exclusion_reason"] = "same_delivery_month"
            else:
                ordered = sorted(
                    zip(values, instruments, strict=True),
                    key=lambda pair: (pair[1]["expiration"], pair[1]["raw_symbol"]),
                )
                near_leg, near = ordered[0]
                far_leg, far = ordered[1]
                if (near_leg["leg_side"], far_leg["leg_side"]) != ("B", "A"):
                    row["exclusion_reason"] = "unsupported_leg_orientation"
                else:
                    row.update(
                        calendar_structure=True,
                        calendar_spread=all(active(i) for i in instruments),
                        near_key=near["contract_key"],
                        far_key=far["contract_key"],
                    )
                    if not row["calendar_spread"]:
                        row["exclusion_reason"] = "leg_outside_lifecycle"
        return (
            resolved
            if include_inactive
            else {key: row for key, row in resolved.items() if active(row)}
        )

    def identity(self, catalog: dict, day: date, publisher: int, instrument: int) -> dict:
        row = catalog.get((publisher, instrument))
        if row:
            return {key: day if key == "date" else row.get(key) for key in IDENTITY}
        return {
            **dict.fromkeys(IDENTITY),
            "date": day,
            "publisher_id": publisher,
            "instrument_id": instrument,
            "calendar_spread": False,
            "calendar_structure": False,
            "mapping_reason": "no_eligible_asof_definition",
        }
