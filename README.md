# Contract coverage

Select which actual futures outright contracts and exchange-listed calendar
spreads to support for one exchange and product family over a supplied period.
Rank daily activity, apply a support budget or coverage target, and explain
each support decision, including required spread legs and missing data.

**Current status:** private, pre-release. The repository includes synthetic
examples only. A public release and an open-source license have not been chosen.

## Run

Python 3.11 or newer; standard library only. No installation or external service
is required. From the repository root:

```sh
python3 contract_coverage.py --self-test
mkdir -p output
python3 contract_coverage.py --input examples/input.json \
  --output output/report.json --csv output/recommendations.csv --summary
```

Each **session is an exchange trading day**. Each daily observation holds the
volume for one actual outright contract month or one directly reported spread
pair. Supply the trading calendar and lifecycle catalog alongside daily rows.
The tool does not acquire exchange or vendor data.

## Limit outright support

For at most two outright contracts over the selected period:

```sh
python3 contract_coverage.py --input examples/input.json \
  --policy examples/policy_outrights.json \
  --start-date 2025-03-24 --analysis-date 2025-03-28 \
  --output output/outrights.json --csv output/outrights.csv --summary
```

Change `budgets.outrights` in the policy, or override it with
`--outright-budget 3`. Cost defaults to one per actual instrument; configurable
costs turn these counts into cost limits.

For one overall budget across outright and spread support, the sample input
uses `shared_budget: 4`, explicit category weights, and
`require_spread_legs: true`. Supporting a spread adds its actual legs and counts
each unique leg once. Outright and spread coverage always remain separate.

For minimum-cost support meeting coverage targets, use
`--policy examples/policy_target.json`. Individual CLI overrides take precedence
over a policy file, which replaces the input policy. Run `--help` for options.

## Synthetic example

With a two-outright limit, the sample selects **C1 and C4**, capturing **87.5%**
of supplied known outright volume. With the shared budget of four:

| Actual ID | Relative label | Daily ADV | Action | Reason |
| --- | --- | ---: | --- | --- |
| A | C1 | 100 | Support | Observed activity |
| B | C2 | 0 | Dependency only | Required leg of AB |
| C | C3 | 40 | Do not support | Capacity |
| D | C4 | 180 | Support | Observed activity |
| AB | S(1,2) | 80 | Support | Direct spread activity |
| BC | S(2,3) | 10 | Do not support | Capacity |

Total cost is four. Separate known-volume coverage is **87.5% outright** and
**88.9% spread**. Five supplied trading days make ADV20/60 partial.

See the [sample input](examples/input.json), [readable results](examples/summary.txt),
and [expected CSV](examples/expected_recommendations.csv).

## Behavior and limits

- Rebuilds historical C/S ranks from actual dated instruments, independently of
  volume. Actual spread identities survive rank changes and shared labels.
- Distinguishes confirmed zero, missing observations, and unverified calendar
  or catalog completeness. Missing volume never becomes confirmed inactivity.
- Returns activity, frequency bounds, rolling metrics, OI freshness, costs,
  reasons, coverage curves, and contiguous-chain comparisons in JSON/CSV.
- Separates historical decisions from future-session evaluation. Publication
  timestamps and a decision cutoff are needed to verify point-in-time inputs.
- Uses exact independent equal-cost selection and exact small coupled searches.
  Beyond the configured coupled-search limit it abstains unless approximation
  is explicitly enabled, in which case optimality is labeled `heuristic`.
- Coverage refers to the supplied known-volume universe and specified
  constraints. Unknown denominators leave the full objective unresolved.

The importable `analyze(payload: dict) -> dict` function performs no file I/O or
network access. The selection implementation and its self-tests remain in **one Python file**.

## Multiple vendors and larger universes: product-coverage

The Python 3.12 `product-coverage` module calculates exact per-family minimum
support from local CSV/Parquet activity exports, with no fixed family list.
Its default monthly calendar rules preserve seasonal gaps and target 99% of
known trading volume. It also supports fixed dated products, supplied C-label
patterns and a daily hindsight lower bound, with frozen-policy holdout tests.

```sh
python3.12 -m pip install .
product-coverage analyze --input examples/activity_export.csv \
  --mapping examples/activity_mapping.json --start 2024-01-01 --end 2024-12-31 \
  --target 0.99 --out out/product-coverage
```

See [data contract, exact methods and vendor integration](docs/product-coverage.md).
The configurable file adapter can accept approved Bloomberg exports; no live
Bloomberg connector or Bloomberg field semantics are assumed. It includes a
bridge from existing `futures-ts` Parquet outputs.

## Optional Databento data preparation: futures-ts

The separate `src/futures_ts/` package builds daily futures tables from local
GLBX.MDP3 DBN batch downloads. It requires Python 3.12 and the dependencies in
`pyproject.toml`; the standalone selection script above still requires only
Python 3.11 and the standard library.

```sh
python3.12 -m pip install '.[test]'
python3.12 -m pytest tests/ -q
python3.12 -m tests.generate_fixtures --out out/synthetic-source
futures-ts build --src out/synthetic-source --out out/synthetic-series --roots CL
futures-ts coverage --out out/synthetic-series
futures-ts check --out out/synthetic-series
futures-ts analyze --out out/synthetic-series --lookback 60 --target 0.95
```

Install from an approved local wheelhouse with pip's `--no-index --find-links`
options when offline. Builds and tests do not contact Databento, require an API
key, or load environment files. The generator creates synthetic DBN fixtures;
binary fixtures, Parquet and output directories are ignored by Git.

See the [implementation plan](docs/packet-F1-plan.md),
[data contract and operator guide](docs/futures-ts.md), and
[packet report](docs/reports/packet-F1.md). UTC-day bars remain separate from
session statistics. Unknown identities, preliminary/theoretical settlements,
missing volume, incomplete calendars and excluded structures stay explicit.

The adapter to `analyze` uses cleared volume only and requires an explicit
lifecycle-basis choice. It never fills absent spread cleared volume from UTC
bars; such selection problems can correctly remain unresolved.

The offline `analyze` report maps C1/C2 and C3-C1 labels to actual dated contracts,
compares 90/95/99% known-activity selections, and shows daily shortfalls and price
history completeness. Outright cleared volume and listed-calendar UTC volume remain
separate measures. Open the generated `analysis/report.html`; CSV/JSON results are
beside it. Synthetic spread prices never acquire invented volume or OI.

## Development and future publication

Read the [input and policy reference](docs/input-and-policy.txt),
[contributing guide](CONTRIBUTING.md), and [public-release checklist](docs/PUBLIC_RELEASE.md).
CI runs embedded tests and CLI/CSV smoke checks on Python 3.11–3.13 on Linux
and Windows. Vendor data belongs in ignored `data/`; generated reports belong
in ignored `output/`. Ignore rules do not substitute for reviewing staged files.

No `LICENSE` has been added. The owner must choose distribution terms before
an open-source release. Keep real vendor data and credentials out of commits.
