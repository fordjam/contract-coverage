# Security and support

This experimental CLI processes local files. It is not a hosted service or a
sandbox for hostile uploads. The project is maintained on a best-effort basis;
there is no response-time or production-support commitment.

## Report privately

Once private vulnerability reporting is enabled for the public repository, use
https://github.com/fordjam/contract-coverage/security/advisories/new.
If that option is unavailable, open an issue requesting a private contact channel
**without vulnerability details or attachments**. Wait for that channel before
sharing details. Maintainers must enable and verify this route before publication.

Describe the affected version, Python/platform, impact and a synthetic minimal
reproduction. Never send credentials, environment files, licensed vendor data,
real activity reports, or private source paths. Do not open a public issue with
an undisclosed vulnerability's reproduction.

## Handling files and reports

- Use trusted local datasets. Resolve vendor revisions, aliases and lifecycles
  upstream. A complete-universe assertion is an operator assertion, not a scan
  that proves the export is complete.
- Reports use spreadsheet text escaping for formula-like strings and control
  characters. CSV is a presentation format; exact identities and frozen rules
  are stored in checksummed JSON. Do not import CSV display strings as policies.
- Default generic CLI provenance omits input names and absolute paths. This does not
  anonymize activity, identifiers, mapping constants or report contents. Review
  every diagnostic before sharing it. `--include-local-paths` is an explicit opt-in.
- A policy hash detects accidental modification; it does not authenticate the
  sender. Use policies from trusted sources. Schema, capacity and minimality
  certificate checks run independently of hashes, including in-memory policies.
- Runtime commands and pytest require no service/API access. Dependency
  installation, release secret scanning and vulnerability checks use public
  package/scanner services outside the runtime and test harness.

The initial supported package validation targets are Python 3.12 and 3.13.
The standalone selector is separately tested on Python 3.11–3.13. Only versions
with successful release checks should be labeled verified in release notes.

## Release checks

`tools/secret_scan.py` verifies a pinned official Gitleaks binary and scans all
locally reachable Git history plus nonignored source files, with full finding
redaction. It does not inspect GitHub logs, issues, artifacts or unfetched refs.
The release workflow audits exact third-party versions installed in fresh
artifact environments with pip-audit; it does not silently ignore findings.
Do not treat either scan as a guarantee that no vulnerability or secret exists.
