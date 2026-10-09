# Changelog

All notable changes to the UIBC competition repository are documented in this
file. The format follows [Keep a Changelog](https://keepachangelog.com/) and
versions follow [Semantic Versioning](https://semver.org/).

A released section (e.g. `## [0.1.0] — YYYY-MM-DD`) is added **together with its
git tag**; the `repo-gate` check enforces that the newest released heading has a
matching tag, so a release can never be half-cut.

## [Unreleased]

### Added

- **`repo-gate` — a required check that can actually fail.** Re-runnable,
  falsifiable checks for: required files, CITATION metadata (incl. empty-value
  detection), CITATION ↔ CHANGELOG version sync, release-tag correspondence,
  **license-metadata consistency** and relative-link resolution (incl. titled,
  reference-style and `<...>`-wrapped links, and `..` escapes). Its `selftest`
  ships 16 negative controls, so the gate cannot silently regress. Deployed
  because a green checkmark that cannot fail proves nothing.
- **`uibc-smoke` — end-to-end smoke suite (read-only arm).** Exercises the live
  race API (`/uibc/v1/info`, `/uibc/v1/leaderboard`) and the auth boundary, with
  before/after leaderboard-total assertions so it can never silently write to
  the production board. Scheduled daily; also runnable on demand.
- **`SECURITY.md`**, **`CITATION.cff`**, issue + PR templates.

### Fixed

- **The deployed `repo-gate` was a revision that could not fail on license metadata, and
  `CITATION.cff` carried the value it should have rejected.** The gate shipped in the previous
  change passed `license family consistent: arr (LICENSE == CITATION.cff
  LicenseRef-AllRightsReserved)` — it printed the illegal string inside a **PASS** line and went
  green. CFF 1.2.0 requires `license:` to be a strict SPDX enum id, and `LicenseRef-*` is not a
  member, so the citation was invalid and the check certified it. Two separate defects: the
  metadata was wrong, and the checker could not see it. Neither is visible from a green badge —
  the gate had to be *asked to fail*, which is what its `selftest` is for. `CITATION.cff` now
  omits `license:` and declares terms by `license-url:` only (the spec's own fallback), and
  `tools/repo_gate_check.py` is replaced by the revision that validates the field against the
  enum and rejects every operand of an `OR` / `AND` expression. Stated counts, measured rather
  than asserted: `selftest` now reports **23 negative controls** (was 16 when this gate was
  first deployed) and 4 positive controls; `check` on this tree reports **GREEN** where the
  previous revision also reported GREEN, and the previous revision reports GREEN on the same
  tree with the illegal value restored — which is the point.
