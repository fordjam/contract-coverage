# Before making this repository public

The repository should remain private until the owner explicitly requests a
visibility change. Preparing documentation or CI does not authorize publication.

## Prepared in the private repository

- [x] Keep the implementation portable and in one standard-library Python file.
- [x] Provide synthetic inputs, policies, expected decisions, and runnable docs.
- [x] Keep generated reports, real data folders, caches, and environment files
  out of normal Git staging with ignore rules.
- [x] Remove local filesystem paths from tracked documentation.
- [x] Add a CI workflow for embedded tests and CLI/CSV fixture checks on Linux
  and Windows, with a Python version matrix, read-only token permissions, and
  third-party action references pinned to immutable commits.
- [x] Document known data, coverage, point-in-time, and optimizer limitations.

These items describe repository setup. CI results still need to pass on the
actual pushed commit before treating a release candidate as verified.

## Owner decisions before publication

- [ ] Choose a license and confirm ownership/contribution rights. No license
  grant has been added in this private preparation step.
- [ ] Confirm redistribution rights for any future real vendor samples,
  instrument metadata, identifiers, report URLs, or derived reports. Keeping
  all public examples synthetic avoids introducing that data now.
- [ ] Choose a public project description, initial version, support scope, and
  maintenance expectations. This is currently a supplied-data CLI, not a
  hosted application or data acquisition service.
- [ ] Decide whether to publish only the repository or later add an installable
  package. Packaging is optional for the existing single-file CLI.
- [ ] Choose a private security contact and enable private vulnerability
  reporting if appropriate. Do not request sensitive datasets in public issues.

## Release validation

- [ ] Review the full Git history and GitHub metadata for credentials, real
  data, personal information, and accidental vendor artifacts. A clean current
  tree does not establish that earlier commits are clean.
- [ ] Run an appropriate secret scan and inspect every tracked example and
  generated fixture. Ignore rules do not retroactively remove committed files.
- [ ] Confirm the CI matrix passes on the exact release commit. Test another
  supported Python/platform combination before expanding compatibility claims.
- [ ] Independently review lifecycle/snapshot handling, point-in-time revision
  filtering, volume denominators, spread dependencies, and optimizer claims.
- [ ] Exercise representative synthetic production-sized chains; document
  runtime/memory behavior and the exact-search/heuristic boundary.
- [ ] Review malformed/untrusted input handling, CSV export handling, and
  bounded resource consumption. Add regression cases for any confirmed gaps.
- [ ] Decide which input/output fields form a stable versioned interface and
  record compatibility rules and a changelog for the first release.
- [ ] Confirm examples reproduce and all partial-data/unknown-denominator
  limitations are visible to users. Do not imply market-wide completeness or
  market performance from the synthetic example.
- [ ] Apply suitable branch/release protection for the chosen maintenance
  model, then make the repository public only with explicit owner authorization.

No exchange/vendor credentials, account links, or real market feeds are needed
for the current implementation or its CI workflow.
