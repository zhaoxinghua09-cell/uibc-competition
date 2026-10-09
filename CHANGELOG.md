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
