"""Spreadsheet presentation escaping; canonical identities belong in JSON/Parquet."""

from __future__ import annotations

import json
import unicodedata

import polars as pl


def safe_cell(value: object) -> object:
    if isinstance(value, str) and (
        unicodedata.normalize("NFKC", value).lstrip().startswith(("=", "+", "-", "@", "'"))
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        # Escape controls as visible text and force formula-like strings to text.
        return "'" + json.dumps(value, ensure_ascii=True)[1:-1]
    return value


def safe_frame(data: pl.DataFrame) -> pl.DataFrame:
    return data.with_columns(
        [
            pl.col(name).map_elements(safe_cell, return_dtype=pl.String)
            for name, dtype in data.schema.items()
            if dtype == pl.String
        ]
    )
