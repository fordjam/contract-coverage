# Public release gates

Preparing this candidate does not authorize a push, merge, package publication
or visibility change. Keep the repository private until publication of a reviewed
candidate is explicitly authorized.

## Technical preparation completed locally

- [x] 0BSD license selected for unrestricted use without attribution; metadata
      and LICENSE included in wheel/sdist.
- [x] Generic core works without vendor SDKs; Databento is an optional extra.
- [x] Policy format v2: checksummed exact JSON, training bindings, semantic
      validation and capacity assertions in both import and API paths.
- [x] Spreadsheet presentation escaping and shell-independent input discovery.
- [x] Synthetic examples, documented objectives, missingness and compatibility.
- [x] Fresh wheel/sdist installation outside the source tree, synthetic CLI tests,
      dependency constraints and exact installed-version audit receipts.
- [x] Pinned-checksum Gitleaks history/source scan tool and CI job, without secret
      scan suppressions or dependency vulnerability allowlists.
- [x] Read-only, SHA-pinned Actions; package/core targets Python 3.12/3.13 across
      Linux/Windows/macOS, separately from the standalone 3.11–3.13 matrix.
- [x] Security guidance, synthetic-only issue forms, dependency updates, changelog.
- [x] Fresh-process benchmark separates analysis memory from generation.

The hardening report records actual local executions. Configured workflows or
checked preparation items do not establish a green remote CI run.

## Remaining owner and GitHub gates

- [ ] Confirm copyright/contribution rights for the selected code license.
- [ ] Confirm public scope of the real-data-derived counts/archive statistics in
      docs/reports/packet-F1.md against applicable dataset terms. Software licensing
      does not grant data rights. Keep/remove those statements as that review
      determines, including historical versions.
- [ ] Review GitHub issues/comments, logs/artifacts, releases, unfetched refs and
      author/committer email/hostname metadata. The local scanner does not cover
      every GitHub surface. No author/history rewriting is performed silently.
- [ ] Obtain a reviewed release PR and passing checks on its exact candidate;
      integration into the old default branch is a separate authorized operation.
- [ ] Enable/verify private vulnerability reporting or establish the private
      contact route in SECURITY.md before public use.
- [ ] Configure and verify branch/ruleset protection: reviewed changes, required
      tests/security/artifact checks, no force-push/deletion, approval policy
      appropriate for the maintenance team. Recheck settings after visibility changes.
- [ ] Choose release tag/support scope and explicitly authorize publication.
      PyPI is optional; no automatic package upload is configured.

## Reproduce release validation

Use Python 3.12 or 3.13 in a fresh virtual environment. Installation and security
services may use public endpoints; runtime commands and pytest do not.

```sh
python -m pip install -c requirements/validation.txt '.[databento,test,release]' pip-audit==2.10.1
python -m pytest tests/ -q
python contract_coverage.py --self-test
python -m build --no-isolation
python tools/artifact_smoke.py --dist dist --work out/artifact-validation
python tools/secret_scan.py
python -m pip_audit --strict --no-deps --disable-pip \
  -r out/artifact-validation/wheel/installed-requirements.txt
```

Run `python -m twine check` with explicit wheel and sdist paths. The artifact
helper verifies license metadata, fresh base/core operation without vendor SDKs,
then the optional SDK and synthetic futures-ts pipeline for each artifact. It
runs the standalone selector from the source archive. Receipts list exact public
third-party versions, excluding this unreleased project. Do not substitute a
private developer environment's entire installed-package list.

Gitleaks verifies a committed SHA256 for its pinned official binary. Supply
`--binary` to use a previously verified local executable. Reports redact all
findings; failures stop the gate. Dependency audits also stop on findings. Fix
and rerun rather than weakening checks. Audits are point-in-time evidence,
not security guarantees. See docs/reports/public-hardening.md for results.
