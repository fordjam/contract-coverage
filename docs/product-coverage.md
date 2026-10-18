# Generalized minimum product coverage

`product-coverage` calculates minimum support for any number of futures families
and venues from daily activity facts. There is no 35-root list in this module.
It reads approved local CSV/Parquet exports, including exports prepared from
Bloomberg, and has an adapter for existing `futures-ts` outputs. It performs no
network requests and needs no vendor credentials. The Bloomberg path is a
configurable **file adapter**, not a live Bloomberg connection.

The default question is: **for each venue, family and category, what is the
smallest fixed concurrent capacity whose monthly delivery rules capture 99%
of supplied known volume over the training period?** Outright and directly
traded spread volume are independent objectives. This preserves gaps in the
curve: a far December contract can be selected without intervening months.

## Run a complete synthetic example

Install the repository with Python 3.12 (`python3.12 -m pip install .`), or
install an approved wheel from a local wheelhouse when offline. Existing
`futures-ts` installation/dependencies remain compatible. The generic core
imports Polars, not a vendor SDK.

```sh
product-coverage analyze --input examples/activity_export.csv \
  --mapping examples/activity_mapping.json \
  --start 2024-01-01 --end 2024-12-31 --target 0.99 \
  --method monthly-calendar --out out/calendar-training

product-coverage evaluate --input examples/activity_export.csv \
  --mapping examples/activity_mapping.json \
  --policy out/calendar-training --start 2025-01-01 --end 2025-12-31 \
  --out out/calendar-test
```

The synthetic training result is ExampleA: one outright slot and one spread
slot; ExampleB: two outright slots. The later ExampleA outright test captures
only 1%: the evaluator applies the frozen February rule and does not refit to
the new December activity. This deliberately illustrates why an in-sample
99% result is not a promise about future trading.

`python -m product_coverage.cli` provides the same commands. Every output path
must be new. `report.json` is the completion marker; a failed run can leave
partial diagnostic files without a completed report.

## Canonical daily activity contract, version 1

One row represents one resolved economic product on one observation date.
CSV uses ISO dates and plain integer volume; Parquet is preferred for repeated
analysis. Supply long-form facts, rather than one ticker per spreadsheet column.
Materialized normalized Parquet uses a Date column, Int64 volume and String
identity/metadata columns. Calculations promote volume to Int128 for sums.

| Field | Meaning |
| --- | --- |
| `date` | ISO `YYYY-MM-DD`, already assigned to the stated trading/session basis |
| `venue`, `family` | Arbitrary nonempty identifiers; identical family names on different venues remain separate |
| `category` | Exactly `outright` or `listed_spread` |
| `source` | One reconciled source/dataset identifier per venue/family/category |
| `metric` | E.g. explicitly chosen cleared volume or reported trade volume |
| `unit` | E.g. `contracts`; units are never converted implicitly |
| `date_basis` | E.g. `exchange_session` or `UTC_day`; not inferred from timestamps |
| `volume` | Nonnegative integer or null, at most 2^63−1 per observation |
| `product_id` | Stable economic identity for an actual dated product; null if unresolved |
| `delivery_month` | Optional outright delivery `YYYY-MM`; required for its calendar rule |
| `near_month`, `far_month` | Optional actual leg delivery months; near must precede far; required for a calendar spread rule |
| `pattern` | Optional externally resolved relative label such as `C1` or `C3-C1`, used by `fixed-patterns` |

All columns other than the final five are required. Optional columns may be
omitted. Missing `volume` stays unknown; a zero is a confirmed observation.
Missing rows/days are never manufactured or filled with zeros. Activity with
missing identity or pattern stays in the known-volume denominator but cannot
be selected by methods requiring that metadata. An unattainable target returns
`minimum_capacity: null`, `optimality: unresolved`, and no recommendation.
An entirely zero/missing group is also unresolved, not evidence for zero support.

Resolve updates, corrections/deletes, vendor symbol aliases, recycled IDs and
contract lifecycles **before** supplying facts. Duplicate date/product rows and
IDs that change delivery identity are rejected. A single pattern must name at
most one positive-volume economic product on a date; otherwise a one-slot
claim would be false. Resolve aliases explicitly rather than adding duplicate
vendor copies. As-of joins require the latest definition available on/before
the observation and a valid lifecycle. Never join recycled IDs without time.

The engine rejects mixed sources, metrics, units or date bases within a
venue/family/category. It never adds outright and spread denominators together.
Do not use open interest as trading volume. Synthetic price differences have
no independently traded volume; they must not be supplied as listed spreads.
Calendar rules support two-leg same-family calendars. Other listed structures
need their own explicit upstream identity/pattern policy and the fixed methods.

Coverage is of **known supplied activity**. Absent products, missing sessions
and unreported observations cannot be discovered from a sparse export alone.
Default reports therefore say `denominator_status: known_only`.
`--universe-complete` records an operator assertion that the supplied universe
and observations are complete; missing volume still prevents verification of
the full-universe objective. Do not set this flag merely because parsing passed.

## Minimum objectives and exact methods

Every product/slot has unit cost. The target is an exact rational parsed from
the decimal command argument; integer comparisons determine the threshold.
Int128 aggregation and Python integer threshold checks avoid UInt64 overflow
and rounding a 98.999999% result up to 99%.

| Method | What is minimized | Policy |
| --- | --- | --- |
| `monthly-calendar` (default) | Uniform concurrent capacity K per group | At most K delivery patterns in each observation calendar month |
| `fixed-products` | Number of actual dated products over the whole period | One fixed subset of `product_id` values |
| `fixed-patterns` | Number of relative patterns over the whole period | One fixed subset of supplied labels, e.g. C1, C3-C1 |
| `daily-oracle` | Uniform concurrent capacity K per group | At most K actual products per observation date, selected using that day's volume |

For calendar rules, an outright pattern is `(delivery year − observation year,
delivery month)`. A spread uses both legs' offsets/months. For example, state
`1` and candidate `Y2-M12` select December two years after the observation year
during January. `Y0-M03/Y2-M12` selects the March-current-year / December-two-
years-ahead pair. The pair is ordered near/far for identity; the conventional
price label `Cfar-Cnear` is a separate price-sign choice. No price is calculated
by this activity engine. Rules do not imply all intervening contracts are needed.

For each state s and candidate i, sum activity to obtain w(s,i). Sort candidates
within each state by descending activity, with candidate text as a deterministic
tie break. The curve for capacity K is:

```text
D = sum of all known activity, including unselectable activity
F(K) = sum over states s of the K largest candidate weights w(s,i)
minimum K = first K for which F(K) * target_denominator >= D * target_numerator
```

The fixed methods have one state (`all`); monthly rules have 12 possible states;
the daily oracle has one state per observed date. For a given K, each state's
top K maximize its contribution independently. Therefore no other allowed
K-slot selection can exceed F(K). The first passing K is exact within that
policy class, and the reported K−1 capture is a minimality certificate.

The daily oracle is a hindsight lower bound for repeatable calendar policies.
Monthly rules are fitted in-sample and can overfit; use a later holdout period.
Fixed dated products count historical expiries, rather than reusable slots.
Fixed C-label rules require ranks based on the complete eligible contract
catalog for each date, including inactive-volume contracts; never rank by volume
or rename gaps away. The core does not guess C ranks from an incomplete export.

The target applies to **aggregate period volume**, not every trading day.
`daily.csv`, `days_meeting_target` and `minimum_daily_capture` show shortfalls.
Totals sum independently required per-group capacities: every group has its
own target. They are not the smaller solution to pooling all families' volumes.
There is no optimization of shared spread-leg acquisition costs or unequal
product costs here. For small coupled-cost problems use the original selector
and its explicit exact-search limits in `input-and-policy.txt`.

## Connecting a larger Bloomberg or other vendor export

1. Acquire the export through your authorized vendor workflow. Export actual
   dated contracts and directly reported spread trading volume, plus identity,
   delivery and session metadata. Generic continuous tickers alone cannot
   establish the full volume denominator or resolve actual delivery gaps.
2. Reconcile the vendor's meaning of volume, revisions, session dates,
   identifiers, spread structures and completeness. Resolve identity as-of each
   date. Convert wide exports to long form and explicit canonical category values.
3. Copy `examples/activity_mapping.json` and map your approved column names.
   `columns` maps canonical field to export field; `constants` supplies dataset
   metadata. The example names are synthetic, **not Bloomberg field codes**.
   Column mapping does not invent absent spread volume or normalize vendor
   semantics. Separate incompatible datasets instead of concatenating copies.
4. Run `normalize` once to materialize reusable Zstd Parquet, then analyze it.
   Normalization projects names/types; strict accounting/identity/volume checks
   run during analysis. Review missingness and denominator status in the result.
5. Fit on your chosen five-year window and evaluate frozen rules on later data.
   Retune only as a separately versioned new training run. The evaluator refuses
   overlapping windows, daily-oracle policies, accounting changes, missing policy
   groups and new groups without a fitted policy. A calendar month absent in
   training has no rules.

```sh
product-coverage normalize --input approved-export.csv --mapping vendor-map.json \
  --out normalized.parquet
product-coverage analyze --input normalized.parquet --start 2021-08-11 \
  --end 2026-08-10 --target 0.99 --out out/vendor-99
```

No Bloomberg field names, data entitlements, live connection, or Bloomberg
coverage results have been verified by this implementation. An adapter should
be validated against independently supplied vendor totals before claiming them.

## Reuse existing Databento outputs

```sh
product-coverage from-futures-ts --src "$FUTURES_OUT" --start 2021-08-11 \
  --end 2026-08-10 --out out/canonical-five-year
product-coverage analyze --input out/canonical-five-year/*.parquet \
  --target 0.99 --method monthly-calendar --out out/monthly-99
product-coverage analyze --input out/canonical-five-year/*.parquet \
  --target 0.99 --method daily-oracle --out out/oracle-99
```

This adapter uses outright cleared session volume and separately listed spread
UTC-day bar volume; it excludes synthetic spreads. It resolves spread delivery
months from as-of, lifecycle-valid, nondeleted definitions. Economic delivery
aliases aggregate explicitly; multiple positive outright aliases on one date
are rejected. All-null volume remains null. It cannot attribute unknown-family
rows in `daily_unmapped`; consult the source `coverage.md/.csv` and do not assert
a complete universe. The existing GLBX ingestion root list remains local to
that adapter. Adding a vendor/universe does not require editing the generic core.

## Outputs, reproducibility and API

`report.json` records version, exact target, grouping/accounting, source file
SHA256s, mapping, window, completeness assertion and runtime versions.
`summary.md`/`.csv` give counts and coverage; every numbered group directory
contains `rules.csv`, the full `curve.csv`, and `daily.csv`. Rules retain state,
candidate, training weight and priority. Evaluate runs additionally record
training dates and hashes of the frozen report/rules. Counts are never inferred
from a first-to-last C range.

```python
from datetime import date
from pathlib import Path
from product_coverage import analyze, evaluate
from product_coverage.files import scan_files, write_report, read_policy

facts = scan_files([Path("normalized.parquet")])
# Iterators: write each family's result without retaining all daily facts.
write_report(analyze(facts, target="0.99", method="monthly-calendar",
                     start=date(2021, 8, 11), end=date(2026, 8, 10)),
             Path("out/training"), {"description": "approved daily export"})
policy = read_policy(Path("out/training"))
write_report(evaluate(facts, policy, start=date(2026, 8, 11)),
             Path("out/holdout"), {"description": "later observations"})
```

Custom adapters return canonical `pl.LazyFrame` facts (or use `normalize` with
a mapping); they never need access to optimizer internals. API callers supply
their own provenance to `write_report`; the CLI hashes file inputs automatically.

## Scale and validation

Prefer narrow Zstd Parquet facts, partitioned by venue/family/category/year,
with row-group statistics and dates sorted inside a partition. Avoid repeated
wide spreadsheet scans. The file adapter lazily projects/filter scans; the
engine materializes one venue/family/category at a time and emits it before
the next group. Working memory scales with the largest group's window and
policy, not the full universe. Group discovery and filtered scans still cost
I/O; this is not a distributed engine or a guarantee that an arbitrarily large
single family fits under 2 GB. Normalize once, prune the date window, and split
upstream data sensibly. No universe-sized pairwise combination matrix is built.
Only observed listed pairs enter spread optimization.

The file reader batches files with identical physical schemas into one scan,
rather than constructing a separate scan plan per file for every family. This
also handles null-only optional columns in separate outright/spread partitions.
On the validation Mac, the 200-family benchmark analyzed 400 groups and
5,040,000 daily rows in 7.71 seconds at 115,376,128 bytes peak process RSS.
Its highly repetitive synthetic facts compressed to 5,075,200 Parquet bytes;
these figures are not a size/speed guarantee for a vendor archive. Reproduce:

```sh
python3.12 -m tests.benchmark_product_coverage \
  --work out/generic-scale --families 200 --days 1260
```

The command includes generation in its RSS high-water mark and times only
analysis/report writing. The recorded analysis was measured in a fresh process
against already generated facts; its receipt and facts remain outside Git.

Tests compare monthly minima against exhaustive independent subset searches,
exercise 240 venue/family groups, seasonal holes, spread pairs, all four
objectives, exact large-integer thresholds, duplicates, aliases, missingness,
accounting mismatches, deterministic ties, file/CLI round trips and frozen
holdout evaluation. The Databento bridge is tested using generated DBN data.

Run `python3.12 -m pytest tests/ -q` and
`python3 contract_coverage.py --self-test`. See the packet report for recorded
real-data regression results and measured limits; synthetic tests alone are
not vendor completeness certification.
