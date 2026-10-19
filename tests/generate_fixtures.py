"""Generate synthetic GLBX-shaped DBN fixtures. Never consumes vendor data."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import databento_dbn as dbn
import zstandard as zstd

UNDEF = 2**63 - 1
UNDEF_TS = 2**64 - 1


def ns(value: str | date) -> int:
    if isinstance(value, date):
        value = value.isoformat() + "T00:00:00+00:00"
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp()) * 1_000_000_000


def definition(
    instrument: int,
    symbol: str,
    *,
    day: str = "2025-01-01",
    root: str = "CL",
    expiry: str = "2025-06-20",
    activation: str = "2024-01-01",
    leg: tuple[int, int, str, str] | None = None,
    leg_count: int = 0,
    uds: bool = False,
    action: str = "A",
    ratio: int = 1,
) -> dbn.InstrumentDefMsg:
    return dbn.InstrumentDefMsg(
        publisher_id=1,
        instrument_id=instrument,
        ts_event=ns(day + "T00:00:00Z"),
        ts_recv=ns(day + "T00:00:01Z"),
        min_price_increment=10_000_000,
        display_factor=1_000_000_000,
        raw_symbol=symbol,
        asset=root,
        security_type="FUT",
        instrument_class=dbn.InstrumentClass.FUTURE_SPREAD
        if leg_count
        else dbn.InstrumentClass.FUTURE,
        security_update_action=dbn.SecurityUpdateAction(action),
        expiration=ns(expiry + "T23:00:00Z"),
        activation=ns(activation),
        user_defined_instrument=dbn.UserDefinedInstrument.YES
        if uds
        else dbn.UserDefinedInstrument.NO,
        maturity_year=int(expiry[:4]),
        maturity_month=int(expiry[5:7]),
        leg_count=leg_count,
        leg_index=leg[0] if leg else 0,
        leg_instrument_id=leg[1] if leg else 0,
        leg_raw_symbol=leg[2] if leg else "",
        leg_side=dbn.Side(leg[3]) if leg else dbn.Side.NONE,
        leg_ratio_qty_numerator=ratio if leg else 0,
        leg_ratio_qty_denominator=1 if leg else 0,
    )


def statistic(
    instrument: int,
    stat_type: int,
    value: float | int,
    *,
    day: str = "2025-01-02",
    ref: str | None = "default",
    event: str | None = None,
    received: str | None = None,
    action: int = 1,
    flags: int = 3,
    sequence: int = 0,
) -> dbn.StatMsg:
    event_ns = ns(event or day + "T18:00:00Z")
    return dbn.StatMsg(
        publisher_id=1,
        instrument_id=instrument,
        ts_event=event_ns,
        ts_recv=ns(received) if received else event_ns,
        ts_ref=UNDEF_TS if ref is None else ns(day if ref == "default" else ref),
        price=round(value * 1_000_000_000) if stat_type == 3 else UNDEF,
        quantity=int(value) if stat_type in (6, 9) else UNDEF,
        stat_type=dbn.StatType(stat_type),
        update_action=dbn.StatUpdateAction(action),
        stat_flags=flags if stat_type == 3 else 0,
        sequence=sequence,
    )


def bar(
    instrument: int, value: float = 70, volume: int = 100, *, day: str = "2025-01-02"
) -> dbn.OHLCVMsg:
    p = round(value * 1_000_000_000)
    return dbn.OHLCVMsg(
        rtype=dbn.RType.OHLCV_1D,
        publisher_id=1,
        instrument_id=instrument,
        ts_event=ns(day),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=volume,
    )


def batch(
    base: Path,
    name: str,
    schema: str,
    days: dict[str, Iterable],
    *,
    roots: tuple[str, ...] = ("CL",),
    conditions: dict[str, str] | None = None,
    end: str | None = None,
) -> Path:
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    dates = sorted(set(days) | set(conditions or {}))
    start_ns = ns(dates[0])
    end_ns = ns(end) if end else ns(date.fromisoformat(dates[-1]) + timedelta(days=1))
    metadata = dbn.Metadata(
        dataset="GLBX.MDP3",
        start=start_ns,
        end=end_ns,
        stype_in=dbn.SType.PARENT,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema(schema),
        symbols=[root + ".FUT" for root in roots],
    )
    entries = []
    for day, records in sorted(days.items()):
        filename = f"glbx-mdp3-{day.replace('-', '')}.{schema}.dbn.zst"
        path = folder / filename
        with path.open("wb") as handle, zstd.ZstdCompressor().stream_writer(handle) as stream:
            stream.write(metadata.encode())
            for record in records:
                stream.write(bytes(record))
        content = path.read_bytes()
        entries.append(
            {
                "filename": filename,
                "size": len(content),
                "hash": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    query = {
        "dataset": "GLBX.MDP3",
        "schema": schema,
        "stype_in": "parent",
        "stype_out": "instrument_id",
        "symbols": [r + ".FUT" for r in roots],
        "start": start_ns,
        "end": end_ns,
        "encoding": "dbn",
        "compression": "zstd",
    }
    (folder / "metadata.json").write_text(json.dumps({"version": 1, "query": query}) + "\n")
    (folder / "manifest.json").write_text(json.dumps({"files": entries}) + "\n")
    (folder / "condition.json").write_text(
        json.dumps(
            [{"date": d, "condition": (conditions or {}).get(d, "available")} for d in dates]
        )
        + "\n"
    )
    return folder


def example(base: Path, scale: int = 1) -> None:
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    instruments = [definition(i, f"CL-SYN-{i}", expiry=f"2025-{i + 1:02}-20") for i in range(1, 8)]
    instruments.extend(
        [
            definition(100, "CL-SYN-1-2", leg_count=2, leg=(0, 1, "CL-SYN-1", "B")),
            definition(100, "CL-SYN-1-2", leg_count=2, leg=(1, 2, "CL-SYN-2", "A")),
        ]
    )
    batch(base, "synthetic-definitions", "definition", {"2025-01-01": instruments})
    stats, bars = {}, {}
    for offset, day in enumerate(days):
        rows = []
        for i in range(1, 8):
            rows.extend(
                [
                    statistic(i, 3, 70 + i + offset, day=day),
                    statistic(i, 6, 100 + offset * 10, day=day),
                    statistic(i, 9, (200 if i == 2 and offset else 100) + i, day=day),
                ]
            )
        rows.append(statistic(100, 3, -1, day=day))
        if offset == 1:
            rows.append(statistic(999, 9, 10, day=day))
        if scale > 1:
            # Distinct ordered revisions exercise chunking without inflating the catalog.
            original = rows
            rows = revisions(day, 71 + offset, scale * 25_000)
            from itertools import chain

            rows = chain(rows, original)
        stats[day] = rows
        bars[day] = [bar(i, 70 + i + offset, day=day) for i in range(1, 8)]
    batch(base, "synthetic-statistics", "statistics", stats, conditions={days[1]: "degraded"})
    batch(base, "synthetic-bars", "ohlcv-1d", bars)


def revisions(day: str, value: float, count: int):
    for n in range(1, count + 1):
        yield statistic(1, 3, value, day=day, sequence=n)


def catalog_workload(base: Path, contracts: int = 64, sessions: int = 252) -> None:
    """Two independent roots and all listed pairs; many keys, not just revisions."""
    definitions, futures, spreads = [], [], []
    for number, root in enumerate(("CL", "NG")):
        current = []
        for n in range(contracts):
            instrument = number * 100_000 + n + 1
            symbol = f"{root}-SYN-{n}"
            expiry = f"{2027 + n // 12:04}-{n % 12 + 1:02}-20"
            definitions.append(definition(instrument, symbol, root=root, expiry=expiry))
            current.append((instrument, symbol, expiry, n))
            futures.append((instrument, n))
        serial = number * 100_000 + 1_000
        for front in range(contracts):
            for back in range(front + 1, contracts):
                serial += 1
                near, far = current[front], current[back]
                for index, leg, side in [(0, near, "B"), (1, far, "A")]:
                    definitions.append(
                        definition(
                            serial,
                            f"{root}-PAIR-{front}-{back}",
                            root=root,
                            expiry=near[2],
                            leg_count=2,
                            leg=(index, leg[0], leg[1], side),
                        )
                    )
                spreads.append((serial, front - back))
    batch(base, "synthetic-catalog", "definition", {"2025-01-01": definitions}, roots=("CL", "NG"))
    days, day = [], date(2025, 1, 2)
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)

    def records(label):
        for instrument, position in futures:
            yield statistic(instrument, 3, 70 + position, day=label)
            yield statistic(instrument, 6, 100 + position, day=label)
            yield statistic(instrument, 9, 1000 - position, day=label)
        for instrument, value in spreads:
            yield statistic(instrument, 3, value, day=label)

    batch(
        base,
        "synthetic-statistics",
        "statistics",
        {d: records(d) for d in days},
        roots=("CL", "NG"),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=1)
    options = parser.parse_args()
    if options.scale < 1:
        parser.error("--scale must be positive")
    if options.out.exists() and any(options.out.iterdir()):
        parser.error("--out must be empty")
    example(options.out, options.scale)
