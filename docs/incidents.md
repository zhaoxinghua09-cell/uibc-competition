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
