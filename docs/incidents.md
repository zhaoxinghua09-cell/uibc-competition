# Public incident log

A competition that hides outages cannot be trusted with scores. This file records service incidents
(date, duration, category, what entrants were told). It is append-only.

| # | Date (CST) | Duration | Category | Impact | What entrants were told |
|---|---|---|---|---|---|
| 1 | 2026-11-12 | ongoing | `hosted-api-partial-network-block` | Hosted API returns `403` managed-link interstitial for some external networks; `OPTIONS` still `204` | "Part of the hosted API is unreachable from some networks — being fixed. Use the GitHub route." |

Category vocabulary: `hosted-api-partial-network-block`, `hosted-api-down`, `scorer-degraded`, `scorer-error`.

What is *not* an incident: a submission scoring badly. That is a result.

## Standing facts behind entry #1

- The daily smoke workflow in `uibc-core` was `success` on 2026-11-07 through 2026-11-11, so the API is
  reachable from at least one network (GitHub's).
- The `403` was observed from two independent external vantage points (a Linux VM and a cloud browser),
  and the transparent live page reported it itself.
- Therefore the accurate description is **partial network block**, not "backend down". Both
  "everything is fine" and "the service is down" would be false.
- Recovery criterion: ordinary-browser reachability **and** seven consecutive green smoke runs.


## Addendum (2026-11-13, CST) — corrections and continuation of entry #1

This addendum is **append-only**: no earlier sentence or table row was edited or removed.
Where an earlier standing fact no longer holds, it is quoted verbatim below and marked
SUPERSEDED with the date and evidence. This follows the rule that a historical PASS
must not be read as current trustworthiness.

- **SUPERSEDED (2026-11-13)**: "The daily smoke workflow in `uibc-core` was `success`
  on 2026-11-07 through 2026-11-11, so the API is reachable from at least one network
  (GitHub's)." Two corrections: (i) the workflow lives in **`uibc-competition`**
  (`.github/workflows/uibc-smoke.yml`), not `uibc-core`; (ii) runs 43207906549 (2026-11-12)
  and 43413749107 (2026-11-13) both returned HTTP 403 to the GitHub Actions runner
  itself, with every GET probe failing and a managed-link interstitial in place. The
  earlier green runs establish reachability **at that time only**.

- **Continuation of entry #1 (no new incident number)**: 2026-11-12 and 2026-11-13
  share the same cause (`hosted-api-partial-network-block`, 403 managed-link
  interstitial). On 2026-11-13 the `OPTIONS /join` probe degraded from `204` to `500
  (allow=)`; the other four OPTIONS probes remained `204`. This is an expanding
  degradation surface, not a new root cause.

- **Known assertion limitation (2026-11-13)**: the smoke suite's
  `pollution: board total unchanged` probe reports `FAIL` with detail
  "production board may be polluted" when the leaderboard is unreachable. The
  read-only arm performed **zero writes** (`writes_performed=0`) and the
  write-class negative probe was `SKIP`; this is an `UNKNOWN / UNAVAILABLE`
  condition, **not** evidence of pollution. Do not cite this probe as pollution
  evidence until the fix lands (pending per-item approval, `uibc_smoke.py` patch
  in this pack).

- **What entrants were told (additional, 2026-11-13)**: "The GitHub smoke route is
  also affected since 2026-11-12; do not treat a green/red smoke result from that
  route as a reachability proof while the block is in place." The original 2026-11-12
  wording above is retained verbatim.

- **Recovery criterion unchanged**: ordinary-browser reachability **and** seven
  consecutive green smoke runs. As of this addendum the green-run counter is **zero**;
  the launch post remains on hold. This addendum is not a recovery or unhold signal.
