# Packet F1: offline futures time series

Implementation branch: `packet/packet-f1`. The original selector's decisions are
unchanged; its CSV presentation exports are hardened. No raw market data,
credentials or generated binary artifacts are
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

The hardened candidate has **193 pytest cases** (84 pipeline, 53 generic coverage
and 56 release regressions), plus **24 original selector tests** (217 total).
See [the hardening report](public-hardening.md) for interpreter/artifact/security
results and remaining publication gates. Original JSON/CSV/policy smoke
checks reproduce the committed example.
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
Unmapped coverage counts distinct publisher/instrument identities separately by year.
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

## Real-data execution

The authorized full available archive build of all 35 roots completed and passed
invariant/checksum validation. It processed 10,349 selected source files, decoding
4,919,665 UTC bars and 590,264,341 statistics messages, with 70,940,475 retained
early-reduced candidate events. Final
Parquet tables occupy 1,844,095,333 bytes across 6,393 files. The complete two-worker
build took 2050.32 seconds with maximum process RSS of 1,507,753,984 bytes,
below the 2 GB limit. The execution receipt also records the platform's separate
peak-memory-footprint counter and phase times.

The first completed historical build passed artifact checks but exceeded the memory
limit at 2,346,926,080 bytes RSS. Its coverage report materialized the entire wide
unmapped table to count annual IDs. The query now aggregates only identity/year
columns before collecting. Standalone full-archive coverage fell from
1,643,380,736 to 563,036,160 bytes maximum RSS with identical results. The final
end-to-end rerun above verifies the fix. Every Parquet checksum matches the earlier
completed build; recent, full-history and all 17 annual JSON reports also agree
across the optimizations.

The separately bounded all-root build beginning 2025-01-01 also passed checks. It
decoded 626,505 UTC bars and 90,283,390 statistics messages in 311.48 seconds,
with 925,581,312 bytes maximum RSS and 233,006,585 bytes of final Parquet.

The default 60-reference-date report covers 2026-05-14 through 2026-08-10,
excluding the partial final activity source day. Against the full archive it
completed in 1.46 seconds at 228,163,584 bytes maximum RSS after pruning obsolete
definition snapshots. It retains the complete latest snapshot before the window
and all snapshots inside it. Its full JSON result exactly matches the bounded
build's report. Generated tests verify as-of equivalence, unobserved contracts
and first listings inside a window.

A full-history activity report completed in 6.20 seconds at 1,063,370,752 bytes
maximum RSS. These final analysis measurements use cached derived files. Annual
reports cover all 17 archive years, with combined price availability and 95% selection CSVs. Actual per-root observation ranges, field
availability, symbol/leg mappings, calendar gaps and daily target shortfalls remain
inspectable in the local derived output. Browser checks cover the recent report,
category/target changes, mobile layout and roots absent from early years. No browser
network requests were needed. Market extracts, reports and machine-specific command
receipts remain outside Git.

The analysis projects only required columns, aggregates unknown/excluded counts
lazily, and omits settlement-only listed rows from activity aggregation except for
the final date's symbol/price mapping. Root/year Zstd Parquet supports repeated
analysis without decoding the batch archive again. Price completeness, aggregate
volume capture and per-date attainment remain separate measures; these results do
not certify exchange-wide market completeness or a root-specific trading calendar.

## Mac commands

### Generalized multi-vendor coverage

The `product-coverage` CLI and importable `product_coverage` package remove the
fixed universe from the minimum-support calculation. CSV/Parquet column mappings
accept approved vendor exports; a Bloomberg live connection is not included.
The generic core imports no vendor SDK. Its default monthly delivery patterns
retain seasonal holes instead of requiring a contiguous first-N curve.

See [the full data contract, proofs, adapter procedure and examples](../product-coverage.md).
Methods explicitly distinguish monthly calendar capacity, fixed actual dated
products, supplied relative-label subsets and a daily hindsight lower bound.
Frozen-policy evaluation refuses overlap, accounting changes, absent policy
groups and unfitted new families. Missing activity and unselectable identities
remain visible; an unknown full-universe denominator is never certified by default.

The five-year real-data bridge/regression covers 2021-08-11–2026-08-10. All 70
root/category groups match the earlier independent scripts' counts and known
denominators under all three comparable methods (210 comparisons). Every
selected K passes the exact 99% threshold and K−1 fails:

| Method | Sum of per-root outright counts | Sum of per-root listed spread counts |
| --- | ---: | ---: |
| Monthly calendar capacity | 173 | 487 |
| Daily hindsight capacity | 171 | 410 |
| Fixed distinct dated products over five years | 1,066 | 3,702 |

These are independent unit-cost per-root/category objectives over aggregate
known volume, not a joint optimization of shared legs or a daily coverage guarantee.
Outrights use cleared reference-session volume; listed spreads use UTC-day bar
volume. Unknown-family observations remain in the upstream coverage audit.

The generic scale workload covers 200 synthetic families / 400 groups,
1,260 dates and 5,040,000 daily facts. A fresh analysis process completed in
7.71 seconds with 115,376,128 bytes peak RSS. Highly repetitive Parquet inputs
totaled 5,075,200 bytes. This is a synthetic scale check, not a vendor archive
compression or memory guarantee. The full generator/reproducer is
`tests/benchmark_product_coverage.py`; facts, measured receipt and real analysis
artifacts remain outside Git. Source and wheel packaging and the documented
synthetic train/holdout CLI example are checked offline.

With `FUTURES_OUTPUT_ROOT` pointing at the verified real build, run:

```sh
.venv/bin/product-coverage from-futures-ts --src "$FUTURES_OUTPUT_ROOT" \
  --start 2021-08-11 --end 2026-08-10 --out out/canonical-five-year
.venv/bin/product-coverage analyze --input out/canonical-five-year/*.parquet \
  --method monthly-calendar --target 0.99 --out out/generalized-99
```

Use `--method daily-oracle` or `fixed-products` in a new output directory for
the other two rows. For a larger vendor dataset, substitute normalized export
files and an explicit column mapping as documented in the operator guide.

### Existing futures-ts pipeline

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
reference-date revisions. The full 35-root offline real-data build completed after
the owner authorized real-data execution following synthetic acceptance. Its data, analysis reports
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
