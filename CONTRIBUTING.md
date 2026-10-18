# Contributing

This is a private, pre-release project. Preserve the single-source-file,
standard-library-only design and Python 3.11 compatibility.

Run the embedded suite from the repository root:

```sh
python3 contract_coverage.py --self-test
```

For a CLI change, also run the sample through JSON, CSV, and the readable summary:

```sh
mkdir -p output
python3 contract_coverage.py --input examples/input.json \
  --output output/report.json --csv output/recommendations.csv --summary
```

Add meaningful regression cases to `CoverageTests` for changed decisions,
historical ranks, missingness, accounting, or validation. When changing the
output schema or policy defaults, update the input/policy reference and explain
the compatibility impact. Keep ordering deterministic and JSON values finite.

Use only synthetic fixtures. Store real inputs in ignored `data/` and outputs
in ignored `output/`. Review `git diff --cached` and the staged file list before
pushing; filenames and ignore rules alone do not establish that data is safe to
share. Do not commit vendor extracts, credentials, or local machine paths.

When intentionally changing sample recommendations, regenerate the expected
CSV from `examples/input.json` and update `examples/summary.txt` together. These
files are illustrative fixtures, not evidence of real-market performance.
