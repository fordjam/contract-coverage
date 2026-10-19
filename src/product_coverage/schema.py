"""A deliberately small daily activity contract; identity resolution is upstream."""

from __future__ import annotations

import polars as pl

GROUP = ["venue", "family", "category"]
ACCOUNTING = ["source", "metric", "unit", "date_basis"]
REQUIRED = ["date", *GROUP, *ACCOUNTING, "volume"]
OPTIONAL = ["product_id", "delivery_month", "near_month", "far_month", "pattern"]
COLUMNS = [*REQUIRED, *OPTIONAL]
METHODS = ("monthly-calendar", "fixed-products", "fixed-patterns", "daily-oracle")


def normalize(query: pl.LazyFrame, mapping: dict | None = None) -> pl.LazyFrame:
    """Project configured column names/constants to canonical columns, lazily.

    No symbol parsing, date/session conversion, fallback volume, or ID joins occur.
    Volume remains text until strict integer validation by the analysis engine.
    """
    mapping = {"version": 1} if mapping is None else mapping
    if not isinstance(mapping, dict):
        raise ValueError("Mapping must be an object")
    if set(mapping) - {"version", "columns", "constants"} or mapping.get("version") != 1:
        raise ValueError("Mapping must have version 1, columns and/or constants")
    columns, constants = mapping.get("columns", {}), mapping.get("constants", {})
    if not isinstance(columns, dict) or not isinstance(constants, dict):
        raise ValueError("Mapping columns and constants must be objects")
    if (set(columns) | set(constants)) - set(COLUMNS) or set(columns) & set(constants):
        raise ValueError("Unknown or multiply mapped canonical field")
    schema = query.collect_schema()
    names = set(schema.names())
    expressions = []
    for name in COLUMNS:
        if name in constants:
            if not isinstance(constants[name], str) or not constants[name]:
                raise ValueError("Mapping constants must be nonempty strings")
            expression = pl.lit(constants[name])
        else:
            raw = columns.get(name, name)
            if not isinstance(raw, str) or (raw not in names and name in columns):
                raise ValueError(f"Missing mapped input column for {name}")
            if raw not in names and name in REQUIRED:
                raise ValueError(f"Missing required column {name}")
            expression = pl.col(raw) if raw in names else pl.lit(None)
        if name == "date" and name not in constants and schema[raw] == pl.Date:
            expressions.append(expression.alias(name))
            continue
        expression = expression.cast(pl.String)
        if name in OPTIONAL:
            expression = pl.when(expression.str.len_chars() > 0).then(expression)
        if name == "date":
            expression = expression.str.to_date("%Y-%m-%d", strict=True)
        expressions.append(expression.alias(name))
    return query.select(expressions)
