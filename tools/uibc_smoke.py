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
import re
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


def unreachable(st: int) -> bool:
    """True when the probe got NO HTTP response at all (status sentinel 0)."""
    return st == 0


def _parse_bronze_floor(bronze_spec):
    """Structurally parse the declared bronze medal spec.

    Returns (floor, ok, reason):
      floor  — the minimum score required for bronze (must be > 0)
      ok     — True only when a machine-readable, numeric, positive floor is declared
      reason — human-readable explanation when ok is False

    Rules (no substring heuristics like "min" in the name — the field must
    actually parse as a number):
      * dict  : bronze.min_score must be numeric (bool excluded) and > 0
      * str   : must fullmatch ">=N" or ">N" with N numeric and > 0
      * anything else (prose such as "其余"/"others", missing, wrong type):
        NOT a floor -> the spec grants bronze to a 0-score entrant
    """
    if isinstance(bronze_spec, dict):
        v = bronze_spec.get("min_score")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, False, f"bronze.min_score is not numeric: {v!r}"
        if v <= 0:
            return None, False, f"bronze.min_score must be > 0, got {v!r}"
        return v, True, ""
    if isinstance(bronze_spec, str):
        m = re.fullmatch(r"(>=?)\s*(\d+(?:\.\d+)?)", bronze_spec.strip())
        if m:
            op, v = m.group(1), float(m.group(2))
            # semantics: bronze granted when score {op} threshold
            #   ">=N" excludes 0 only if N > 0;  ">N" excludes 0 as long as N >= 0
            ok = (v > 0) if op == ">=" else (v >= 0)
            if ok:
                return v, True, ""
            return None, False, f"bronze threshold '{bronze_spec}' does not exclude a 0-score entrant"
        return None, False, (f"bronze spec is non-numeric prose (no machine-readable floor): "
                             f"{bronze_spec!r}")
    return None, False, f"bronze spec has unsupported type: {bronze_spec!r}"


def run(allow_reject: bool) -> Result:
    res = Result()

    # ---- 0. control-point body (unknown path) — needed by the 404 discriminator
    st_ctl, body_ctl, _ = call("GET", f"{V}/this-path-does-not-exist-{datetime.now(timezone.utc).timestamp():.0f}")
    if st_ctl == 404:
        res.add("negative-control: unknown path", "PASS", f"unknown path -> {st_ctl} (probe CAN detect absence)")
    else:
        res.add("negative-control: unknown path", "FAIL",
                f"unknown path -> {st_ctl}" + (" (unreachable: no HTTP response)" if unreachable(st_ctl) else "") + " (absence undetectable)")

    # ---- 1. self-describing contract
    st, body, _ = call("GET", f"{V}/info")
    if st == 200:
        try:
            info = json.loads(body)
            n_ep = len(info.get("endpoints", {}))
            medals = info.get("medal", {})
            res.add("contract: GET /info", "PASS", f"200, {n_ep} endpoints declared, medal={medals}",
                    endpoints=sorted(info.get("endpoints", {}).keys()))            # Negative control for the medal spec: the declared bronze spec must be a
            # machine-readable, numeric, positive minimum-score floor. Missing field,
            # wrong type, or prose ("其余"/"others") all FAIL — the default must be
            # FAIL, never PASS, when the spec cannot be verified.
            medals = info.get("medal")
            if not isinstance(medals, dict) or "bronze" not in medals:
                res.add("medal-spec: zero-score exclusion declared", "FAIL",
                        f"/info medal spec missing or not an object: medal={medals!r} — "
                        "cannot verify the zero-score exclusion (absence of evidence is not compliance)")
            else:
                bronze_spec = medals.get("bronze")
                floor, ok, why = _parse_bronze_floor(bronze_spec)
                if ok:
                    res.add("medal-spec: zero-score exclusion declared", "PASS",
                            f"bronze spec = {bronze_spec!r} declares a machine-readable floor "
                            f"min_score={floor} (> 0) — 0-score entrants excluded by declared spec")
                else:
                    res.add("medal-spec: zero-score exclusion declared", "FAIL",
                            f"{why} -> a 0-score entrant is granted bronze by construction "
                            "(criterion: 0 must get NO medal)")
        except Exception as e:  # noqa: BLE001
            res.add("contract: GET /info", "FAIL", f"200 but unparseable: {e}")
    else:
        res.add("contract: GET /info", "FAIL",
                f"expected 200, got {st}" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

    # ---- 2. schema endpoint
    st, body, _ = call("GET", f"{V}/schema")
    res.add("contract: GET /schema", "PASS" if st == 200 else "FAIL",
            f"{st}, {len(body)} bytes" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

    # ---- 3. leaderboard (before) + internal consistency
    total_before = board_total()
    st, body, _ = call("GET", f"{V}/leaderboard?track=MEM&top=200")
    if st == 200:
        try:
            lb = json.loads(body)["leaderboard"]
            ranked = [float(r["best_total"]) for r in lb]
            monotonic = all(ranked[i] >= ranked[i + 1] for i in range(len(ranked) - 1))
            zero_rows = [r for r in lb if float(r.get("best_total", -1)) <= 0]
            res.add("leaderboard: GET", "PASS",
                    f"200, total_agents={total_before}, rows={len(lb)}, "
                    f"rank-order monotonic={monotonic}, zero-score rows={len(zero_rows)}")
            if not monotonic:
                res.add("leaderboard: rank order", "FAIL", "best_total not monotonically non-increasing by rank")
            # Inspect the ACTUAL medal/cert fields on zero-score rows — do not stop at
            # counting them. If a row exposes a non-empty medal/cert value, that is a
            # direct criterion violation. If the board exposes no such field at all,
            # medal granting is NOT verifiable from this endpoint (say so explicitly;
            # field-absent here does not prove the cert path is clean).
            if zero_rows:
                medal_keys = [k for k in ("medal", "cert", "certificate", "cert_id")
                              if any(r.get(k) not in (None, "", False) for r in zero_rows)]
                if medal_keys:
                    sample = {k: zero_rows[0].get(k) for k in medal_keys}
                    res.add("scoring: 0-score medal grant", "FAIL",
                            f"{len(zero_rows)} zero-score row(s) carry non-empty medal/cert fields "
                            f"{medal_keys} (sample: {sample}) — criterion violated")
                else:
                    res.add("scoring: 0-score medal grant", "OBSERVE",
                            f"{len(zero_rows)} zero-score row(s) expose NO medal/cert field on the "
                            "leaderboard (field absent, NOT verified-absent: grant may happen at "
                            "cert issuance — verifying requires an isolated-env write test)")
        except Exception as e:  # noqa: BLE001
            res.add("leaderboard: GET", "FAIL", f"unparseable: {e}")
    else:
        res.add("leaderboard: GET", "FAIL",
                f"expected 200, got {st}" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

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
        res.add("verify: fake cert (negative)", "FAIL",
                f"expected 404, got {st}" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

    # ---- 5. route existence for write endpoints (OPTIONS only; never POST)
    # A 5xx on OPTIONS is a real server defect (route exists but handler/capacity
    # is broken): it must FAIL the run, not pass as WARN. Previously 500s were
    # recorded as WARN, which let a "14 probes passed, 0 FAIL" verdict coexist
    # with three 500 responses in the same run (observed 2026-10-29, Run 40854189959).
    for ep in WRITE_ENDPOINTS:
        st, body, hdrs = call("OPTIONS", f"{V}/{ep}")
        if st in (200, 204, 405):
            res.add(f"route: OPTIONS /{ep}", "PASS", f"{st} (allow={hdrs.get('Allow', hdrs.get('allow',''))})")
        elif unreachable(st):
            res.add(f"route: OPTIONS /{ep}", "FAIL", "0 — unreachable: no HTTP response at all (host down or network failure)")
        elif st >= 500:
            res.add(f"route: OPTIONS /{ep}", "FAIL", f"{st} — real HTTP server error on preflight (intermittent if transient)")
        else:
            res.add(f"route: OPTIONS /{ep}", "WARN", f"{st} (allow={hdrs.get('Allow', hdrs.get('allow',''))})")

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
    # Distinguish three cases; "unverifiable" must NOT be counted as PASS evidence:
    #   - both totals numeric and equal  -> PASS (real before/after evidence)
    #   - both numeric and different     -> FAIL (possible pollution)
    #   - either None (no HTTP 200; sentinel 0 = unreachable) -> FAIL-UNVERIFIABLE:
    #     no evidence either way. (Run 40477811615 showed None -> None during a
    #     full outage; that is absence of evidence, not proof of cleanliness.)
    total_after = board_total()
    if total_before is not None and total_after is not None:
        if total_before == total_after:
            res.add("pollution: board total unchanged", "PASS",
                    f"total_agents {total_before} -> {total_after} (writes_performed={res.writes_performed})")
        else:
            res.add("pollution: board total unchanged", "FAIL",
                    f"total_agents {total_before} -> {total_after} — production board may be polluted")
    else:
        res.add("pollution: board total unchanged", "FAIL",
                f"UNVERIFIABLE: total_agents {total_before} -> {total_after} "
                "(no HTTP 200 from /leaderboard; sentinel 0 = unreachable) — "
                "absence of evidence is not evidence of no pollution")
    return res


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-reject-probes", action="store_true",
                    help="enable fail-fast negative POSTs (invalid credentials only). "
                         "ISOLATED-ENV ONLY: run together with --base pointing at a test "
                         "deployment — never enable write-class probes against production.")
    ap.add_argument("--base", default=BASE,
                    help="override API base URL, e.g. an isolated test deployment "
                         "(default: production %(default)s; read-only probes only)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    BASE = args.base

    write_class = args.allow_reject_probes
    print(f"uibc_smoke @ {BASE}  ({datetime.now(timezone.utc).isoformat()})  "
          f"[write-class probes: {'ON (isolated env expected!)' if write_class else 'OFF'}]")
    if write_class and "workbuddy.host" in BASE and "test" not in BASE:
        print("  !! REFUSING: --allow-reject-probes looks pointed at a production host "
              "(workbuddy.host without 'test' in the base URL). "
              "Write-class negative probes are ISOLATED-ENV ONLY.")
        return 2
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
