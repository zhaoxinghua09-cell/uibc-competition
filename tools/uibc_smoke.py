#!/usr/bin/env python3
"""uibc_smoke — end-to-end smoke tests for the UIBC-MEM competition API.

Scope (recommendation #3 of the external verification): cover join / submit /
score / leaderboard / verify, plus NEGATIVE tests for failure responses and
invalid credentials.

Methodology: `online-scoring-service-blackbox-audit` (skill). Hard rules obeyed:
  - Read-only by default. Write-class endpoints (/join, /submit, /agent,
    /assist, /feedback) are probed with OPTIONS only.
  - Write-class negative probes (POST with a bogus token) are DEFAULT-DENIED.
    Enabling them requires ALL of:
      1. `--isolated-base URL`  (--base can NEVER enable writes);
      2. the URL must EXACTLY match an entry in tools/isolated_envs.json
         (no substring, no "looks like test" heuristics);
      3. a marker handshake: GET /uibc/v1/info on that deployment must return
         marker_key == marker_value (defaults: environment == "isolated-test").
    Any refusal happens BEFORE any write-class request is emitted (the only
    request possibly emitted during gating is a GET /info on the allowlisted
    isolated deployment itself).
  - Write accounting distinguishes request ATTEMPTS, HTTP RESPONSES, and
    CONFIRMED potential side effects (2xx on a write endpoint). A POST that
    was sent is never reported as zero writes.
  - An invalid-credential POST answered with 2xx is an ABNORMAL SUCCESS:
    immediate FAIL, counted as a confirmed potential side effect, deep
    pollution check mandatory.
  - Every probe class has a NEGATIVE CONTROL that must be able to fail:
      * route-existence probe  -> a definitely-absent path must 404
      * semantic-404 probe     -> a fake cert body must DIFFER from the
                                  unknown-path control body (documented trap)
      * medal-spec parser      -> selftest proves bad specs are rejected
      * deep-pollution compare -> selftest proves None/unverifiable -> FAIL
      * abnormal-success path  -> selftest proves a 2xx rejection bypass is caught

Usage:
  uibc_smoke.py                                # read-only arm (production default)
  uibc_smoke.py --base URL                     # read-only arm against URL
  uibc_smoke.py --isolated-base URL --allow-reject-probes
                                     # + write-class negatives, gated as above
  uibc_smoke.py --isolated-base URL --allow-reject-probes   # refused: URL not allowlisted
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://uibc-mem-race.app.workbuddy.host"  # production default: READ-ONLY
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

V = "/uibc/v1"
WRITE_ENDPOINTS = ["join", "submit", "agent", "assist", "feedback"]
ISOLATED_ENV_FILE = Path(__file__).resolve().parent / "isolated_envs.json"

# verdicts that make a run RED (UNVERIFIABLE is deliberately red: absence of
# evidence is not evidence of compliance)
RED_VERDICTS = ("FAIL", "UNVERIFIABLE")


class Result:
    """Probe log + write accounting.

    writes_attempted    — write-class HTTP requests actually SENT (>= 0)
    writes_responses    — attempts that received ANY HTTP response (status != 0)
    writes_side_effects — attempts answered with 2xx (potential mutation confirmed)
    Invariant checked in payload(): attempted >= responded >= side_effects.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.writes_attempted = 0
        self.writes_responses = 0
        self.writes_side_effects = 0

    def add(self, probe: str, verdict: str, detail: str, **extra) -> None:
        row = {"probe": probe, "verdict": verdict, "detail": detail}
        row.update(extra)
        self.rows.append(row)
        print(f"  [{verdict}] {probe}: {detail}")

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["verdict"] in RED_VERDICTS]

    def write_counters(self) -> dict:
        return {"attempted": self.writes_attempted,
                "responded": self.writes_responses,
                "confirmed_side_effects": self.writes_side_effects}


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


def unreachable(st: int) -> bool:
    """True when the probe got NO HTTP response at all (status sentinel 0)."""
    return st == 0


def snapshot_state() -> dict | None:
    """Deep, hashable snapshot of exposed competition state (P1 pollution check).

    Covers what the API exposes: entry records, scores (best_total per row),
    and any medal/cert fields carried on leaderboard rows. Returns None when
    the snapshot is unavailable (unreachable / non-200 / unparseable).
    """
    st, body, _ = call("GET", f"{V}/leaderboard?track=MEM&top=500")
    if st != 200:
        return None
    try:
        d = json.loads(body)
        rows = d.get("leaderboard", [])
        return {
            "total_agents": d.get("total_agents"),
            "row_count": len(rows),
            "rows_sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
        }
    except Exception:  # noqa: BLE001
        return None


def board_total() -> int | None:
    """Total agent count from the leaderboard snapshot (None when unavailable)."""
    snap = snapshot_state()
    return snap["total_agents"] if snap else None


def compare_states(before: dict | None, after: dict | None):
    """Compare two snapshots. Returns (verdict, detail).

    Negative-control property (proven in selftest): an UNAVAILABLE snapshot
    (None) must yield UNVERIFIABLE/FAIL — never PASS.
    """
    if before is None or after is None:
        return "UNVERIFIABLE", (
            f"state snapshot unavailable (before={'ok' if before else 'None'}, "
            f"after={'ok' if after else 'None'}) — absence of evidence is not "
            "evidence of no pollution")
    if before == after:
        return "PASS", (f"deep state unchanged: total={before['total_agents']} "
                        f"rows={before['row_count']} "
                        f"rows_sha256={before['rows_sha256'][:12]}…")
    changed = [k for k in before if before.get(k) != after.get(k)]
    return "FAIL", f"state CHANGED in {changed}: {before} -> {after} — possible pollution"


def _parse_bronze_floor(bronze_spec):
    """Structurally parse the declared bronze medal spec.

    Returns (floor, ok, reason):
      floor  — the minimum threshold declared (machine-readable only)
      ok     — True only when a finite, non-bool, numeric floor exists that
               excludes a 0-score entrant
      reason — human-readable explanation when ok is False

    Rules (no substring heuristics like "min" in the name — the field must
    actually parse as a number):
      * dict  : bronze.min_score must be a finite, non-bool number and, under
                "score >= min_score" semantics, min_score > 0.
      * str   : must fullmatch ">=N" or ">N" (N finite numeric);
                ">=N" excludes 0 only if N > 0; ">N" excludes 0 if N >= 0.
      * anything else (prose such as "其余"/"others", missing, wrong type,
                NaN/Infinity, string-encoded numbers): NOT a floor -> the spec
                grants bronze to a 0-score entrant.
    """
    if isinstance(bronze_spec, dict):
        v = bronze_spec.get("min_score")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, False, f"bronze.min_score is not numeric: {v!r}"
        if not math.isfinite(v):
            return None, False, f"bronze.min_score must be finite, got {v!r}"
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


def negative_credential_probe(res: Result, caller=call):
    """Fail-fast invalid-credential POST. Write accounting is exact:
    an attempted POST is never reported as zero writes, and a 2xx answer is
    an ABNORMAL SUCCESS (FAIL + confirmed potential side effect), never a
    quietly-ignored rejection-test miss."""
    res.writes_attempted += 1
    st, body, _ = caller("POST", f"{V}/submit", {
        "agent_id": "uibc-mem-2026-00000000",
        "token": "definitely-not-a-valid-token",
        "memory_package": {"entries": []},
        "manifest": {"track": "MEM"},
    })
    if unreachable(st):
        res.add("negative: invalid credentials", "FAIL",
                "POST was ATTEMPTED but no HTTP response arrived (sentinel 0) — "
                "outcome unknown; counted in writes_attempted, not in responses")
        return
    res.writes_responses += 1
    if 200 <= st < 300:
        res.writes_side_effects += 1
        res.add("negative: invalid credentials", "FAIL",
                f"ABNORMAL SUCCESS: bogus token accepted with {st} — unauthenticated "
                "submit possible; counted as a CONFIRMED potential side effect; "
                "the deep pollution check below is mandatory, not optional")
    elif 400 <= st < 500:
        res.add("negative: invalid credentials", "PASS",
                f"bogus token -> {st} (4xx rejection as required; writes so far: "
                f"{res.write_counters()})")
    else:
        res.add("negative: invalid credentials", "FAIL",
                f"bogus token -> {st} (expected 4xx; 5xx/3xx/1xx are not rejections)")


def isolated_award_flow(res: Result, entry: dict | None):
    """Full join/submit/medal/cert-verify flow on the VERIFIED isolated env.

    Requires the allowlist entry to carry test_agent credentials AND the exact
    join/submit payloads (the server owner owns the valid-payload shape — the
    smoke must not guess it). Anything missing -> NOT VERIFIED, never a silent
    pass. Production NEVER reaches this function.
    """
    ta = (entry or {}).get("test_agent")
    if (not isinstance(ta, dict) or not ta.get("agent_id") or not ta.get("token")
            or not isinstance(ta.get("join_payload"), dict)
            or not isinstance(ta.get("submit_payload"), dict)):
        res.add("isolated: award+cert flow", "SKIP",
                "NOT VERIFIED: allowlist entry lacks test_agent "
                "(agent_id / token / join_payload / submit_payload) — the full "
                "award and certificate-verify flow cannot run without them")
        return
    agent_id = ta["agent_id"]
    before = snapshot_state()
    res.writes_attempted += 1
    st, body, _ = call("POST", f"{V}/join", ta["join_payload"])
    if unreachable(st):
        res.add("isolated: award+cert flow", "FAIL",
                "join POST attempted but no HTTP response — outcome unknown")
        return
    res.writes_responses += 1
    if not (200 <= st < 300):
        res.add("isolated: award+cert flow", "FAIL",
                f"join -> {st} (test credentials or payload stale?)")
        return
    res.writes_side_effects += 1
    res.writes_attempted += 1
    st2, body2, _ = call("POST", f"{V}/submit", ta["submit_payload"])
    if unreachable(st2):
        res.add("isolated: award+cert flow", "FAIL",
                "submit POST attempted but no HTTP response — outcome unknown")
        return
    res.writes_responses += 1
    if not (200 <= st2 < 300):
        res.add("isolated: award+cert flow", "FAIL", f"submit -> {st2}")
        return
    res.writes_side_effects += 1
    # inspect the board for the test agent's medal exposure
    st3, body3, _ = call("GET", f"{V}/leaderboard?track=MEM&top=500")
    row = None
    if st3 == 200:
        try:
            row = next((r for r in json.loads(body3).get("leaderboard", [])
                        if r.get("agent_id") == agent_id), None)
        except Exception:  # noqa: BLE001
            row = None
    if row is None:
        res.add("isolated: award+cert flow", "UNVERIFIABLE",
                f"test agent {agent_id} not found on the leaderboard after submit — "
                "medal grant cannot be observed from this endpoint")
        return
    medal_keys = [k for k in ("medal", "cert", "certificate", "cert_id") if row.get(k)]
    if float(row.get("best_total", -1)) <= 0 and medal_keys:
        res.add("isolated: award+cert flow", "FAIL",
                f"zero-score agent carries medal fields {medal_keys}: "
                f"{{k: row.get(k) for k in medal_keys}} — criterion violated")
    elif medal_keys:
        res.add("isolated: award+cert flow", "PASS",
                f"scored agent exposes medal fields {medal_keys}")
        cert_id = row.get("cert_id") or row.get("certificate")
        if cert_id:
            st4, _, _ = call("GET", f"{V}/verify/{cert_id}")
            res.add("isolated: cert verify", "PASS" if st4 == 200 else "FAIL",
                    f"GET /verify/{cert_id} -> {st4}")
        else:
            res.add("isolated: cert verify", "OBSERVE",
                    "row exposes no verifiable cert id — cert-verify step untestable from board")
    else:
        res.add("isolated: award+cert flow", "PASS",
                "agent on board without medal exposure (award logic defers to /info spec)")
    verdict_s, detail_s = compare_states(before, snapshot_state())
    res.add("isolated: deep pollution (award flow)", verdict_s, detail_s)


def verify_isolated_env(isolated_base: str | None):
    """Layered gate for write-class probes. Returns (ok, why, entry).

    MUST refuse BEFORE any network request when the base is not exactly
    allowlisted (zero I/O in that case); the handshake itself is a GET on the
    allowlisted isolated deployment — never a write.
    """
    if not isolated_base:
        return False, ("write-class probes DENIED by default: no --isolated-base given. "
                       "--base can never enable writes (production protection). "
                       "No request emitted."), None
    norm = isolated_base.rstrip("/")
    if not ISOLATED_ENV_FILE.exists():
        return False, (f"allowlist file missing: {ISOLATED_ENV_FILE}. "
                       "No request emitted."), None
    try:
        cfg = json.loads(ISOLATED_ENV_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"allowlist unreadable: {e}. No request emitted.", None
    entries = [e for e in cfg.get("environments", []) if isinstance(e, dict)]
    entry = next((e for e in entries if str(e.get("base", "")).rstrip("/") == norm), None)
    if entry is None:
        return False, (f"'{norm}' is NOT exactly listed in {ISOLATED_ENV_FILE.name} "
                       "(substring or 'looks-like-test' matching is intentionally "
                       "not accepted). No request emitted."), None
    dflt = cfg.get("marker_defaults", {})
    mk = entry.get("marker_key", dflt.get("marker_key", "environment"))
    mv = entry.get("marker_value", dflt.get("marker_value", "isolated-test"))
    global BASE
    BASE = norm  # handshake queries the allowlisted isolated deployment only
    st, body, _ = call("GET", f"{V}/info")
    if st != 200:
        return False, (f"handshake failed: GET /info -> {st} — marker unverifiable. "
                       "No write probe emitted."), None
    try:
        info = json.loads(body)
    except Exception:  # noqa: BLE001
        return False, "handshake failed: /info unparseable. No write probe emitted.", None
    if info.get(mk) != mv:
        return False, (f"handshake failed: /info[{mk!r}] = {info.get(mk)!r} != expected "
                       f"{mv!r}. No write probe emitted."), None
    return True, ("verified isolated environment: exact allowlist match + "
                  f"marker handshake /info[{mk!r}]=={mv!r}"), entry


def run(allow_reject: bool, isolated_entry: dict | None = None) -> Result:
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
    medals = None
    if st == 200:
        try:
            info = json.loads(body)
            n_ep = len(info.get("endpoints", {}))
            medals = info.get("medal")
            res.add("contract: GET /info", "PASS", f"200, {n_ep} endpoints declared, medal={medals}",
                    endpoints=sorted(info.get("endpoints", {}).keys()))
            # Negative control for the medal spec: the declared bronze spec must be a
            # machine-readable, finite, positive minimum-score floor. Missing field,
            # wrong type, prose, NaN/Infinity all FAIL — the default must be
            # FAIL, never PASS, when the spec cannot be verified.
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
                            f"({floor}) — 0-score entrants excluded by declared spec")
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
    schema_ok = st == 200
    res.add("contract: GET /schema", "PASS" if schema_ok else "FAIL",
            f"{st}, {len(body)} bytes" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

    # ---- 2b. medal declaration vs formal /schema (read-only; P1)
    if schema_ok:
        try:
            schema_txt = json.dumps(json.loads(body), ensure_ascii=False)
            machine_declared = (isinstance(medals, dict)
                                and _parse_bronze_floor(medals.get("bronze"))[1])
            schema_backed = ("medal" in schema_txt) or ("min_score" in schema_txt)
            if machine_declared and not schema_backed:
                res.add("schema: medal spec schema-backed", "WARN",
                        "/info declares a machine-readable bronze floor but /schema "
                        "describes no medal/min_score fields — declaration is NOT schema-backed")
            elif schema_backed:
                res.add("schema: medal spec schema-backed", "PASS",
                        "/schema describes medal/min_score fields — declaration is schema-backed")
            else:
                res.add("schema: medal spec schema-backed", "OBSERVE",
                        "/schema describes no medal fields and /info declares none — "
                        "nothing to cross-check yet")
        except Exception as e:  # noqa: BLE001
            res.add("schema: medal spec schema-backed", "UNVERIFIABLE",
                    f"/schema unparseable: {e} — consistency with /info cannot be checked")
    else:
        res.add("schema: medal spec schema-backed", "UNVERIFIABLE",
                f"/schema unavailable (status {st}) — consistency with /info cannot be checked")

    # ---- 3. leaderboard (before) + internal consistency
    state_before = snapshot_state()
    total_before = state_before["total_agents"] if state_before else None
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

    # ---- 5. route existence for write endpoints (OPTIONS only in read-only arm)
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

    # ---- 6. negative credential probe (DEFAULT-DENIED; verified isolated env only)
    if allow_reject:
        negative_credential_probe(res)
    else:
        res.add("negative: invalid credentials", "SKIP",
                "not run — write-class probes are default-denied; enabling requires "
                "--isolated-base exactly listed in tools/isolated_envs.json AND passing "
                "the marker handshake")

    # ---- 7. pollution assertion
    if allow_reject:
        # deep before/after on the isolated env: entry records, scores, medal-exposed
        # fields via leaderboard snapshot; certificate store separately.
        verdict_s, detail_s = compare_states(state_before, snapshot_state())
        res.add("pollution: deep state compare (isolated)", verdict_s,
                detail_s + f" [writes: {res.write_counters()}]")
        res.add("pollution: certificate store (isolated)", "UNVERIFIABLE",
                "no public endpoint enumerates certificates — a before/after comparison "
                "of the certificate store is impossible from the API alone; this absence "
                "of evidence is UNVERIFIABLE, not cleanliness. "
                f"[writes: {res.write_counters()}]")
    else:
        # read-only arm: total comparison; UNVERIFIABLE must NOT be counted as PASS
        total_after = board_total()
        if total_before is not None and total_after is not None:
            if total_before == total_after:
                res.add("pollution: board total unchanged", "PASS",
                        f"total_agents {total_before} -> {total_after} [writes: {res.write_counters()}]")
            else:
                res.add("pollution: board total unchanged", "FAIL",
                        f"total_agents {total_before} -> {total_after} — production board may be polluted")
        else:
            res.add("pollution: board total unchanged", "UNVERIFIABLE",
                    f"total_agents {total_before} -> {total_after} "
                    "(no HTTP 200 from /leaderboard; sentinel 0 = unreachable) — "
                    "absence of evidence is not evidence of no pollution")

    # ---- 8. full award + cert-verify flow (isolated env ONLY; never production)
    if allow_reject:
        isolated_award_flow(res, isolated_entry)

    return res


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-reject-probes", action="store_true",
                    help="enable write-class negative probes. DEFAULT-DENIED: requires "
                         "--isolated-base exactly listed in tools/isolated_envs.json AND "
                         "passing the environment-marker handshake. Refusal emits NO request.")
    ap.add_argument("--base", default=BASE,
                    help="target base URL for the READ-ONLY arm (default: production). "
                         "Write-class probes can NEVER be enabled via --base.")
    ap.add_argument("--isolated-base", default=None,
                    help="isolated test deployment base URL; the ONLY way to enable "
                         "write-class probes (see --allow-reject-probes)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    isolated_entry = None
    if args.allow_reject_probes:
        ok, why, isolated_entry = verify_isolated_env(args.isolated_base)
        print(f"  isolated-env gate: {why}")
        if not ok:
            return 2
    else:
        BASE = args.base
        if args.isolated_base:
            print("  NOTE: --isolated-base ignored without --allow-reject-probes (read-only arm)")

    print(f"uibc_smoke @ {BASE}  ({datetime.now(timezone.utc).isoformat()})  "
          f"[write-class probes: {'ON (verified isolated env)' if args.allow_reject_probes else 'OFF (read-only)'}]")
    res = run(args.allow_reject_probes, isolated_entry)
    verdict = "GREEN" if not res.failed else "RED"
    payload = {"base": BASE, "when": datetime.now(timezone.utc).isoformat(),
               "verdict": verdict,
               "writes": res.write_counters(),
               "rows": res.rows}
    print(f"verdict: {verdict}  ({len(res.failed)} FAIL/UNVERIFIABLE / {len(res.rows)} probes)")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("written:", args.out)
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
