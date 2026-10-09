#!/usr/bin/env python3
"""uibc_smoke — end-to-end smoke tests for the UIBC-MEM competition API.

Scope (recommendation #3 of the external verification): cover join / submit /
score / leaderboard / verify, plus NEGATIVE tests for failure responses and
invalid credentials.

Methodology: `online-scoring-service-blackbox-audit` (skill). Hard rules obeyed:
  - Read-only by default. Write-class endpoints (/join, /submit, /agent,
    /assist, /feedback) are probed with OPTIONS only.
  - Negative-credential probes (POST with a bogus token) are FAIL-FAST: the
    server rejects before any record exists. They are OFF unless
    `--allow-reject-probes` is passed, and are always bracketed by a
    before/after leaderboard-total assertion so "no pollution" is PROVEN,
    not claimed.
  - Every probe class has a NEGATIVE CONTROL that must be able to fail:
      * route-existence probe  -> a definitely-absent path must 404
      * semantic-404 probe     -> a fake cert body must DIFFER from the
                                  unknown-path control body, else the probe
                                  cannot distinguish "not found" from
                                  "route missing" (documented trap)

Usage:
  uibc_smoke.py                     # read-only arm
  uibc_smoke.py --allow-reject-probes   # + invalid-credential negatives
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://uibc-mem-race.app.workbuddy.host"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

V = "/uibc/v1"
WRITE_ENDPOINTS = ["join", "submit", "agent", "assist", "feedback"]


class Result:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.writes_performed = 0

    def add(self, probe: str, verdict: str, detail: str, **extra) -> None:
        row = {"probe": probe, "verdict": verdict, "detail": detail}
        row.update(extra)
        self.rows.append(row)
        print(f"  [{verdict}] {probe}: {detail}")

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["verdict"] == "FAIL"]


def call(method: str, path: str, body: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"User-Agent": UA, "Accept": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", {}


def board_total() -> int | None:
    st, body, _ = call("GET", f"{V}/leaderboard?track=MEM&top=200")
    if st != 200:
        return None
    try:
        return int(json.loads(body)["total_agents"])
    except Exception:  # noqa: BLE001
        return None


def run(allow_reject: bool) -> Result:
    res = Result()

    # ---- 0. control-point body (unknown path) — needed by the 404 discriminator
    st_ctl, body_ctl, _ = call("GET", f"{V}/this-path-does-not-exist-{datetime.now(timezone.utc).timestamp():.0f}")
    if st_ctl == 404:
        res.add("negative-control: unknown path", "PASS", f"unknown path -> {st_ctl} (probe CAN detect absence)")
    else:
        res.add("negative-control: unknown path", "FAIL", f"unknown path -> {st_ctl} (absence undetectable)")

    # ---- 1. self-describing contract
    st, body, _ = call("GET", f"{V}/info")
    if st == 200:
        try:
            info = json.loads(body)
            n_ep = len(info.get("endpoints", {}))
            medals = info.get("medal", {})
            res.add("contract: GET /info", "PASS", f"200, {n_ep} endpoints declared, medal={medals}",
                    endpoints=sorted(info.get("endpoints", {}).keys()))
            res.add("observability: medal threshold for score 0", "OBSERVE",
                    f"bronze spec = {medals.get('bronze')!r} -> does a 0-score entry get a medal? "
                    "(criterion: 0 must get NO medal)")
        except Exception as e:  # noqa: BLE001
            res.add("contract: GET /info", "FAIL", f"200 but unparseable: {e}")
    else:
        res.add("contract: GET /info", "FAIL", f"expected 200, got {st}")

    # ---- 2. schema endpoint
    st, body, _ = call("GET", f"{V}/schema")
    res.add("contract: GET /schema", "PASS" if st == 200 else "FAIL",
            f"{st}, {len(body)} bytes")

    # ---- 3. leaderboard (before) + internal consistency
    total_before = board_total()
    st, body, _ = call("GET", f"{V}/leaderboard?track=MEM&top=200")
    if st == 200:
        try:
            lb = json.loads(body)["leaderboard"]
            totals = [float(r.get("best_total", -1)) for r in lb]
            ranked = [float(r["best_total"]) for r in lb]
            monotonic = all(ranked[i] >= ranked[i + 1] for i in range(len(ranked) - 1))
            zeros = sum(1 for t in totals if t <= 0)
            res.add("leaderboard: GET", "PASS",
                    f"200, total_agents={total_before}, rows={len(lb)}, "
                    f"rank-order monotonic={monotonic}, zero-score rows={zeros}")
            if not monotonic:
                res.add("leaderboard: rank order", "FAIL", "best_total not monotonically non-increasing by rank")
            if zeros:
                res.add("scoring: 0-score entries on board", "OBSERVE",
                        f"{zeros} entrant(s) with best_total<=0 — per criterion they must NOT carry a medal")
        except Exception as e:  # noqa: BLE001
            res.add("leaderboard: GET", "FAIL", f"unparseable: {e}")
    else:
        res.add("leaderboard: GET", "FAIL", f"expected 200, got {st}")

    # ---- 4. verify — semantic 404 discriminator (positive control on the trap)
    fake = f"UIBC-MEM-9999-FAKE{int(datetime.now(timezone.utc).timestamp())}"
    st, body, _ = call("GET", f"{V}/verify/{fake}")
    if st == 404:
        # trap: a semantic 404 must differ from the unknown-path control body
        if body.strip() and body.strip() != body_ctl.strip():
            res.add("verify: fake cert (negative)", "PASS",
                    "404 with a *business-semantic* body (differs from unknown-path control) => route exists, cert absent")
        else:
            res.add("verify: fake cert (negative)", "FAIL",
                    "404 body identical to unknown-path control => cannot tell 'route missing' from 'cert absent'")
    else:
        res.add("verify: fake cert (negative)", "FAIL", f"expected 404, got {st}")

    # ---- 5. route existence for write endpoints (OPTIONS only; never POST)
    for ep in WRITE_ENDPOINTS:
        st, body, hdrs = call("OPTIONS", f"{V}/{ep}")
        ok = st in (200, 204, 405)
        res.add(f"route: OPTIONS /{ep}", "PASS" if ok else "WARN",
                f"{st} (allow={hdrs.get('Allow', hdrs.get('allow',''))})")

    # ---- 6. negative credential probe (fail-fast; opt-in)
    if allow_reject:
        st, body, _ = call("POST", f"{V}/submit", {
            "agent_id": "uibc-mem-2026-00000000",
            "token": "definitely-not-a-valid-token",
            "memory_package": {"entries": []},
            "manifest": {"track": "MEM"},
        })
        rejected = 400 <= st < 500
        res.add("negative: invalid credentials", "PASS" if rejected else "FAIL",
                f"bogus token -> {st} (must be 4xx; a 2xx would mean unauth submit is possible)")
    else:
        res.add("negative: invalid credentials", "SKIP",
                "not run (add --allow-reject-probes); write-class POSTs stay off by default")

    # ---- 7. pollution assertion
    total_after = board_total()
    if total_before is not None and total_before == total_after:
        res.add("pollution: board total unchanged", "PASS",
                f"total_agents {total_before} -> {total_after} (writes_performed={res.writes_performed})")
    else:
        res.add("pollution: board total unchanged", "FAIL",
                f"total_agents {total_before} -> {total_after} — production board may be polluted")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-reject-probes", action="store_true",
                    help="enable fail-fast negative POSTs (invalid credentials only)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    print(f"uibc_smoke @ {BASE}  ({datetime.now(timezone.utc).isoformat()})")
    res = run(args.allow_reject_probes)
    verdict = "GREEN" if not res.failed else "RED"
    payload = {"base": BASE, "when": datetime.now(timezone.utc).isoformat(),
               "verdict": verdict, "writes_performed": res.writes_performed, "rows": res.rows}
    print(f"verdict: {verdict}  ({len(res.failed)} FAIL / {len(res.rows)} probes)")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("written:", args.out)
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
