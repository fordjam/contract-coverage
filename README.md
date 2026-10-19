# Minimum futures product coverage

Calculate minimum support for a target share of supplied daily futures activity,
across arbitrary families and venues. Monthly delivery rules preserve seasonal
gaps in the curve. Inputs are local CSV/Parquet exports; runtime makes no vendor
or API requests.

**Status:** public experimental source release. Review the GitHub Actions results
for the revision you use. Original code is [0BSD licensed](LICENSE):
anyone may use, modify, redistribute or sell it without an attribution requirement.
[Dependency and market-data rights remain separate](THIRD_PARTY.md).

## Quick start: generic coverage

Use Python 3.12 or 3.13 in an activated virtual environment, from this checkout:

```sh
python -m pip install .
product-coverage analyze --input examples/activity_export.csv \
  --mapping examples/activity_mapping.json --start 2024-01-01 --end 2024-12-31 \
  --target 0.99 --out out/training
product-coverage evaluate --input examples/activity_export.csv \
  --mapping examples/activity_mapping.json --policy out/training \
  --start 2025-01-01 --out out/holdout
```

These examples are synthetic. Training selects three outright slots across two
families and one listed-spread slot. The later ExampleA outright test captures
only 1%, illustrating why historical fitted coverage is not a future guarantee.
Open `out/training/summary.md` or `summary.csv`; exact reusable rules are in each
numbered group's checksummed `rules.json`.

Normalized inputs may be dataset directories, literal wildcards or explicit files.
Files are resolved and deduplicated independently of shell wildcard expansion:

```sh
product-coverage analyze --input out/canonical-data --target 0.99 --out out/coverage
```

The base installation uses Polars and portable timezone data, without a vendor
SDK. Approved Bloomberg or other vendor exports use a JSON column/constants
mapping. This is a file interface, not a live Bloomberg connector or a verified
Bloomberg field mapping. See [schemas and methods](docs/product-coverage.md).

## What “minimum” means

Each venue/family/category has its own target and one unit cost per product/slot.
Outright and directly traded spread volumes have separate denominators.

| Method | Quantity minimized | Policy |
| --- | --- | --- |
| `monthly-calendar` | Fixed concurrent capacity | Up to K delivery patterns per observation calendar month |
| `fixed-products` | Actual dated product count | One subset over the supplied period |
| `fixed-patterns` | Supplied C-label/pattern count | One subset of externally resolved labels |
| `daily-oracle` | Fixed concurrent capacity | Up to K products using each day's volume; hindsight lower bound |

Results are exact within these independent, equal-cost policy classes and target
**aggregate known supplied volume**, not every trading day. Missing volume stays
unknown. Unselectable known activity stays in the denominator. A full-universe
assertion needs independent completeness evidence. Group capacity totals do not
optimize shared spread-leg acquisition costs or a pooled target across families.
Monthly rules are fitted in-sample; evaluate a later holdout before relying on them.

## Three interfaces

| Interface | Purpose | Requirements |
| --- | --- | --- |
| `product-coverage` | Generic independent minima and frozen-policy evaluation | Python 3.12+; base installation |
| `futures-ts` | Local DBN preparation, continuous series and expiry-rank reports | Python 3.12+; Databento extra for ingestion |
| `contract_coverage.py` | Original supplied-data selector, including small coupled-cost/shared-leg problems | Python 3.11+; standard library only |

### Optional Databento preparation

```sh
python -m pip install '.[databento,test]'
python -m tests.generate_fixtures --out out/synthetic-source
futures-ts build --src out/synthetic-source --out out/synthetic-series --roots CL
futures-ts coverage --out out/synthetic-series
futures-ts check --out out/synthetic-series
product-coverage from-futures-ts --src out/synthetic-series \
  --start 2025-01-01 --end 2025-12-31 --out out/canonical-data
```

The GLBX ingestion adapter has a 35-root list; the generic engine has no fixed
family list. UTC-day bar volume remains separate from reference-session cleared
volume. Synthetic spread prices have no invented activity. See the
[futures-ts guide](docs/futures-ts.md). No real data belongs in Git.

### Original standalone selector

```sh
python contract_coverage.py --self-test
python contract_coverage.py --input examples/input.json \
  --policy examples/policy_target.json --output output/selector.json --summary
```

Create `output/` first. Its different budget/cost/point-in-time contract is in the
[input and policy reference](docs/input-and-policy.txt). Coupled exact search has
explicit size limits and labels approximations. The generic engine does not
claim to solve those shared-leg cost problems.

## Reports and compatibility

Generic reports use **format v2**. JSON retains exact identities; CSV is
spreadsheet presentation text, with formula/control escaping. Policy imports
verify hashes, training bindings, schemas, capacity and minimality certificates.
Regenerate legacy v1 policies from their original facts/window; they are not
accepted as frozen inputs. See the [changelog](CHANGELOG.md).

Generic CLI provenance omits input names/absolute paths by default. Reports still contain
identifiers and activity. Review them before sharing; use synthetic reproductions
in public issues. See [security reporting](SECURITY.md).

## Validation and contributions

```sh
python -m pip install -c requirements/validation.txt '.[databento,test,release]'
python -m pytest tests/ -q
python contract_coverage.py --self-test
```

CI is configured for package/core Python 3.12/3.13 on Linux, Windows and macOS.
The standalone selector has a separate Python 3.11–3.13 matrix. Release jobs
install wheel/sdist outside the source checkout, check core operation without a
vendor SDK, exercise synthetic pipelines, and scan history/installed dependencies.
Configured jobs are not proof of a green remote run.

See [contributing](CONTRIBUTING.md), [release gates](docs/PUBLIC_RELEASE.md), and
[implementation/validation report](docs/reports/packet-F1.md). Runtime commands
and pytest are offline. Package installation and explicit release security tools
use public services. PyPI publication is not required for a GitHub source release.
