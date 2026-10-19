"""Batch discovery and bounded, offline DBN reads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import databento as db
import numpy as np

from .model import BuildError, PRICE_SCALE, ROOTS

DAY_FILE = re.compile(r"glbx-mdp3-(\d{8})\.(ohlcv-1d|statistics|definition)\.dbn(?:\.zst)?$")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    day: date
    schema: str
    roots: tuple[str, ...]
    batch: str
    start: int
    end: int
    expected_size: int
    content_hash: str


@dataclass
class Sources:
    files: list[SourceFile]
    quality: list[dict]
    skipped_subsets: int
    requested_roots: tuple[str, ...]


def _ns(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) * PRICE_SCALE


def dominates(a: SourceFile, b: SourceFile) -> bool:
    left, right = _ns(a.day), _ns(a.day + timedelta(days=1))
    return (
        set(a.roots).issuperset(b.roots)
        and max(a.start, left) <= max(b.start, left)
        and min(a.end, right) >= min(b.end, right)
    )


def discover(src: Path, roots: tuple[str, ...] | None = None) -> Sources:
    if not src.is_dir():
        raise BuildError(f"Source directory does not exist: {src}")
    requested = roots or ROOTS
    if not requested or len(set(requested)) != len(requested) or set(requested) - set(ROOTS):
        raise BuildError("--roots must contain unique supported CME roots")
    candidates, quality = [], []
    for meta in sorted(src.rglob("metadata.json")):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            query = payload["query"]
            schema = query["schema"]
            if schema not in ("definition", "statistics", "ohlcv-1d"):
                continue
            if query["dataset"] != "GLBX.MDP3":
                raise BuildError(f"{meta}: expected GLBX.MDP3")
            if query["stype_in"] != "parent" or query["stype_out"] != "instrument_id":
                raise BuildError(f"{meta}: expected parent -> instrument_id symbology")
            symbols = query["symbols"]
            if any(not isinstance(s, str) or not s.endswith(".FUT") for s in symbols):
                raise BuildError(f"{meta}: expected .FUT parent symbols")
            scope = tuple(sorted({s[:-4] for s in symbols}))
            selected = sorted(set(scope) & set(requested))
            if not selected:
                continue
            start, end = int(query["start"]), int(query["end"])
            if end <= start:
                raise BuildError(f"{meta}: invalid query interval")
            manifest = json.loads((meta.parent / "manifest.json").read_text(encoding="utf-8"))
            conditions = json.loads((meta.parent / "condition.json").read_text(encoding="utf-8"))
            if not isinstance(conditions, list):
                raise BuildError(f"{meta}: condition.json must contain a list")
            seen, present_days, expected_days = set(), set(), set()
            for entry in manifest["files"]:
                name = entry["filename"]
                if Path(name).name != name or "/" in name or "\\" in name:
                    raise BuildError(f"{meta}: unsafe manifest filename")
                match = DAY_FILE.fullmatch(name)
                if not match:
                    if ".dbn" in name:
                        raise BuildError(f"{meta}: unrecognized DBN filename {name}")
                    continue
                if name in seen or match[2] != schema:
                    raise BuildError(f"{meta}: duplicate filename or schema mismatch")
                seen.add(name)
                day = datetime.strptime(match[1], "%Y%m%d").date()
                expected_days.add(day)
                path = meta.parent / name
                if path.is_symlink() or meta.is_symlink():
                    raise BuildError(f"{meta}: symlinked source files are unsupported")
                if not path.exists():
                    continue
                size = int(entry["size"])
                if path.stat().st_size != size:
                    raise BuildError(f"{path}: size disagrees with manifest")
                present_days.add(day)
                candidates.append(
                    SourceFile(
                        path,
                        day,
                        schema,
                        scope,
                        meta.parent.name,
                        start,
                        end,
                        size,
                        entry.get("hash", ""),
                    )
                )
            condition_dates = set()
            for item in conditions:
                day = date.fromisoformat(item["date"])
                if day in condition_dates:
                    raise BuildError(f"{meta}: duplicate condition date")
                condition_dates.add(day)
                condition = item["condition"]
                if condition not in ("available", "degraded", "missing"):
                    raise BuildError(f"{meta}: unsupported condition {condition!r}")
                for root in selected:
                    quality.append(
                        {
                            "date": day,
                            "root": root,
                            "schema": schema,
                            "batch": meta.parent.name,
                            "condition": condition,
                            "file_present": day in present_days,
                            "file_expected": day in expected_days,
                            "partial_day": (start > _ns(day) or end < _ns(day + timedelta(days=1))),
                            "date_basis": "source_utc_day",
                        }
                    )
            for day in present_days - condition_dates:
                raise BuildError(f"{meta}: DBN day {day} absent from conditions")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BuildError):
                raise
            raise BuildError(f"Invalid batch {meta}: {exc}") from exc
    if not candidates:
        raise BuildError("No matching DBN batch files")
    kept, skipped = [], 0
    for source in sorted(
        candidates, key=lambda s: (s.schema, s.day, -len(s.roots), -(s.end - s.start), str(s.path))
    ):
        same = [other for other in kept if (other.schema, other.day) == (source.schema, source.day)]
        superior = [other for other in same if dominates(other, source)]
        if superior and (
            any(set(s.roots) != set(source.roots) for s in superior)
            or any(s.content_hash and s.content_hash == source.content_hash for s in superior)
        ):
            skipped += 1
        else:
            kept.append(source)
    return Sources(kept, quality, skipped, requested)


def arrays(source: SourceFile, count: int = 65_536) -> Iterator[np.ndarray]:
    """The SDK is used only for local decoding, never a Historical/Live client."""
    try:
        store = db.DBNStore.from_file(source.path)
        metadata = store.metadata
        if str(metadata.schema) != source.schema or metadata.dataset != "GLBX.MDP3":
            raise BuildError(f"{source.path}: DBN header disagrees with batch metadata")
        if str(metadata.stype_out) != "instrument_id" or str(metadata.stype_in) != "parent":
            raise BuildError(f"{source.path}: invalid DBN symbology")
        for chunk in store.to_ndarray(count=count):
            yield chunk
    except (ValueError, OSError, RuntimeError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"Cannot decode {source.path}: {exc}") from exc
