# futures-ts operator guide and data contract

Python 3.12; optional dependencies are declared in `pyproject.toml`. The original
selection script is independent. Runtime reads local DBN and Parquet only. It
does not instantiate Databento clients, retrieve credentials, or make requests.

## Commands

```sh
futures-ts build --src "$FUTURES_BATCH_ROOT" --out "$FUTURES_OUTPUT_ROOT" \
  --roots CL,NG --start 2020-01-01 --end 2026-08-10 --workers 2
futures-ts coverage --out "$FUTURES_OUTPUT_ROOT"
futures-ts check --out "$FUTURES_OUTPUT_ROOT"
futures-ts analyze --out "$FUTURES_OUTPUT_ROOT" --lookback 60 --target 0.95
```

Set the two shell variables to the local batch directory and a new output
directory. Omitting `--roots` selects all 35 supported roots. Date bounds are
inclusive output dates. Historical input still establishes rolling windows and
path-dependent rolls; later files can correct statistics referring to earlier
sessions. `--end` is not a historical publication-time cutoff.

Builds use a sibling staging directory, validate artifacts, then rename into
place. Existing nonempty output is refused. Choose a new directory for a rebuild.
`coverage` regenerates reports; `check` verifies hashes, schemas, partitions,
uniqueness, lifecycle bounds, price ranges, totals, synthetic signs and OI evidence.
Exit status is 0 on success, 2 for invalid input or artifacts. CLI stdout is JSON;
progress and errors go to stderr. Hashes cover Parquet artifacts; input file sizes are checked
against manifests and vendor hashes are retained without recomputing them.

## Input discovery and memory

Each recognized batch needs `metadata.json`, `manifest.json` and `condition.json`.
Only `GLBX.MDP3`, `.FUT` parents and `instrument_id` output symbology are accepted.
The directory may contain unrelated schemas, which are ignored. Missing downloads
listed in the manifest are reported separately from available days with no file.
An `available` condition does not establish a complete final day when the request
ended before UTC midnight. No absent observations are synthesized as zero.

A broader parent request supersedes a narrower overlapping request only when it
covers the same source-day time interval. Equal-scope downloads are deduplicated
only when their manifest content hashes match; otherwise their observations must
agree. Conflicting winning event ordering keys fail closed.

DBN is decoded in at most 65,536-record chunks, one source file at a time. Types
other than 3/6/9 are discarded and counted. Within-chunk superseded updates are
reduced before allocating identity dictionaries, then reduced across chunks and
source files again. Definition snapshots use a bounded cache. One/two root workers
reduce one reference month at a time, carrying rolling history across boundaries.
Temporary Parquet is compacted into one Zstd file per
root/year, with up to 65,536 rows per row group. Validation also works per partition.
No JSON copy of the full archive is produced. Benchmark full real workloads locally
before treating the synthetic RSS result as a production memory guarantee.

## Tables

All facts have typed dates, nullable values and root/year directories. A null root
is placed under `root=__unmapped__` and remains null inside the file; disable Hive
inference if using a third-party reader. Empty tables have a typed empty file.

| Table | Key/grain | Contents |
|---|---|---|
| instruments | definition_version | Contract identity, dated metadata, lifecycle, delivery month, structural eligibility, observed first/last dates |
| instrument_legs | definition_version, leg_index | Explicit leg ID/symbol, side and quantity ratio |
| bars_utc | date, publisher_id, instrument_id | Original UTC-day OHLC nanos and volume |
| daily_statistics | session date, publisher_id, instrument_id | Last settlement, cleared volume and OI, independent flags and publication times; includes excluded/unknown identities |
| daily_outright | date, contract_key | Wide analysis projection, expiry rank, prices, volume and ADV |
| daily_spread | date, kind, contract_key | Listed calendars and synthetic settlement differences for ranks 1–2 through 5–6 |
| daily_unmapped | date, publisher_id, instrument_id | Unknown observations with null instrument metadata |
| daily_excluded | date, publisher_id, instrument_id | Observed non-outright/non-calendar structures and explicit reasons |
| root_daily | date, root, instrument_class | Separate outright/calendar totals, counts, completeness and ADV |
| continuous | date, root, rank | Six roll ladders and three price variants, with settlement provenance |
| roll_dates | date, root, rank | Old/new identities, bridge prices, reason and timestamped OI evidence |
| source_quality | UTC source date, root, schema, batch | Conditions, expected/present file status and partial request days |

Canonical raw OHLC prices are nullable signed Int64 nanos, scale 1e9. Daily analysis
prices and ADV are Float64 in ordinary price/contract units; daily statistics also
retain settlement nanos. Counts/volumes/OI use unsigned integers; sentinel values
become null. Negative and zero prices remain valid. Synthetic spreads have no
invented OI, volume or OHLC. Their settlement flags reflect both source legs.

Expiry rank includes every active known outright, even those with no observations.
First/last seen describe processed observation history, including warmup before
`--start`. Definition metadata before the output start remains for provenance.

## Dates, missingness and provenance

Vendor IDs are resolved against the latest snapshot on/before the reference date,
with activation/expiration bounds. Stable contract keys include publisher, root,
class, raw symbol and lifecycle, preventing cross-year ID pooling. Multiple leg
records become one definition version. No future snapshot is borrowed backwards.

Calendar spreads require two complete explicit legs, same root, distinct delivery
months, one-for-one positive ratios, and opposite buy/sell sides. User-defined,
incomplete, cross-root, unknown-ratio and other structures are excluded with counts.
Symbols and missing legacy leg fields are not guessed. Inactive legs can become
eligible later within their recorded lifecycle without changing structural identity.
`listed_price_sign=1` means the exchange quote buys near/sells far; `-1` means it
buys far/sells near. Listed prices retain their exchange orientation. Synthetic
pipeline prices remain near-minus-far. Equal-side legs are not inferred as calendars.

Statistics are reduced by reference session and stat type. Latest means exchange
time, then receive time, sequence and channel. Delete clears that statistic without
clearing others. CME settlement bits record final/preliminary, actual/theoretical,
trading tick and intraday status. Null bit 7 clears the price; unsupported reserved
bits clear it and remain visible in coverage. A latest preliminary/theoretical or
intraday value is retained with its flags, not silently called a final settlement.

`ts_ref` is a UTC date label; do not localize it. Without it, use the 17:00
America/Chicago date boundary, with DST. Preserve which rule supplied each metric's
date. `ts_recv` remains the publication/availability evidence.

UTC daily bars cannot reconstruct 17:00 CT session OHLC. They remain in `bars_utc`.
Daily projections may attach the bar with the same date label, identified by
`ohlcv_basis=utc_day_same_date_label`; this does not make intervals equal.

ADV takes cleared volume, otherwise explicitly flagged UTC-bar volume. Windows
span the last 20/60 root session dates, not the last 20/60 nonmissing observations.
Means exclude unknown values; sample/session counts and partial flags remain.
Root ADV requires a complete eligible supplied-catalog aggregate for that day;
partial known totals remain visible without masquerading as full daily samples.
Neither these completeness flags nor the catalog assert exchange-wide completeness.

For a complete expected calendar, pass `--calendar` with exactly `root,date` CSV
columns and unique sessions for every selected root. Otherwise derive observed
session dates from statistics, or explicitly labeled UTC bar dates if statistics
are absent. Observed calendars leave rolling windows marked partial/unverified.

## Continuous series

Six ranks share a monotone front-contract roll schedule. Compare the current and
next contracts' most recent common OI reference date strictly before the new session,
using only values received before its 17:00 CT opening cutoff. A higher next OI rolls
the ladder forward. Missing/unavailable OI uses `--roll-days` (default five calendar
days before expiry); expired/unlisted contracts advance to a later expiry. Ranks do
not revert or substitute a quoted far contract for a missing near quote.

Final daily revisions are retained, not every preliminary vintage. A final revision
arriving after a historical cutoff is conservatively unavailable at that cutoff;
an overwritten earlier preliminary value is not reconstructed. OI evidence and
cutoffs are in the roll table. This is not a verified vintage replay/backtest engine.

Adjustments are backward, anchored to the last output-era contract. At a roll,
bridge using old/new settlements on that date. Earlier prices add the future
`new-old` gaps or multiply future `new/old` ratios. Missing bridges invalidate earlier
adjustments. Zero/negative bridges invalidate ratio adjustment, while differences
can remain valid. Latest settlement flags remain available on the continuous rows.

## Analysis API

### Relative-contract report

`futures-ts analyze` writes `analysis/report.html`, a self-contained offline view,
plus `rank_coverage.csv`, `recommendations.csv`, `contracts.csv`,
`series_availability.csv`, `source_quality.csv` and `analysis.json`.
`front_chain.csv` compares contiguous front chains using the same denominators.
Use `--start YYYY-MM-DD --end YYYY-MM-DD` for an explicit historical range;
otherwise it chooses the last 60 observed positive-cleared-volume dates, ending
before a source day marked partial. With no cleared volume it uses observed bar
dates. The date span can contain additional UTC bar dates, including Sundays.
This observed archive calendar is not a verified exchange calendar.

C1, C2, etc. are dated expiry ranks from the whole eligible as-of catalog, including
untraded contracts. They are independent of continuous-contract roll decisions.
The report labels calendars **far minus near**, so C3-C1 is third expiry minus first.
For an exchange-listed quote, that price is `-listed_price_sign * settlement`.
Activity is unsigned and is not duplicated to represent the reverse direction.

For each root and separate market category, the report sorts relative labels by
aggregate known activity. It gives the minimum fixed label count needed to meet
90%, 95%, 99% and the chosen target, assuming equal independent label costs. Required
outright legs are reported separately; leg costs and joint budgets are not optimized.
Selections are in-sample, not forecasts or point-in-time trading decisions.
Worst-day, fifth-percentile and daily target-attainment rates expose concentration
that an aggregate percentage hides. OI coverage sums instrument-days over the range.

Choose cleared volume or UTC-bar volume explicitly. No denominator mixes them.
Spread volume is direct listed-calendar activity only, never synthetic volume.
Unranked activity stays in denominators and may make a target unattainable. Unknown
IDs cannot be assigned to a root; their overall UTC-volume share remains visible.
Excluded structures are counted separately, so listed-calendar coverage does not
claim coverage of all spread structures or the entire exchange.

A separate price-completeness table measures outright settlement/volume/OI availability
and all synthetic pairs among C1 through C6, including C3-C1. Both leg settlements
must be present for a synthetic price. Fractions use all reference dates in the
analysis window; eligible-catalog date counts are retained separately. Missing
definitions or quotes do not become zero prices. Completeness describes availability,
not whether a settlement is final/actual; inspect retained flags for those requirements.

The analysis command replaces its own text reports and does not modify market
tables or source batches. Run `check` to verify build artifacts before analyzing an
existing copied build.

### Columnar access

```python
from datetime import date
from pathlib import Path
import polars as pl
from futures_ts.reader import scan_daily

result = (
    scan_daily(Path("out/series"), "CL", start=date(2025, 1, 1),
               columns=["date", "contract_key", "cleared_volume"])
    .group_by("contract_key")
    .agg(pl.col("cleared_volume").mean().alias("known_mean"))
    .collect()
)
```

`scan_daily` prunes root/year files before Polars pushes date filters and column
selection into scans. Choose metrics explicitly for a particular analysis.

For the existing selector, use a bounded payload from the repository:

```python
from contract_coverage import analyze
from futures_ts.selection import prepare_selection

payload = prepare_selection(Path("out/series"), "CL",
                            lifecycle_basis="activation_expiration",
                            policy={"mode": "budget", "budgets": {"outrights": 3, "spreads": 0}})
report = analyze(payload)
```

The explicit lifecycle choice accepts vendor activation/expiration as bounds;
actual last-trading/first-notice dates are not supplied by this module. Delivery
month must be explicitly present in definitions. The adapter uses cleared volume
only, preserves missing spread activity and marks catalog/PIT completeness unverified.
It includes upstream unknown/excluded counts and refuses payloads above a configurable
250,000 instrument-session budget. Use columnar queries for bulk archive work.

## References

- [Databento statistics and flags](https://databento.com/docs/knowledge-base/datasets)
- [UTC-day OHLCV conventions](https://databento.com/docs/schemas-and-data-formats/ohlcv)
- [Polars lazy optimizations](https://docs.pola.rs/user-guide/lazy/optimizations/)
