# CI cost optimization, 2026-10-19

## Scope

Based on main commit `faa3dbfd2661581c33d76942808e473a82dc1833`. Prepared on `packet/ci-cost-20261019`; no main/staging update, pull request, merge, deployment, or billing change is included.

All three workflows now run on pushes to main, on pull requests, and by manual dispatch. Feature-branch and tag pushes no longer start a duplicate run. A feature-branch change with an open PR still receives all 22 existing jobs on the PR event. Branch-only pushes without a PR no longer run CI; open a PR or use the existing manual dispatch when validation is needed.

Each workflow cancels superseded runs in the same workflow/event/ref group. Including the event name keeps manual and push runs separate; distinct PRs and workflows do not cancel each other. The latest run retains all original checks. These workflows contain testing, linting, local package builds, and validation only, with no release publication or deployment step.

## Validation performed

- Read repository contribution instructions; no AGENTS.md or repository-local agent skills were present in the inspected tree.
- Parsed all changed workflow YAML with PyYAML BaseLoader, preserving the GitHub Actions `on` key.
- Asserted every original job, step, matrix, timeout, permission, and other non-trigger field is unchanged. The entire file suffix beginning at `permissions:` is byte-for-byte identical.
- Asserted the exact intended trigger mapping and cancellation group/flag.
- Counted preserved matrix expansion: 6 synthetic + 6 generic core + 3 built-artifact + 1 secret scan + 6 selector jobs = 22 jobs.
- Checked group equality for a newer run of the same workflow/event/ref, and inequality across PRs, workflows, event types, and refs.
- No Mac coverage reduction, test weakening, path filter, or monitor changes.

## Limits and rollout

Actionlint was not installed in the validation workspace. Full application tests and GitHub-hosted runner execution were not run for this workflow-only change; YAML/semantic checks are not a full test pass. No required-check settings were changed.

The optimization is not active on main until separately reviewed and merged. The packet branch itself does not trigger these main-only push workflows, and no PR was opened. Review the diff, then run the full existing PR checks before any separately authorized merge. Older cancelled runs must not substitute for successful checks on the latest commit.
