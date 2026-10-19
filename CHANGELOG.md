# Changelog

## 0.1.0 — experimental public source release (2026-10-18)

- Generic per-venue/family/category minimum coverage, with monthly calendar,
  fixed-product, supplied-pattern and daily-hindsight objectives.
- Optional Databento daily DBN preparation, typed Parquet, as-of identities,
  continuous series and relative-contract reports.
- 0BSD license for original project code; third-party/data rights stay separate.
- Databento SDK and NumPy moved to the optional `databento` extra. Generic
  installation requires Polars and portable timezone data, without a vendor SDK.
- Hardened frozen policies: semantic validation, capacity assertions and training
  certificate checks for both file and in-memory inputs.
- **Report format v2:** exact policy rules are `rules.json`, bound to the training
  summary and SHA256 in `report.json`. `rules.csv` is a spreadsheet presentation
  export. Legacy unchecksummed v1 policy imports are rejected; regenerate them by
  running `analyze` against their original canonical facts and training window.
- Formula/control escaping in CSV presentation outputs across all three tools.
  JSON/Parquet identities remain unchanged. Numeric negative values stay numeric.
- Shell-independent input globs and dataset directories; resolved files are
  deduplicated. Literal brackets and spaces in filenames are supported.
- CLI provenance omits input paths/names by default; `--include-local-paths`
  explicitly restores them. Reports still contain potentially licensed activity.
- Fresh-process benchmark mode, isolated wheel/sdist smoke checks, constraints,
  secret/dependency scans, security guidance and synthetic-only bug reports.

Report schema version and package version are separate. Before the first tagged
release, interfaces may change as recorded here. After release, incompatible
schema changes require a new schema version and explicit migration guidance.
GitHub Actions records validation per revision. This source release does not
imply a package-index upload or a tagged binary release.
