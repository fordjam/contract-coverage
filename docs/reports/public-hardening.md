# Public release hardening

Historical report for the local hardening pass on branch `packet/packet-f1`.
At completion of that pass no push, merge, release upload, GitHub protection
change or visibility change had been performed. The owner subsequently authorized
public source publication on 2026-10-18; GitHub records the publication PR and checks.

## Changes

- Original source is now 0BSD licensed, matching the owner's preference for
  unrestricted use without attribution. LICENSE and SPDX metadata are included
  in wheel/sdist. Dependencies and market-data rights are documented separately.
- Frozen evaluation validates rule types, states, candidates, priorities,
  capacity and training minimality certificates. The in-memory API receives the
  same checks as file imports; evaluation also asserts daily capacity.
- Report format v2 stores exact JSON rules with a training-summary binding and
  SHA256 in report.json. Spreadsheet CSV is presentation-only. Legacy unchecksummed
  v1 policies are rejected with regeneration instructions. Re-saving a validated
  policy preserves its identities and certificate; CSV edits cannot change it.
- Spreadsheet output escaping covers ASCII/fullwidth formula prefixes, leading
  whitespace, control/format characters and apostrophes across all three tools.
  JSON/Parquet identities and numeric negative values remain unchanged.
- Input files, literal globs and dataset directories are resolved/deduplicated
  inside the tool. Spaces and literal bracket filenames work without double globbing.
- Generic provenance omits input names/absolute paths by default. It does not
  anonymize licensed activity. UTF-8 summaries support non-ASCII family names.
- Databento/DBN/NumPy moved to an optional extra; the base package has Polars and
  portable timezone data. Missing ingestion dependencies produce an actionable error.
- README, method/compatibility docs, changelog, third-party notices, security
  reporting, synthetic-only issue form and release checklist are updated.
- CI now has vendor-free core jobs, a package matrix, fresh wheel/sdist checks,
  Gitleaks history/source scans, exact installed dependency audits and weekly
  dependency updates. Actions are SHA pinned with read-only token permissions.
- Benchmarks launch fresh analysis processes, excluding fixture-generation memory.

## Validation

The previous 161 tests passed but missed the release cases found by review.
The expanded suite has 193 pytest cases plus 24 embedded selector tests.
The final local receipts record Python 3.12 and 3.13 on macOS arm64; Linux and
Windows validation remains configured in CI and must pass on a pushed candidate.

The final suites on Python 3.12.13 and Python 3.13.15 each passed all 217 tests.
Original synthetic JSON/CSV/policy recommendations remain unchanged for ordinary identifiers.
Ruff F/E9/B/E501 and formatting checks cover changed package/release files.

Fresh virtual environments outside the checkout installed both wheel and sdist,
first as a vendor-free core and then with the Databento extra. Checks exercised
normalization, training, frozen holdout, synthetic DBN generation, build,
coverage, invariant checks, relative analysis, license metadata and the standalone
selector extracted from the sdist. No COT/developer libraries were injected into
those artifact environments. Twine metadata checks passed for both formats.

Gitleaks 8.30.1, verified against its pinned official archive SHA256, found no
leaks in all 12 pre-hardening reachable commits or in the nonignored source tree.
Reports use full redaction. The final commit is scanned again after committing.
GitHub logs/artifacts, unfetched refs and commit email/hostname privacy still
require the owner-side review described in the release checklist.

The first dependency audit identified a vulnerable pip supplied by the fresh
interpreter's bootstrap. It was upgraded to 26.2.1 in artifact validation, rather
than ignored. A strict pip-audit run against all 27 exact installed third-party
versions then found zero known vulnerabilities, with no skipped packages.
This is point-in-time evidence; future security checks may require updates.

The v2 rules generated from the existing five-year real canonical facts passed
capacity and minimality validation for all 70 groups. Counts remain 173 outright
and 487 listed-spread monthly slots, independently per root/category. No raw
vendor data or generated reports were committed, and no DBN rebuild was needed.

The fresh 200-family benchmark processed 400 groups and 5,040,000 synthetic facts
in 8.57 seconds with 117,325,824 bytes analysis-process peak RSS. Its repetitive
inputs compressed to 5,075,200 bytes. These are measured synthetic dimensions,
not a guarantee for another archive or an arbitrarily large single family.

## Remaining publication gates

Technical preparation does not make this a verified public release. Require:

1. Owner review of code/contribution rights, public scope of real-data-derived
   statements/history, commit identities and GitHub logs/assets.
2. An explicitly authorized release PR/push, followed by passing CI on its exact
   candidate, including Linux/Windows jobs not executable on this local Mac.
3. Verified branch/ruleset protections and a private vulnerability reporting
   route before visibility changes. These settings have not been modified here.
4. Explicit publication authorization. No automatic package upload exists.

See [release commands and checklist](../PUBLIC_RELEASE.md). Machine-specific
receipts, scanner reports, artifact environments and the real v2 report remain
outside Git. The hardening commit uses the owner's GitHub noreply identity for
new metadata; existing commit identities have not been rewritten.
