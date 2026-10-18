# Packet F1: offline futures time series

Implementation branch: `packet/packet-f1`. The original single-file selector is
unchanged. No real market data, credentials or generated binary artifacts are
included in this change. Quant Factory and the household-finance repository are
not modified.

## Built

- Python 3.12 `futures-ts build`, `coverage`, `check`, `analyze`, with an installable wheel
  and source distribution; existing selector remains Python 3.11/std-library.
- Local DBN decoding, source overlap selection, condition tracking and bounded
  early update reduction. As-of lifecycle identities prevent vendor-ID reuse
  from pooling different contracts. Multi-record legs resolve explicit calendars;
  unknown/excluded structures remain counted and inspectable.
- Wide daily statistics, outrights, listed/synthetic spreads, UTC bars, separate
  root totals, ADV20/60, six continuous ladders and auditable roll records.
- Compact Zstd root/year Parquet, raw price nanos, flags, provenance, nulls and
  publication timestamps. Root/month processing carries rolling/roll state across
  boundaries; typed writer batches avoid whole-year copies.
- Checksums, typed-schema and cross-table checks, safe staged publication and
  overwrite refusal. Coverage CSV/Markdown and a full unmapped-ID CSV.
- Lazy partition-pruned analysis API and an explicit-basis bounded adapter to the
  existing selector. UTC bar volume is not silently substituted in that adapter.
- Generated DBN fixtures, offline pytest checks and Linux/Windows CI definitions.
- Relative expiry-label activity selection at 90/95/99%, contiguous-chain comparisons,
  actual dated contract/leg mappings and all C1–C6 synthetic price-completeness pairs.
  Offline HTML and CSV/JSON distinguish aggregate activity from daily shortfalls and
  price-history completeness. Reversed exchange-listed calendars retain their sign;
  ambiguous equal-side legs stay excluded.

## Validation

On Python 3.12: **79 pipeline tests passed**, plus **24 original selector tests**
(103 total). Original JSON/CSV/policy smoke checks reproduce the committed example.
Ruff F/E9/B checks and the 100-column gate pass on new Python files. Wheel and
source distribution are built offline, and the installed CLI is exercised with
generated fixtures. The installed-wheel smoke test reuses already installed
third-party libraries: a clean offline dependency resolution could not complete
because Databento/tzdata wheels were absent from the registry cache. No network
fallback was used. A complete local wheelhouse or a separately authorized package
installation is needed for a fresh environment. CI definitions are added; remote
CI has not been run locally.

Tests cover ID reuse, updates/deletes across source files, independent statistic
missingness, missing/future/deleted definitions, multi-record legs and exclusions,
synthetic signs, OI publication lag, monotone rolls, expiry fallback, difference/
ratio adjustment and invalid bridges, holidays/missing observations, cross-year
ADV, degraded/missing/partial days, winter/summer 17:00 boundaries, root filtering,
worker determinism, source immutability, corruption, unsafe manifests, output
refusal, lazy file pruning and adapter scope limits.
Relative-contract tests also cover reversed/ambiguous sides, C3-C1 price direction,
unranked activity in denominators, aggregate-vs-daily coverage, whole-catalog ranks,
missing reference dates, synthetic completeness, contiguous-chain pair eligibility,
report generation and safe HTML embedding. An isolated installed-Chrome check
exercises category/target changes and desktop/mobile layouts without network requests.

Synthetic memory workload on an M4 / 16 GiB Mac, Python 3.12.13, Databento 0.86.0,
databento-dbn 0.69.0 and Polars 1.44.1:

| Workload | Messages decoded | Workers | Build time | Peak process RSS |
|---|---:|---:|---:|---:|
| Two roots, 252 sessions, 64 outrights/root plus all listed pairs | 1,112,832 | 2 | 13.088 s | 979.48 MiB |

It produced 1,048,320 daily statistic rows, 32,256 outright rows and 1,018,584
listed/synthetic spread rows; invariant checks passed. The workload measures many
distinct keys, not just repeated revisions. An earlier year-wide implementation
exceeded 2 GB on this workload; the delivered implementation reduces monthly and
avoids repeated wide joins/copies. This synthetic result is not a full real-data
memory or market-coverage certification.

Reproduce in a fresh ignored work directory on macOS/Linux:

```sh
python3.12 -m pytest tests/ -q
python3.12 contract_coverage.py --self-test
python3.12 -m tests.benchmark_synthetic --work out/memory-catalog --mode catalog
python3.12 -m tests.benchmark_synthetic --work out/memory-revisions --scale 5
```

## Mac commands

Install the project into its own Python 3.12 environment. Per CONTRIBUTING.md,
machine-specific filesystem paths are not committed. Set `FUTURES_BATCH_ROOT` to
the existing local batch directory and `FUTURES_OUTPUT_ROOT` to a new derived-data
directory. The operator can then run these exact commands from the checkout:

```sh
.venv/bin/futures-ts build --src "$FUTURES_BATCH_ROOT" \
  --out "$FUTURES_OUTPUT_ROOT" --workers 2
.venv/bin/futures-ts coverage --out "$FUTURES_OUTPUT_ROOT"
.venv/bin/futures-ts check --out "$FUTURES_OUTPUT_ROOT"
.venv/bin/futures-ts analyze --out "$FUTURES_OUTPUT_ROOT" --lookback 60 --target 0.95
```

Start with `--roots CL,NG` and an optional `--end` when inspecting a subset. Date
filters still scan historical warmup and later source publications needed for
reference-date revisions. A full 35-root offline real-data build is in progress,
authorized by the owner after synthetic acceptance. Its data, analysis reports
and machine-specific execution receipt remain outside Git.

The first historical execution stopped before publication on simultaneous
trading-tick/non-tick settlement representations. The reducer now retains the
precise non-tick representation only under the strict flag/action agreement rule
documented in the operator guide; unrelated conflicts still fail. Generated
regressions cover input-order independence and rejected flag mismatches.
`--history-start` provides an explicitly bounded recent build while preserving
the preceding as-of definition snapshot and declaring the roll initialization scope.

## Limits requiring operator interpretation

- Missing latest definitions remain unmapped. No future snapshot or guessed symbol
  is used to repair a historical gap. Legacy definitions without explicit complete
  legs remain excluded and counted.
- UTC bars are not CME-session bars. Root-wide ADV/calendar completeness is limited
  to the supplied as-of catalog and optional complete session calendar.
- Latest daily statistic flags remain visible, including preliminary/theoretical
  settlements. Unknown reserved settlement bits yield null values and counts.
- Final daily revisions do not reconstruct all preliminary historical vintages.
  OI that was published too late is conservatively unavailable at the roll cutoff.
- Shared front rolls, five-calendar-day fallback and backward adjustments are
  explicit conventions; ratio adjustments are invalid through nonpositive bridges.
- The standalone selector retains its documented solver limits. Its JSON adapter
  has a bounded scope; columnar queries are the path for bulk archive analysis.

Branch publication still requires human review. The user separately authorized
the offline real-data run and derived analysis after the initial implementation.
