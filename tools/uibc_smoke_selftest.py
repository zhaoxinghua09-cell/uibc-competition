#!/usr/bin/env python3
"""uibc_smoke_selftest — OFFLINE negative controls for uibc_smoke.

Proves each detection capability can actually FAIL (a test that cannot fail
proves nothing). No external network: server interaction is stubbed; the one
runtime drill binds to a local logging server on 127.0.0.1.

Sections:
  A. medal-spec parser: prose/missing/wrong-type/NaN/Inf/bool/str-number -> FAIL
  B. deep-pollution compare: None -> UNVERIFIABLE (never PASS); changed -> FAIL
  C. write accounting: a sent POST is never reported as zero writes
  D. abnormal success: 2xx on bogus token -> FAIL + possible side effect
  E. guard inputs (offline layers)
  F. runtime drill: zero-POST guarantee via local logging server
  G. isolated award flow (stubbed): every write path runs the pollution
     close-out (try/finally); zero-score-without-medal is NEVER a bare PASS
     (regression control for the false-PASS defect); cert verify checks
     content, not just HTTP 200; possible vs confirmed side effects
  H. schema medal check: structural node lookup, substring trap, UNVERIFIABLE
     when the rule's meaning cannot be confirmed

Exit 0 = all negative controls hold; non-zero = the smoke has gone decorative.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("uibc_smoke", HERE / "uibc_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'BAD'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def rows_by_probe(res, needle):
    return [r for r in res.rows if needle in r["probe"]]


class StubCaller:
    """Scripted call() replacement: pops (status, body) per invocation."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.methods = []

    def __call__(self, method, path, body=None, timeout=25):
        self.methods.append((method, path))
        st, b = self.responses.pop(0)
        return st, b, {}


def flow_stub(spec_board_row=None, join=(200, "{}"), submit=(200, "{}"),
              board=(200, '{"leaderboard": []}'), cert=None):
    """Build a StubCaller sequence for isolated_award_flow.
    Actual call order in isolated_award_flow:
    1. snapshot_state (before)          -> snap
    2. POST /join                       -> join
    3. POST /submit                     -> submit
    4. GET /leaderboard (row lookup)    -> board / spec_board_row
    5. GET /verify/{cert_id} (optional) -> cert
    6. snapshot_state (after, finally)  -> snap (last, ALWAYS)
    """
    seq = []
    snap = (200, json.dumps({"total_agents": 36, "leaderboard": [
        {"agent_id": "x", "best_total": 1.0}]}))
    seq.append(snap)          # 1: before-snapshot
    seq.append(join)          # 2: POST /join
    seq.append(submit)        # 3: POST /submit
    if spec_board_row is not None:
        seq.append((200, json.dumps({"leaderboard": [spec_board_row]})))
    else:
        seq.append(board)
    if cert is not None:
        seq.append(cert)
    seq.append(snap)          # 6: after-snapshot (finally, always last)
    return StubCaller(seq)


TA = {"agent_id": "uibc-mem-2026-test0", "token": "t",
      "join_payload": {"a": 1}, "submit_payload": {"b": 2}}
ENTRY = {"base": "http://127.0.0.1:1", "test_agent": TA}

# ---------------------------------------------------------------- A. parser
print("A. medal-spec parser (negative controls: bad specs must be rejected)")
cases = [
    ("其余", False), ("others", False), (None, False), ("", False),
    (">0", True), ("> 0", True), (">0.5", True), (">=1", True), (">= 1", True), (">=0.5", True),
    (">=0", False), (">=0.0", False),
    (">-1", False), (">=-1", False),
    (">=x", False), (">min", False), (">= 1 (bronze)", False),
    ({"min_score": 1}, True), ({"min_score": 10}, True), ({"min_score": 0.5}, True),
    ({"min_score": 0}, False), ({"min_score": -3}, False),
    ({"min_score": "1"}, False),
    ({"min_score": True}, False),
    ({"min_score": None}, False),
    ({"minimum": 1}, False),
    ({}, False),
    (["min"], False), (3, False),
]
for spec, want in cases:
    _, ok, why = smoke._parse_bronze_floor(spec)
    check(f"parse {spec!r} -> {'PASS' if want else 'FAIL'}", ok == want, why[:70])
for v in (float("nan"), float("inf"), float("-inf")):
    _, ok, why = smoke._parse_bronze_floor({"min_score": v})
    check(f"parse min_score={v!r} -> FAIL (non-finite)", ok is False, why[:70])

# ------------------------------------------------- B. deep-pollution compare
print("B. deep-pollution compare (negative control: None must never PASS)")
same = {"total_agents": 36, "row_count": 36, "rows_sha256": "a" * 64}
v1, d1 = smoke.compare_states(same, dict(same))
check("identical snapshots -> PASS (scope-limited wording)",
      v1 == "PASS" and "NOT a DB-wide proof" in d1, d1[:70])
v2, d2 = smoke.compare_states(None, same)
check("before=None -> UNVERIFIABLE", v2 == "UNVERIFIABLE", d2[:60])
v3, d3 = smoke.compare_states(same, None)
check("after=None -> UNVERIFIABLE", v3 == "UNVERIFIABLE", d3[:60])
v4, d4 = smoke.compare_states(None, None)
check("both=None (full outage) -> UNVERIFIABLE", v4 == "UNVERIFIABLE", d4[:60])
diff = dict(same, total_agents=37)
v5, d5 = smoke.compare_states(same, diff)
check("changed snapshot -> FAIL", v5 == "FAIL", d5[:60])

# --------------------------- C+D. write accounting & abnormal success (stubbed)
print("C. write accounting (a sent POST is never reported as zero writes)")
res = smoke.Result()
calls = []


def stub_unreachable(method, path, body=None, timeout=25):
    calls.append((method, path))
    return 0, "conn refused", {}


smoke.negative_credential_probe(res, caller=stub_unreachable)
check("unreachable POST: attempted==1, responded==0",
      res.writes_attempted == 1 and res.writes_responses == 0, str(res.write_counters()))
check("unreachable POST: verdict FAIL", bool(res.failed))
check("stub really sent a POST", calls and calls[0][0] == "POST")

print("D. abnormal success (2xx on bogus token = FAIL + possible side effect)")
res2 = smoke.Result()


def stub_2xx(method, path, body=None, timeout=25):
    return 200, '{"ok": true}', {}


smoke.negative_credential_probe(res2, caller=stub_2xx)
check("2xx bogus token -> FAIL", bool(res2.failed))
check("2xx bogus token -> possible_side_effects==1, confirmed==0 (2xx alone "
      "does not confirm a DB change)",
      res2.writes_possible_side_effects == 1 and res2.writes_confirmed_side_effects == 0,
      str(res2.write_counters()))

res3 = smoke.Result()


def stub_401(method, path, body=None, timeout=25):
    return 401, '{"error": "unauthorized"}', {}


smoke.negative_credential_probe(res3, caller=stub_401)
check("401 bogus token -> PASS (proper rejection)", not res3.failed)
check("401: attempted==1 responded==1 possible==0",
      res3.write_counters() == {"attempted": 1, "responded": 1,
                                "possible_side_effects": 0,
                                "confirmed_side_effects": 0},
      str(res3.write_counters()))

res4 = smoke.Result()


def stub_500(method, path, body=None, timeout=25):
    return 500, "boom", {}


smoke.negative_credential_probe(res4, caller=stub_500)
check("5xx bogus token -> FAIL (not a rejection)", bool(res4.failed))

# ------------------------------------------------------------ guard sanity
print("E. guard inputs (offline layers; handshake layer drill is runtime-only)")
ok1, why1, _ = smoke.verify_isolated_env(None)
check("no --isolated-base -> denied", not ok1, why1[:60])
ok2, why2, _ = smoke.verify_isolated_env("http://127.0.0.1:9/")
check("non-allowlisted URL -> denied with ZERO requests", not ok2, why2[:60])
ok2b, why2b, _ = smoke.verify_isolated_env("http://evil.example.com:8080")
check("non-HTTPS non-loopback -> refused (zero requests)", not ok2b, why2b[:60])

# ------------------------------------------- F. runtime drill: layered guard
print("F. runtime drill — local logging server proves the zero-POST guarantee")
import http.server
import tempfile
import threading


class MockHandler(http.server.BaseHTTPRequestHandler):
    requests: list = []
    info_body: dict = {}

    def _reply(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        MockHandler.requests.append(("GET", self.path))
        if self.path == "/uibc/v1/info":
            self._reply(200, json.dumps(MockHandler.info_body).encode())
        else:
            self._reply(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        MockHandler.requests.append(("POST", self.path))
        self._reply(200, b'{"ok": true}')  # deliberately accepts -> abnormal-success drill

    def log_message(self, *a):  # silence
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
mock_base = f"http://127.0.0.1:{server.server_address[1]}"
orig_file = smoke.ISOLATED_ENV_FILE
try:
    def allowlist(environments):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"marker_defaults": {"marker_key": "environment",
                                       "marker_value": "isolated-test"},
                   "environments": environments}, fh)
        fh.close()
        smoke.ISOLATED_ENV_FILE = Path(fh.name)

    MockHandler.requests.clear()
    allowlist([])
    ok, why, _ = smoke.verify_isolated_env(mock_base)
    check("F1 non-allowlisted -> denied", not ok, why[:60])
    check("F1 ZERO requests reached the server", MockHandler.requests == [],
          f"server saw: {MockHandler.requests}")

    MockHandler.requests.clear()
    MockHandler.info_body = {}
    allowlist([{"base": mock_base}])
    ok, why, _ = smoke.verify_isolated_env(mock_base)
    check("F2 marker mismatch -> denied", not ok, why[:60])
    check("F2 only GET /info emitted, ZERO POSTs",
          MockHandler.requests == [("GET", "/uibc/v1/info")],
          f"server saw: {MockHandler.requests}")

    MockHandler.requests.clear()
    MockHandler.info_body = {"environment": "isolated-test"}
    ok, why, entry = smoke.verify_isolated_env(mock_base)
    check("F3 verified isolated env", ok, why[:60])
    smoke.BASE = mock_base
    res5 = smoke.Result()
    smoke.negative_credential_probe(res5, caller=smoke.call)
    check("F3 POST reached server exactly once",
          MockHandler.requests.count(("POST", "/uibc/v1/submit")) == 1,
          f"server saw: {MockHandler.requests}")
    check("F3 200-accepting stub -> ABNORMAL SUCCESS FAIL", bool(res5.failed))
    check("F3 counters attempted==1 responded==1 possible==1 confirmed==0",
          res5.write_counters() == {"attempted": 1, "responded": 1,
                                    "possible_side_effects": 1,
                                    "confirmed_side_effects": 0},
          str(res5.write_counters()))
finally:
    smoke.ISOLATED_ENV_FILE = orig_file
    server.shutdown()

# --------------------------------------- G. isolated award flow (all paths)
print("G. isolated award flow — every write path closes out with pollution compare")

# G1: join non-2xx -> FAIL, pollution compare still ran (try/finally)
res = smoke.Result()
sc = flow_stub(join=(403, '{"error": "bad test credentials"}'))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
check("G1 join non-2xx -> FAIL", any(r["verdict"] == "FAIL" for r in rows_by_probe(res, "isolated: join")))
check("G1 pollution compare RAN despite early return",
      len(rows_by_probe(res, "deep pollution (all write paths)")) == 1,
      "try/finally close-out present")
check("G1 join attempt counted, possible==0",
      res.write_counters()["attempted"] == 1 and res.write_counters()["possible_side_effects"] == 0)

# G2: join unreachable (timeout analog) -> FAIL + pollution compare ran
res = smoke.Result()
sc = flow_stub(join=(0, "timeout"))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
check("G2 join unreachable -> FAIL", any(r["verdict"] == "FAIL" for r in rows_by_probe(res, "isolated: join")))
check("G2 pollution compare RAN", len(rows_by_probe(res, "deep pollution (all write paths)")) == 1)
check("G2 attempted==1 responded==0", res.write_counters()["responded"] == 0)

# G3: submit non-2xx -> FAIL + pollution compare ran
res = smoke.Result()
sc = flow_stub(submit=(422, '{"error": "invalid package"}'))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
check("G3 submit non-2xx -> FAIL", any(r["verdict"] == "FAIL" for r in rows_by_probe(res, "isolated: submit")))
check("G3 pollution compare RAN", len(rows_by_probe(res, "deep pollution (all write paths)")) == 1)

# G4: board without test agent -> UNVERIFIABLE + pollution compare ran
res = smoke.Result()
sc = flow_stub(board=(200, '{"leaderboard": []}'))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
g4 = rows_by_probe(res, "isolated: award observation")
check("G4 missing board row -> UNVERIFIABLE (not pass, not fail)",
      g4 and g4[0]["verdict"] == "UNVERIFIABLE", g4[0]["detail"][:70] if g4 else "")
check("G4 pollution compare RAN", len(rows_by_probe(res, "deep pollution (all write paths)")) == 1)
check("G4 confirmed_side_effects==0 (agent never observed)",
      res.write_counters()["confirmed_side_effects"] == 0)

# G5 REGRESSION (false-PASS defect): zero-score agent, NO medal fields, BAD spec
#    -> must be UNVERIFIABLE, never PASS
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 0.0})
smoke.isolated_award_flow(res, ENTRY, "其余", False, caller=sc)
g5 = rows_by_probe(res, "isolated: award observation")
check("G5 zero-score+no-medal+bad-spec -> UNVERIFIABLE (was false PASS)",
      g5 and g5[0]["verdict"] == "UNVERIFIABLE", g5[0]["detail"][:80] if g5 else "")
check("G5 verdict is NOT PASS", not (g5 and g5[0]["verdict"] == "PASS"))

# G6: zero-score agent, NO medal fields, GOOD spec -> PASS
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 0.0})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
g6 = rows_by_probe(res, "isolated: award observation")
check("G6 zero-score+no-medal+good-spec -> PASS", g6 and g6[0]["verdict"] == "PASS")

# G7: zero-score agent WITH medal fields -> FAIL (criterion violation)
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 0.0,
                               "medal": "bronze"})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
g7 = rows_by_probe(res, "isolated: award observation")
check("G7 zero-score+medal -> FAIL (real values, not literal comprehension)",
      g7 and g7[0]["verdict"] == "FAIL" and "bronze" in g7[0]["detail"],
      g7[0]["detail"][:80] if g7 else "")
check("G7 no literal '{k: row.get(k)...}' in detail",
      not any("row.get(k)" in r["detail"] for r in res.rows))

# G8 (updated round-5): cert served for ANOTHER PARTY (structured field exists
# but does not match) is positive evidence of a defect -> FAIL (round-4 treated
# mere absence of reference as UNVERIFIABLE; a present-but-wrong binding is worse)
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 5.0,
                               "cert_id": "UIBC-MEM-0001"},
               cert=(200, '{"cert_id": "UIBC-MEM-0001", "owner": "someone-else"}'))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
g8 = rows_by_probe(res, "isolated: cert verify")
check("G8 cert bound to another subject -> FAIL (structured mismatch is evidence)",
      g8 and g8[0]["verdict"] == "FAIL", g8[0]["detail"][:70] if g8 else "")

# G9: cert verify — 200 body WITH agent reference -> PASS
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 5.0,
                               "cert_id": "UIBC-MEM-0002"},
               cert=(200, json.dumps({"cert_id": "UIBC-MEM-0002",
                                      "agent": TA["agent_id"]})))
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
g9 = rows_by_probe(res, "isolated: cert verify")
check("G9 cert 200 + agent reference -> PASS", g9 and g9[0]["verdict"] == "PASS")

# G10: agent observed on board -> INDEPENDENTLY CONFIRMED side effect
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": 0.0})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
check("G10 confirmed_side_effects==1 (agent visible in post-write state)",
      res.write_counters()["confirmed_side_effects"] == 1, str(res.write_counters()))
c10 = res.write_counters()
check("G10 invariant attempted>=responded>=possible>=confirmed",
      c10["attempted"] >= c10["responded"] >= c10["possible_side_effects"]
      >= c10["confirmed_side_effects"])

# G11: no test_agent configured -> SKIP, zero writes, no pollution claims
res = smoke.Result()
sc = flow_stub()
smoke.isolated_award_flow(res, {"base": "x"}, None, False, caller=sc)
g11 = rows_by_probe(res, "isolated: award+cert flow")
check("G11 missing test_agent -> SKIP NOT VERIFIED", g11 and g11[0]["verdict"] == "SKIP")
check("G11 zero writes performed", res.write_counters()["attempted"] == 0)
check("G11 no pollution row emitted (no write happened)",
      len(rows_by_probe(res, "deep pollution")) == 0)

# ------------------------------------------------- H. schema medal check
print("H. schema medal check — structural nodes, substring trap, UNVERIFIABLE")
GOOD_SPEC = {"bronze": {"min_score": 1}}
schema_type_only = {"properties": {"award": {"medal": {"bronze": {"min_score": {"type": "number"}}}}}}
v, d = smoke.schema_medal_check(json.dumps(schema_type_only), GOOD_SPEC)
check("H1 (P1-updated) numeric type WITHOUT bound -> UNVERIFIABLE (a numeric "
      "type alone proves no threshold value or comparison semantics)",
      v == "UNVERIFIABLE", d[:80])
check("H1 node path still reported structurally", "$.properties.award.medal" in d, d[:80])
schema_str = {"description": "the medal min_score rules apply"}
v, d = smoke.schema_medal_check(json.dumps(schema_str), GOOD_SPEC)
check("H2 substring-only mention -> WARN (structural check, not substring)",
      v == "WARN", d[:70])
schema_weak = {"properties": {"medal": {"type": "string"}}}
v, d = smoke.schema_medal_check(json.dumps(schema_weak), GOOD_SPEC)
check("H3 nodes without object/numeric structure -> UNVERIFIABLE",
      v == "UNVERIFIABLE", d[:70])
v, d = smoke.schema_medal_check(json.dumps(schema_type_only), {"bronze": "其余"})
check("H4 prose /info + nodes -> OBSERVE (no threshold guessing)", v == "OBSERVE", d[:70])
v, d = smoke.schema_medal_check(None, GOOD_SPEC)
check("H5 schema unavailable -> UNVERIFIABLE", v == "UNVERIFIABLE")
schema_trap = {"note_min_score_x": 1}
v, d = smoke.schema_medal_check(json.dumps(schema_trap), GOOD_SPEC)
check("H6 lookalike key is NOT matched (exact key match only)", v == "WARN", d[:70])

# ------------------------------------------------- I. round-5 targeted regressions
print("I. round-5 regressions — schema path+semantics, cert 3-way identity, best_total hardening")

# ---- I1-I6: schema_medal_check — path relation + threshold semantics
print("I.a schema_medal_check: medal-related path AND provable threshold semantics")
sc_good = {"properties": {"medal": {"min_score": 1}}}
v, d = smoke.schema_medal_check(json.dumps(sc_good), GOOD_SPEC)
check("I1 medal-path + literal finite number -> PASS", v == "PASS", d[:70])

sc_unrelated = {"properties": {"pagination": {"min_score": 1}}}
v, d = smoke.schema_medal_check(json.dumps(sc_unrelated), GOOD_SPEC)
check("I2 min_score in UNRELATED path -> UNVERIFIABLE (was false PASS)",
      v == "UNVERIFIABLE", d[:80])

sc_constraint = {"properties": {"medal": {"min_score": {"type": "number", "minimum": 1}}}}
v, d = smoke.schema_medal_check(json.dumps(sc_constraint), GOOD_SPEC)
check("I3 schema-constraint node (type=number+minimum) -> PASS", v == "PASS", d[:70])

sc_bare_desc = {"properties": {"medal": {"min_score": {"description": "the floor"}}}}
v, d = smoke.schema_medal_check(json.dumps(sc_bare_desc), GOOD_SPEC)
check("I4 min_score node without numeric constraint -> UNVERIFIABLE",
      v == "UNVERIFIABLE", d[:70])

sc_str_num = {"properties": {"medal": {"min_score": "1"}}}
v, d = smoke.schema_medal_check(json.dumps(sc_str_num), GOOD_SPEC)
check("I5 string-number min_score -> UNVERIFIABLE (no type coercion)",
      v == "UNVERIFIABLE", d[:70])

sc_nan = json.loads(json.dumps({"properties": {"medal": {"min_score": float("nan")}}}))
v, d = smoke.schema_medal_check(json.dumps(sc_nan), GOOD_SPEC)
check("I6 NaN min_score -> UNVERIFIABLE (non-finite)", v == "UNVERIFIABLE", d[:70])

# ---- I7-I12: _cert_verify_check — structured 3-way identity
print("I.b cert verify: request-id round-trip x subject binding, never substring")
TA_ID = TA["agent_id"]


def run_cert(body):
    r = smoke.Result()

    class OneShot:
        def __call__(self, method, path, payload=None, timeout=25):
            return (200, json.dumps(body), {})

    smoke._cert_verify_check(r, "C1", TA_ID, caller=OneShot())
    return rows_by_probe(r, "cert verify")


rows = run_cert({"cert_id": "C1", "owner": TA_ID})
check("I7 id round-trip + exact owner match -> PASS",
      rows and rows[0]["verdict"] == "PASS", rows[0]["detail"][:70] if rows else "")
rows = run_cert({"cert_id": "C1", "owner": "someone-else"})
check("I8 subject MISMATCH -> FAIL (was UNVERIFIABLE pre-round-5)",
      rows and rows[0]["verdict"] == "FAIL", rows[0]["detail"][:70] if rows else "")
rows = run_cert({"cert_id": "OTHER-CERT", "owner": TA_ID})
check("I9 response cert id != requested -> FAIL (wrong cert served)",
      rows and rows[0]["verdict"] == "FAIL", rows[0]["detail"][:70] if rows else "")
rows = run_cert({"cert_id": "C1"})
check("I10 id matches but no subject field -> UNVERIFIABLE",
      rows and rows[0]["verdict"] == "UNVERIFIABLE", rows[0]["detail"][:70] if rows else "")
rows = run_cert({"cert_id": "C1", "note": f"hello {TA_ID} world"})
check("I11 substring-only mention of agent -> UNVERIFIABLE (substring trap removed)",
      rows and rows[0]["verdict"] == "UNVERIFIABLE", rows[0]["detail"][:70] if rows else "")
rows = run_cert(["C1", TA_ID])
check("I12 non-object body (list) -> UNVERIFIABLE",
      rows and rows[0]["verdict"] == "UNVERIFIABLE", rows[0]["detail"][:70] if rows else "")

# ---- I13-I16: isolated_award_flow — best_total hardening, report must survive
print("I.c best_total: malformed values recorded as FAIL, report + pollution intact")
res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": "high",
                               "medal": "gold"})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
obs = rows_by_probe(res, "award observation")
check("I13 string best_total -> FAIL (recorded, not crashed)",
      obs and obs[0]["verdict"] == "FAIL" and "MALFORMED" in obs[0]["detail"],
      obs[0]["detail"][:70] if obs else "no row")
check("I13 pollution close-out still ran after malformed-data return",
      len(rows_by_probe(res, "deep pollution")) == 1)

res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"], "best_total": True})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
obs = rows_by_probe(res, "award observation")
check("I14 bool best_total -> FAIL (bool is not a number here)",
      obs and obs[0]["verdict"] == "FAIL", obs[0]["detail"][:70] if obs else "no row")

res = smoke.Result()
sc = flow_stub(spec_board_row={"agent_id": TA["agent_id"]})
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
obs = rows_by_probe(res, "award observation")
check("I15 missing best_total -> UNVERIFIABLE (absence is not zero)",
      obs and obs[0]["verdict"] == "UNVERIFIABLE", obs[0]["detail"][:70] if obs else "no row")
check("I15 pollution close-out still ran", len(rows_by_probe(res, "deep pollution")) == 1)

res = smoke.Result()
nan_row = json.loads(json.dumps({"agent_id": TA["agent_id"], "best_total": float("nan")}))
sc = flow_stub(spec_board_row=nan_row)
smoke.isolated_award_flow(res, ENTRY, {"min_score": 1}, True, caller=sc)
obs = rows_by_probe(res, "award observation")
check("I16 NaN best_total -> FAIL (non-finite)", obs and obs[0]["verdict"] == "FAIL",
      obs[0]["detail"][:70] if obs else "no row")

# ------------------------- J. P1 negative controls: threshold proof standard
print("J. min_score proof standard — value AND comparison semantics both required")
print("J.a threshold proof requires a numeric bound >0 (type:number is NOT proof)")


def schema_check_of(minscore_node):
    s = {"properties": {"medal": {"min_score": minscore_node}}}
    return smoke.schema_medal_check(json.dumps(s), GOOD_SPEC)


v, d = schema_check_of({"type": "number"})
check("J1 type:number alone -> UNVERIFIABLE (P1 core: type != threshold)",
      v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "number", "minimum": "1"})
check("J2 minimum as STRING -> UNVERIFIABLE (invalid constraint value)",
      v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "number", "minimum": 0})
check("J3 minimum=0 -> UNVERIFIABLE (zero threshold cannot exclude 0-score)",
      v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "number", "minimum": -1})
check("J4 negative minimum -> UNVERIFIABLE", v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "number", "minimum": True})
check("J5 minimum as BOOL -> UNVERIFIABLE (invalid constraint value)",
      v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "number", "exclusiveMinimum": 1})
check("J6 exclusiveMinimum=1 vs /info '>=' 1 -> FAIL (comparison-direction mismatch)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:90])
v, d = schema_check_of(0)
check("J7 literal threshold 0 -> UNVERIFIABLE (cannot exclude 0-score)",
      v == "UNVERIFIABLE", d[:80])
v, d = schema_check_of({"type": "integer", "minimum": 2.5})
check("J8 minimum=2.5 vs /info floor 1 -> FAIL (threshold VALUE mismatch)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:90])
# J9 = the exact cross-source counterexample from external review:
# /info declares floor 1, schema declares minimum 0.5 -> must NOT report agreement
v, d = schema_check_of({"type": "number", "minimum": 0.5})
check("J9 /info floor=1 vs schema minimum=0.5 -> FAIL (cross-source mismatch, "
      "was false PASS 'schema-backed')",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:90])
v, d = schema_check_of({"type": "number", "minimum": 1})
check("J10 /info floor=1 AND schema minimum=1 -> PASS (same value AND same "
      "comparison semantics — consistency proven)",
      v == "PASS" and "MATCHES" in d, d[:90])
v, d = smoke.schema_medal_check(
    json.dumps({"properties": {"medal": {"min_score": {"type": "number",
                                                       "minimum": 1}}}}),
    {"bronze": ">1"})
check("J11 /info '>1' vs schema minimum=1 -> FAIL (op mismatch: >= is not >)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:90])
v, d = smoke.schema_medal_check(
    json.dumps({"properties": {"medal": {"min_score": {"type": "number",
                                                       "exclusiveMinimum": 0}}}}),
    {"bronze": ">0"})
check("J12 schema bound 0 (cannot exclude 0-score) with /info '>0' -> UNVERIFIABLE",
      v == "UNVERIFIABLE", d[:90])

# ------------- K. combined constraints: EFFECTIVE bound, never any()-matching
print("K. combined constraints — all apply simultaneously; match on the effective (strictest) bound")
multi = lambda m, em: {"properties": {"medal": {"min_score": {
    "type": "number", "minimum": m, "exclusiveMinimum": em}}}}
INFO_1 = {"bronze": {"min_score": 1}}  # /info semantics: score >= 1

v, d = smoke.schema_medal_check(json.dumps(multi(1, 2)), INFO_1)
check("K1 /info >=1 vs schema {min:1, exclMin:2} -> FAIL (effective threshold "
      "is > 2, not any()-matched minimum=1)",
      v == "FAIL" and "effective threshold > 2.0" in d, d[:100])
v, d = smoke.schema_medal_check(json.dumps(multi(1, 0)), INFO_1)
check("K2 /info >=1 vs schema {min:1, exclMin:0} -> PASS (effective threshold "
      "is >= 1: the looser redundant constraint does not tighten the rule)",
      v == "PASS" and "MATCHES" in d, d[:100])
v, d = smoke.schema_medal_check(json.dumps(multi(2, 2)),
                                {"bronze": ">2"})
check("K3 /info >2 vs schema {min:2, exclMin:2} -> PASS (effective > 2, same "
      "value AND direction)",
      v == "PASS" and "MATCHES" in d, d[:100])
v, d = smoke.schema_medal_check(json.dumps(multi(2, 2)), INFO_1)
check("K4 /info >=2 vs schema {min:2, exclMin:2} -> FAIL (effective > 2 vs "
      "declared >= 2: same value, different direction)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:100])

# ---------- L. round-9: CROSS-NODE combinators (allOf branches, anyOf honesty)
print("L. cross-node combinators — allOf merges ALL branches; anyOf/oneOf/not -> UNVERIFIABLE")
L_INFO = {"bronze": {"min_score": 1}}  # /info semantics: score >= 1


def branch_schema(comb, *subs):
    """Medal min_score nodes spread across separate combinator branches."""
    return {comb: [{"properties": {"medal": {"min_score": s}}} for s in subs]}


v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 1}, {"exclusiveMinimum": 2})), L_INFO)
check("L1 /info >=1 vs allOf{min:1, exclMin:2 branches} -> FAIL (combined "
      "effective threshold > 2 — one matching branch must not hide a stricter one)",
      v == "FAIL" and "effective threshold > 2.0" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 1}, {"minimum": 1})), L_INFO)
check("L2 /info >=1 vs allOf{min:1, min:1 branches} -> PASS (branches genuinely "
      "equivalent to /info)",
      v == "PASS" and "MATCHES" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("anyOf", {"minimum": 1}, {"exclusiveMinimum": 2})), L_INFO)
check("L3 threshold under anyOf -> UNVERIFIABLE (branch-selection semantics not "
      "reliably parsed; allOf/anyOf never conflated, no strictest-bound guessing)",
      v == "UNVERIFIABLE" and "anyOf" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 1}, {"minimum": 5})), L_INFO)
check("L4 /info >=1 vs allOf{min:1, min:5 branches} -> FAIL (combined effective "
      "threshold >= 5 contradicts /info)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 1}, {"type": "number"})), L_INFO)
check("L5 allOf with one unprovable branch -> UNVERIFIABLE (branches apply "
      "simultaneously; one proven branch cannot certify the rule)",
      v == "UNVERIFIABLE" and "NOT provable" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 1})), L_INFO)
check("L6 single-branch allOf regression -> PASS (degenerates to single node, "
      "J10 semantics preserved)",
      v == "PASS" and "MATCHES" in d, d[:110])
v, d = smoke.schema_medal_check(json.dumps(
    branch_schema("allOf", {"minimum": 0.5}, {"exclusiveMinimum": 0.5})), L_INFO)
check("L7 /info >=1 vs allOf{0.5, >0.5 branches} -> FAIL (combined effective "
      "threshold > 0.5 — cross-node, was per-node false-PASS territory)",
      v == "FAIL" and "CROSS-SOURCE MISMATCH" in d, d[:110])

# ----------------------------------------------------------------------------
print(f"\n{len(FAILURES)} failure(s)" + (f": {FAILURES}" if FAILURES else " — all negative controls hold"))
sys.exit(1 if FAILURES else 0)
