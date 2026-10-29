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
    Any refusal happens BEFORE any write-class request is emitted.

    SECURITY SCOPE (stated, not oversold): the static marker is an
    anti-misoperation guard only. It does NOT defend against an adversarial
    impersonator of the isolated environment. HMAC/nonce schemes are NOT
    implemented — key management needs a reviewed design first (owner decision
    pending). Isolated-env entry checklist (all REQUIRED before adding an
    entry): exact-base allowlisting; HTTPS or loopback-only HTTP; verified
    environment marker; dedicated test credentials; NO production secrets.

  - Write accounting distinguishes: attempts, responses, POSSIBLE side
    effects (2xx on a write endpoint — 2xx alone does NOT prove a DB/cert
    change), and INDEPENDENTLY CONFIRMED side effects (the write's effect is
    observable in a post-write state snapshot). Failed/abnormal responses keep
    their evidence; "unconfirmed" is never reported as "confirmed absent".
  - A 2xx on an invalid-credential POST is an ABNORMAL SUCCESS: immediate
    FAIL + possible-side-effect count + mandatory deep pollution comparison.
  - EVERY write path (success, non-2xx, timeout/unreachable, missing board
    row) ends in the deep pollution comparison via try/finally — early
    returns cannot bypass it.
  - Every probe class has a NEGATIVE CONTROL that must be able to fail
    (see tools/uibc_smoke_selftest.py).

Usage:
  uibc_smoke.py                                # read-only arm (production default)
  uibc_smoke.py --base URL                     # read-only arm against URL
  uibc_smoke.py --isolated-base URL --allow-reject-probes
                                    # + write-class negatives, gated as above
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

SNAPSHOT_SCOPE = ("GET /leaderboard?track=MEM (top<=500): entry rows incl. best_total, "
                  "submissions, last_submit_at and any medal/cert fields the board "
                  "exposes. BLIND SPOTS: rows beyond top=500, other tracks, the "
                  "certificate store, and any state the API does not expose.")


class Result:
    """Probe log + write accounting.

    attempted    — write-class HTTP requests actually SENT
    responded    — attempts that received ANY HTTP response (status != 0)
    possible_side_effects — attempts answered with 2xx (2xx alone does NOT
                   prove a DB/certificate mutation — hence "possible")
    confirmed_side_effects — writes whose effect is INDEPENDENTLY observable
                   in a post-write state snapshot (e.g. the test agent appears
                   on the board after join/submit)
    Invariant: attempted >= responded >= possible >= confirmed.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.writes_attempted = 0
        self.writes_responses = 0
        self.writes_possible_side_effects = 0
        self.writes_confirmed_side_effects = 0

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
                "possible_side_effects": self.writes_possible_side_effects,
                "confirmed_side_effects": self.writes_confirmed_side_effects}


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


def snapshot_state(caller=call) -> dict | None:
    """Snapshot of EXPOSED competition state (single track, top<=500).

    Scope and blind spots are documented in SNAPSHOT_SCOPE and must be
    reported alongside any pollution conclusion — a leaderboard snapshot is
    NOT a proof that the whole database is unchanged.
    Returns None when unavailable (unreachable / non-200 / unparseable).
    """
    st, body, _ = caller("GET", f"{V}/leaderboard?track=MEM&top=500")
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


def board_total(caller=call) -> int | None:
    snap = snapshot_state(caller)
    return snap["total_agents"] if snap else None


def compare_states(before: dict | None, after: dict | None):
    """Compare two snapshots. Returns (verdict, detail).

    Negative-control property (proven in selftest): an UNAVAILABLE snapshot
    (None) must yield UNVERIFIABLE — never PASS. A PASS means "no change in
    the EXPOSED snapshot scope", never "the database is unchanged".
    """
    if before is None or after is None:
        return "UNVERIFIABLE", (
            f"state snapshot unavailable (before={'ok' if before else 'None'}, "
            f"after={'ok' if after else 'None'}) — absence of evidence is not "
            "evidence of no pollution")
    if before == after:
        return "PASS", (f"no change in EXPOSED snapshot (scope-limited, NOT a "
                        f"DB-wide proof): total={before['total_agents']} "
                        f"rows={before['row_count']} "
                        f"rows_sha256={before['rows_sha256'][:12]}…")
    changed = [k for k in before if before.get(k) != after.get(k)]
    return "FAIL", f"EXPOSED state CHANGED in {changed}: {before} -> {after} — possible pollution"


def _parse_bronze_floor(bronze_spec):
    """Structurally parse the declared bronze medal spec.

    Returns (floor, ok, reason). ok=True only for a finite, non-bool, numeric
    floor that excludes a 0-score entrant:
      * dict  : bronze.min_score — finite non-bool number; semantics
                "score >= min_score" so min_score > 0.
      * str   : fullmatch ">=N" / ">N" (N finite); ">=N" excludes 0 only if
                N > 0; ">N" excludes 0 if N >= 0.
      * everything else (prose, missing, wrong type, NaN/Infinity,
        string-encoded numbers): NOT a floor.
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
            ok = (v > 0) if op == ">=" else (v >= 0)
            if ok:
                return v, True, ""
            return None, False, f"bronze threshold '{bronze_spec}' does not exclude a 0-score entrant"
        return None, False, (f"bronze spec is non-numeric prose (no machine-readable floor): "
                             f"{bronze_spec!r}")
    return None, False, f"bronze spec has unsupported type: {bronze_spec!r}"


def _find_schema_nodes(node, keys, path="$"):
    """Recursively collect (path, key, value) for dict keys in `keys`.
    Structural lookup for schema checks — no substring matching."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if k in keys:
                out.append((p, k, v))
            out.extend(_find_schema_nodes(v, keys, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_find_schema_nodes(v, keys, f"{path}[{i}]"))
    return out


def _min_score_semantics(v) -> tuple[bool, str]:
    """Semantic proof standard for a min_score node's VALUE (round-6, P1 fix).

    "The field is numeric" is NOT "a threshold exists". A threshold is PROVEN
    only when BOTH hold:
      1. a threshold VALUE exists — either the node is itself a literal finite
         number, or the constraint node carries a numeric minimum /
         exclusiveMinimum bound;
      2. a COMPARISON SEMANTICS exists that excludes zero-score entrants —
         the bound must be finite and > 0 (minimum=0 or a negative bound
         cannot exclude a 0-score medal, and an invalid bound value — string,
         bool, null, NaN — proves nothing).
    Returns (proven, why); when proven, `why` embeds the bound's value and
    comparison operator for cross-source matching.
    Anything less -> not provable -> UNVERIFIABLE upstream.
    """
    if isinstance(v, bool) or v is None:
        return False, f"min_score value {v!r} is not numeric (bool/None rejected)"
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            return False, "min_score is a non-finite number (NaN/Inf)"
        if v <= 0:
            return False, f"literal threshold {v} cannot exclude zero-score entrants (must be >0)"
        return True, f"literal threshold {v} (>0: a 0-score entrant is below the floor)"
    if isinstance(v, dict):
        bounds = []
        for key in ("minimum", "exclusiveMinimum"):
            if key in v:
                b = v[key]
                if isinstance(b, bool) or not isinstance(b, (int, float)) \
                        or not math.isfinite(b):
                    return False, (f"{key}={b!r} is not a finite number — invalid "
                                   "constraint value proves no threshold")
                if b <= 0:
                    return False, (f"{key}={b} cannot exclude zero-score entrants "
                                   "(zero/negative threshold)")
                op = ">=" if key == "minimum" else ">"
                bounds.append(f"{key}={b} ({op}0 semantics: a 0-score entrant is excluded)")
        if bounds:
            return True, "; ".join(bounds)
        return False, (f"min_score node declares type={v.get('type')!r} but NO numeric "
                       "bound (minimum/exclusiveMinimum) — a numeric TYPE alone does not "
                       "prove a threshold VALUE or its comparison semantics")
    return False, f"min_score value has non-numeric type {type(v).__name__}"


def _min_score_bound(v) -> list[tuple[str, float]]:
    """(comparison_op, value) pairs declared by a SEMANTICALLY PROVEN min_score
    node (call only after _min_score_semantics returned True). A literal node
    means the field itself is the floor: score >= value. minimum -> >=,
    exclusiveMinimum -> >."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [(">=", float(v))]
    if isinstance(v, dict):
        out = []
        for key in ("minimum", "exclusiveMinimum"):
            if key in v:
                out.append((">=" if key == "minimum" else ">", float(v[key])))
        return out
    return []


def _bronze_floor_semantics(bronze_spec) -> tuple[float | None, str | None]:
    """(value, comparison_op) of the /info-declared machine-readable floor.
    Reuses _parse_bronze_floor as the single source of parseability truth;
    dict bronze.min_score carries "score >= min_score" semantics (see its
    docstring), a string spec carries its own explicit >=/ > operator."""
    _, ok, _ = _parse_bronze_floor(bronze_spec)
    if not ok:
        return None, None
    if isinstance(bronze_spec, dict):
        return float(bronze_spec["min_score"]), ">="
    m = re.fullmatch(r"(>=?)\s*(\d+(?:\.\d+)?)", bronze_spec.strip())
    if not m:
        return None, None
    return float(m.group(2)), m.group(1)


def _path_related_to_medal(path: str) -> bool:
    """A min_score node only counts when its structural path runs through a
    medal-rule subtree (e.g. $.properties.medal.min_score). A min_score in an
    unrelated position (pagination, rate limits, ...) proves nothing about the
    award rule."""
    medal_keys = {"medal", "medals", "award", "awards", "bronze", "silver", "gold"}
    segs = {s.lower() for s in path.replace("[", ".").replace("]", "").split(".")}
    return bool(segs & medal_keys)


def schema_medal_check(schema_body: str | None, medals) -> tuple[str, str]:
    """Cross-check /info's medal declaration against the formal /schema.

    Structural: locates actual medal/min_score nodes (path+type), never
    substring matching. A PASS requires ALL of:
      (a) the min_score node is on a path related to the medal rule;
      (b) the node semantically PROVES a threshold (value + comparison
          semantics that exclude a 0-score entrant);
      (c) the proven bound MATCHES the /info-declared floor in BOTH value and
          comparison operator — "a valid threshold exists in the schema" and
          "the two sources declare the SAME threshold" are different claims;
          only (c) earns the word "consistent".
    Mismatch between the two sources is positive evidence of a defect -> FAIL.
    Anything unprovable -> UNVERIFIABLE — thresholds are never guessed.
    """
    if schema_body is None:
        return "UNVERIFIABLE", "/schema unavailable — consistency with /info cannot be checked"
    try:
        schema = json.loads(schema_body)
    except Exception as e:  # noqa: BLE001
        return "UNVERIFIABLE", f"/schema unparseable: {e} — consistency cannot be checked"
    nodes = _find_schema_nodes(schema, ("medal", "min_score"))
    machine_declared = (isinstance(medals, dict)
                        and _parse_bronze_floor(medals.get("bronze"))[1])
    if not nodes:
        if machine_declared:
            return "WARN", ("/info declares a machine-readable bronze floor but /schema "
                            "contains NO medal/min_score nodes — declaration is NOT schema-backed")
        return "OBSERVE", "/schema describes no medal fields and /info declares none — nothing to cross-check"
    # a node only counts as threshold-bearing when it IS min_score or a dict
    # that CONTAINS a min_score key — a bare "medal" object (e.g. a JSON-schema
    # node describing a string) does not define the rule
    threshold_nodes = [(p, v if k == "min_score" else v.get("min_score"))
                       for p, k, v in nodes
                       if k == "min_score"
                       or (isinstance(v, dict) and "min_score" in v)]
    if machine_declared:
        info_val, info_op = _bronze_floor_semantics(medals.get("bronze"))
        proven = []
        for p, v in threshold_nodes:
            if not _path_related_to_medal(p):
                continue
            ok, why = _min_score_semantics(v)
            if ok:
                proven.append((p, _min_score_bound(v), why))
        matching = [(p, b, why) for p, b, why in proven
                    if any(op == info_op and val == info_val for op, val in b)]
        if matching:
            return "PASS", (f"/schema threshold at {[p for p, _, _ in matching][:3]} MATCHES the "
                            f"/info declared floor ({info_op} {info_val}): value AND comparison "
                            f"semantics agree ({matching[0][2]})")
        if proven:
            detail = "; ".join(f"{p} declares {b}" for p, b, _ in proven[:3])
            return "FAIL", (f"CROSS-SOURCE MISMATCH: /info declares bronze floor {info_op} "
                            f"{info_val} but the schema's medal-related threshold(s) disagree: "
                            f"{detail} — 'a valid threshold exists' is NOT 'the two sources "
                            "declare the same threshold'")
        if threshold_nodes:
            detail = "; ".join(
                f"{p} ({'unrelated path' if not _path_related_to_medal(p) else _min_score_semantics(v)[1]})"
                for p, v in threshold_nodes[:3])
            return "UNVERIFIABLE", (f"/schema has min_score nodes but none PROVABLE as the medal "
                                    f"threshold: {detail} — the rule's meaning cannot be "
                                    "confirmed; NOT reported as schema-backed")
        return "UNVERIFIABLE", (f"/schema mentions medal nodes {[p for p, _, _ in nodes][:3]} "
                                "but none carries a min_score threshold structure — the rule's "
                                "meaning cannot be confirmed; NOT reported as schema-backed")
    return "OBSERVE", (f"/schema describes medal fields at {[p for p, _, _ in nodes][:3]} "
                       "but /info declares no machine-readable floor")


def negative_credential_probe(res: Result, caller=call):
    """Fail-fast invalid-credential POST. Exact write accounting; a 2xx is an
    ABNORMAL SUCCESS (FAIL + possible side effect), never quietly ignored."""
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
                "outcome unknown; counted as attempted, not responded")
        return
    res.writes_responses += 1
    if 200 <= st < 300:
        res.writes_possible_side_effects += 1
        res.add("negative: invalid credentials", "FAIL",
                f"ABNORMAL SUCCESS: bogus token accepted with {st} — unauthenticated "
                "submit possible; counted as a POSSIBLE side effect (2xx alone does "
                "not prove a DB change); the deep pollution comparison that follows "
                "is mandatory")
    elif 400 <= st < 500:
        res.add("negative: invalid credentials", "PASS",
                f"bogus token -> {st} (4xx rejection as required; writes so far: "
                f"{res.write_counters()})")
    else:
        res.add("negative: invalid credentials", "FAIL",
                f"bogus token -> {st} (expected 4xx; 5xx/3xx/1xx are not rejections)")


_CERT_SUBJECT_KEYS = ("owner", "agent_id", "subject", "agent")
_CERT_ID_KEYS = ("cert_id", "certificate_id", "id")


def _cert_verify_check(res: Result, cert_id: str, agent_id: str, caller=call):
    """Cert verification via STRUCTURED field matching — HTTP 200 alone is not
    a pass, and neither is a substring hit.

    Three-way identity check on the response body:
      1. response cert id == the cert id requested (rounded trip);
      2. a subject field (owner/agent_id/subject/agent) exists AND equals the
         test agent id;
      3. any mismatch is a FAIL (real evidence of a defect), any MISSING field
         is UNVERIFIABLE (absence of evidence is not compliance).
    Unrelated prose containing the agent id string does NOT confirm binding.
    """
    st, body, _ = caller("GET", f"{V}/verify/{cert_id}")
    if unreachable(st):
        res.add("isolated: cert verify", "UNVERIFIABLE",
                f"GET /verify/{cert_id} attempted, no response — content unverifiable")
        return
    if st != 200:
        res.add("isolated: cert verify", "FAIL", f"GET /verify/{cert_id} -> {st}")
        return
    try:
        cert = json.loads(body)
    except Exception:  # noqa: BLE001
        res.add("isolated: cert verify", "UNVERIFIABLE",
                f"GET /verify/{cert_id} -> 200 but body unparseable — content "
                "consistency cannot be checked; NOT reported as verified")
        return
    if not isinstance(cert, dict):
        res.add("isolated: cert verify", "UNVERIFIABLE",
                f"GET /verify/{cert_id} -> 200 but body is {type(cert).__name__}, "
                "not an object — no structured fields to verify against")
        return
    resp_id = next((cert[k] for k in _CERT_ID_KEYS if cert.get(k) is not None), None)
    subject_key = next((k for k in _CERT_SUBJECT_KEYS
                        if cert.get(k) not in (None, "", False)), None)
    subject = cert.get(subject_key) if subject_key else None
    # --- identity 1: response cert id must round-trip
    if resp_id is None:
        res.add("isolated: cert verify", "UNVERIFIABLE",
                f"cert body carries no cert id field {_CERT_ID_KEYS} — cannot "
                "confirm the response cert is the requested one; NOT verified")
        return
    if str(resp_id) != str(cert_id):
        res.add("isolated: cert verify", "FAIL",
                f"cert id MISMATCH: requested {cert_id!r} but response declares "
                f"{resp_id!r} — wrong certificate served")
        return
    # --- identity 2: subject binding (structured, not substring)
    if subject_key is None:
        res.add("isolated: cert verify", "UNVERIFIABLE",
                f"cert {cert_id} round-trips (id matches) but carries no subject "
                f"field {_CERT_SUBJECT_KEYS} (body keys: {sorted(cert)[:8]}) — "
                "agent binding unproven; NOT verified")
        return
    if not isinstance(subject, str) or subject != agent_id:
        res.add("isolated: cert verify", "FAIL",
                f"cert {cert_id} SUBJECT MISMATCH: {subject_key}={subject!r} != "
                f"test agent {agent_id!r} — certificate bound to another party")
        return
    res.add("isolated: cert verify", "PASS",
            f"cert {cert_id}: 200, id round-trips, and {subject_key} is exactly "
            f"the test agent {agent_id} (structured match, not substring)")


def isolated_award_flow(res: Result, entry: dict | None,
                        bronze_spec, spec_ok: bool, caller=call):
    """Full join/submit/medal/cert-verify flow on the VERIFIED isolated env.

    - EVERY exit path (success, non-2xx, timeout, missing board row) runs the
      deep pollution comparison (try/finally) — early returns cannot skip it.
    - A zero-score agent WITHOUT medal fields is PASS only when /info declares
      a machine-readable exclusion floor; otherwise UNVERIFIABLE (a missing
      field on the board is not proof of correct award logic).
    - join/submit 2xx counts as POSSIBLE side effects; the test agent actually
      appearing in the post-write snapshot counts as INDEPENDENTLY CONFIRMED.
    - Cert verification checks content, not just HTTP 200.
    Production NEVER reaches this function.
    """
    ta = (entry or {}).get("test_agent") if isinstance(entry, dict) else None
    if (not isinstance(ta, dict) or not ta.get("agent_id") or not ta.get("token")
            or not isinstance(ta.get("join_payload"), dict)
            or not isinstance(ta.get("submit_payload"), dict)):
        res.add("isolated: award+cert flow", "SKIP",
                "NOT VERIFIED: allowlist entry lacks test_agent "
                "(agent_id / token / join_payload / submit_payload) — the full "
                "award and certificate-verify flow cannot run without them; "
                "no write was performed")
        return
    agent_id = ta["agent_id"]
    state_before = snapshot_state(caller)
    try:
        # ---- join
        res.writes_attempted += 1
        st, body, _ = caller("POST", f"{V}/join", ta["join_payload"])
        if unreachable(st):
            res.add("isolated: join", "FAIL",
                    "POST attempted, no HTTP response — outcome unknown")
            return
        res.writes_responses += 1
        if not (200 <= st < 300):
            res.add("isolated: join", "FAIL",
                    f"join -> {st} (test credentials or payload stale?); "
                    f"writes: {res.write_counters()}")
            return
        res.writes_possible_side_effects += 1
        # ---- submit
        res.writes_attempted += 1
        st2, body2, _ = caller("POST", f"{V}/submit", ta["submit_payload"])
        if unreachable(st2):
            res.add("isolated: submit", "FAIL",
                    "POST attempted, no HTTP response — outcome unknown")
            return
        res.writes_responses += 1
        if not (200 <= st2 < 300):
            res.add("isolated: submit", "FAIL",
                    f"submit -> {st2}; writes: {res.write_counters()}")
            return
        res.writes_possible_side_effects += 1
        # ---- board observation
        st3, body3, _ = caller("GET", f"{V}/leaderboard?track=MEM&top=500")
        row = None
        if st3 == 200:
            try:
                row = next((r for r in json.loads(body3).get("leaderboard", [])
                            if r.get("agent_id") == agent_id), None)
            except Exception:  # noqa: BLE001
                row = None
        if row is None:
            res.add("isolated: award observation", "UNVERIFIABLE",
                    f"test agent {agent_id} not found on the leaderboard after "
                    "submit — medal grant is unobservable from this endpoint; "
                    "NOT reported as pass or fail")
            return
        # the write's effect is independently observable in exposed state
        res.writes_confirmed_side_effects += 1
        medal_keys = [k for k in ("medal", "cert", "certificate", "cert_id")
                      if row.get(k) not in (None, "", False)]
        # strict best_total parsing: a malformed value must be RECORDED, never
        # crash the report — the try/finally pollution close-out still runs on
        # every early return below
        if "best_total" not in row:
            res.add("isolated: award observation", "UNVERIFIABLE",
                    f"board row for {agent_id} lacks best_total — award criterion "
                    "unobservable (missing field is not a zero score); NOT guessed")
            return
        raw_best = row["best_total"]
        try:
            if isinstance(raw_best, bool) or not isinstance(raw_best, (int, float)):
                raise TypeError(f"{raw_best!r} is {type(raw_best).__name__}, not a number")
            best = float(raw_best)
            if not math.isfinite(best):
                raise ValueError(f"{raw_best!r} is not finite (NaN/Inf)")
        except (TypeError, ValueError) as e:
            res.add("isolated: award observation", "FAIL",
                    f"MALFORMED best_total on board row: {e} — award criterion "
                    "cannot be evaluated; recorded, not silently skipped")
            return
        if best <= 0 and medal_keys:
            sample = {k: row.get(k) for k in medal_keys}
            res.add("isolated: award observation", "FAIL",
                    f"zero-score agent (best_total={best}) carries medal fields "
                    f"{medal_keys}, sample={sample} — criterion violated")
        elif best <= 0 and not medal_keys:
            if spec_ok:
                res.add("isolated: award observation", "PASS",
                        f"zero-score agent exposes no medal field, consistent with "
                        f"the declared machine-readable floor {bronze_spec!r}")
            else:
                res.add("isolated: award observation", "UNVERIFIABLE",
                        f"zero-score agent exposes no medal field, but /info's medal "
                        f"spec ({bronze_spec!r}) is not machine-parseable — cannot "
                        "distinguish correct exclusion from missing exposure; "
                        "NOT reported as PASS (regression-controlled in selftest)")
        elif medal_keys:
            if spec_ok:
                res.add("isolated: award observation", "PASS",
                        f"scored agent (best_total={best}) exposes medal fields "
                        f"{medal_keys}, consistent with the declared spec {bronze_spec!r}")
            else:
                res.add("isolated: award observation", "UNVERIFIABLE",
                        f"scored agent exposes medal fields {medal_keys} but the "
                        f"declared spec ({bronze_spec!r}) is not machine-parseable — "
                        "cannot confirm the medal matches the rule")
        else:
            res.add("isolated: award observation", "UNVERIFIABLE",
                    f"scored agent (best_total={best}) exposes no medal fields — "
                    "cannot confirm the award actually happened")
        # ---- cert verification (content-checked)
        cert_id = row.get("cert_id") or row.get("certificate")
        if cert_id:
            _cert_verify_check(res, str(cert_id), agent_id, caller=caller)
        else:
            res.add("isolated: cert verify", "OBSERVE",
                    "row exposes no verifiable cert id — cert-verify step "
                    "untestable from the board")
    finally:
        state_after = snapshot_state(caller)
        verdict_s, detail_s = compare_states(state_before, state_after)
        res.add("isolated: deep pollution (all write paths)", verdict_s,
                detail_s + f" | scope: {SNAPSHOT_SCOPE} | writes: {res.write_counters()}")


def verify_isolated_env(isolated_base: str | None):
    """Layered gate for write-class probes. Returns (ok, why, entry).

    MUST refuse BEFORE any network request when the base is not exactly
    allowlisted (zero I/O in that case); the handshake itself is a GET on the
    allowlisted isolated deployment — never a write. The static marker is an
    anti-misoperation guard, not an adversarial defense (see module docstring).
    """
    if not isolated_base:
        return False, ("write-class probes DENIED by default: no --isolated-base given. "
                       "--base can never enable writes (production protection). "
                       "No request emitted."), None
    norm = isolated_base.rstrip("/")
    # loopback plain-HTTP is the standard isolated deployment shape; anything
    # else must be https (checked here and documented in the allowlist entry)
    if not norm.startswith("https://"):
        host = norm.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False, (f"refused: non-HTTPS, non-loopback target '{norm}' — add an "
                           "explicit https base or use a loopback address. No request "
                           "emitted."), None
    if not ISOLATED_ENV_FILE.exists():
        return False, f"allowlist file missing: {ISOLATED_ENV_FILE}. No request emitted.", None
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
    st, schema_body, _ = call("GET", f"{V}/schema")
    schema_ok = st == 200
    res.add("contract: GET /schema", "PASS" if schema_ok else "FAIL",
            f"{st}, {len(schema_body)} bytes" + (" (unreachable: no HTTP response)" if unreachable(st) else ""))

    # ---- 2b. medal declaration vs formal /schema (structural; P1)
    v_sch, d_sch = schema_medal_check(schema_body if schema_ok else None, medals)
    res.add("schema: medal spec schema-backed", v_sch, d_sch)

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

    bronze_spec = medals.get("bronze") if isinstance(medals, dict) else None
    spec_ok = _parse_bronze_floor(bronze_spec)[1] if bronze_spec is not None else False

    # ---- 6..8 writes (isolated only) with GUARANTEED pollution close-out:
    # the try/finally ensures every exit path (non-2xx, timeout, missing row,
    # unexpected exception) still runs the before/after state comparison.
    if allow_reject:
        try:
            negative_credential_probe(res)
            isolated_award_flow(res, isolated_entry, bronze_spec, spec_ok)
        finally:
            verdict_s, detail_s = compare_states(state_before, snapshot_state())
            res.add("pollution: deep state compare (isolated, all writes)", verdict_s,
                    detail_s + f" | writes: {res.write_counters()}")
            res.add("pollution: certificate store (isolated)", "UNVERIFIABLE",
                    "no public endpoint enumerates certificates — before/after "
                    "comparison of the certificate store is impossible from the API "
                    "alone; this is a COVERAGE GAP, not proof of cleanliness. "
                    f"| writes: {res.write_counters()}")
            res.add("pollution: snapshot scope", "OBSERVE", SNAPSHOT_SCOPE)
    else:
        total_after = board_total()
        if total_before is not None and total_after is not None:
            if total_before == total_after:
                res.add("pollution: board total unchanged", "PASS",
                        f"total_agents {total_before} -> {total_after} [writes: "
                        f"{res.write_counters()}] (read-only arm; scope-limited evidence)")
            else:
                res.add("pollution: board total unchanged", "FAIL",
                        f"total_agents {total_before} -> {total_after} — production board may be polluted")
        else:
            res.add("pollution: board total unchanged", "UNVERIFIABLE",
                    f"total_agents {total_before} -> {total_after} "
                    "(no HTTP 200 from /leaderboard; sentinel 0 = unreachable) — "
                    "absence of evidence is not evidence of no pollution")

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
