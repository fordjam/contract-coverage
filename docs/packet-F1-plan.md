# Packet F1 implementation and acceptance plan

The optional Python 3.12 `futures-ts` package prepares offline market data for
analysis. The original Python 3.11, standard-library `contract_coverage.py`
remains independently runnable. No exchange client, key or network request is
needed by the build. Only generated DBN fixtures enter tests or Git.

## Delivery sequence

1. Discover and validate batch manifests, conditions and parent scopes. Prefer
   complete broader requests to overlapping subset requests; reject ambiguous
   conflicting observations. Decode bounded DBN chunks, one source day at a time.
2. Resolve dated definitions, vendor-ID reuse and repeated leg records. Persist
   versioned instruments and legs. Retain unknown IDs and classify exclusions.
3. Reduce statistics across source files by reference session, honoring deletes,
   missing sentinels and CME settlement flags. Preserve publication timestamps.
   Keep UTC bars separate; daily analysis projections explicitly label date joins.
4. Produce wide daily tables, expiry ranks from the entire active outright catalog,
   cleared-volume-first ADV, listed and synthetic near-minus-far spreads, separate
   outright/spread root totals, continuous ladders and roll records.
5. Compact facts into Zstd Parquet root/year partitions. Produce coverage and run
   invariants. Publish a build only after validation; refuse to overwrite nonempty
   destinations. Root processing uses a bounded number of workers and monthly
   reductions, with state carried across month/year boundaries.
6. Run all existing regressions, generated-DBN integration tests, CLI/error and
   reproducibility checks, and an offline synthetic memory workload. Commit the
   source and report on `packet/packet-f1`; stop for review.

## Explicit semantics

- A stable contract key includes dataset, publisher, raw symbol, activation and
  expiration. Vendor IDs remain evidence, not global contract identities.
- Definitions are the latest eligible snapshot on/before the observation date;
  activation/expiration dates must contain the observation. No backfill from future
  snapshots and no guessed root or spread identity.
- Reference timestamps contain UTC date labels and are not localized. Without one,
  the trading date changes at 17:00 America/Chicago, including DST. Retain that basis.
- The final statistic is ordered by exchange time, receive time and sequence.
  Deletes/null flags clear the value. Preliminary, theoretical and intraday flags
  remain explicit; unknown reserved settlement bits produce unknown values.
- UTC-day OHLCV is never relabeled as session OHLCV. A same-date daily projection
  identifies the UTC bar basis; its volume fallback is explicitly marked.
- ADV uses the last 20/60 root sessions, averages known observations only, and
  reports sample counts and partial windows. An optional supplied session calendar
  can establish expected days; otherwise the calendar is labeled observed, never
  claimed complete. Missing observations are not zero or forward-filled.
- All six continuous ranks share a monotone front-contract roll schedule, preserving
  distinct ordered contracts. OI signals use prior reference sessions and must be
  published before the new session opens. Missing or later-revised OI is conservatively
  unavailable; this is not a reconstruction of every preliminary historical vintage.
  Fallback is five calendar days before expiration, configurable with `--roll-days`.
- Backward difference adjustment adds future roll gaps to historical prices; ratio
  adjustment multiplies future new/old bridge ratios. Missing bridges invalidate
  earlier adjustments. Nonpositive bridges invalidate ratio adjustments only.
- Date filters select output dates. Earlier source history is still processed to
  establish ranks, rolling windows and path-dependent rolls; later publications
  referencing the selected period are reduced too.

## Acceptance tests

Generate real encoded, Zstd-compressed DBN messages using `databento-dbn` and test
the CLI through Parquet reads. Required cases: ID reuse; update/delete across files;
settlement-only and OI-only rows; missing/future/expired definitions; multi-record
spread legs, UDS, butterflies and cross-root exclusions; negative synthetic sign;
OI crossover, publication lag, expiry fallback and adjustment bridges; holiday and
missing-day ADV; degraded/missing/partial source days; winter/summer 17:00 boundaries.

Also test overlap handling, stable root/year schemas, empty inputs, malformed
manifests, conflicting duplicates, calendar validation, year-boundary history,
worker determinism, root/date filters, zero and negative prices, corruption detection,
no network, no source mutation and safe output publication. Existing 24 analyzer
tests and committed example CSV must continue to pass.

The synthetic memory run records workload dimensions, command, elapsed time and
peak RSS. It does not certify memory use or data coverage for an unexecuted full
real-data build. The local operator runs that separately after review.
