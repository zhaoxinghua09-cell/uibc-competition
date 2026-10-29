#!/usr/bin/env python3
"""uibc_smoke_selftest — OFFLINE negative controls for uibc_smoke.

Proves each new detection capability can actually FAIL (a test that cannot
fail proves nothing). No network I/O: server interaction is stubbed.

Covers (review round 3):
  A. medal-spec parser: NaN / Infinity / -Infinity / negative / zero / bool /
     string-number / missing field / prose / wrong type -> all FAIL.
  B. deep-pollution compare: unavailable snapshot (None) -> UNVERIFIABLE
     (never PASS); identical -> PASS; changed -> FAIL.
  C. write accounting: attempted POST is never reported as zero writes;
     unreachable POST counts as attempted but not responded.
  D. abnormal success: a 2xx on an invalid-credential POST is an immediate
     FAIL and increments confirmed_side_effects.

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


# ---------------------------------------------------------------- A. parser
print("A. medal-spec parser (negative controls: bad specs must be rejected)")
cases = [
    ("其余", False), ("others", False), (None, False), ("", False),
    (">0", True), ("> 0", True), (">0.5", True), (">=1", True), (">= 1", True), (">=0.5", True),
    (">=0", False), (">=0.0", False),  # >=0 admits a 0-score entrant
    (">-1", False), (">=-1", False),
    (">=x", False), (">min", False), (">= 1 (bronze)", False),  # prose
    ({"min_score": 1}, True), ({"min_score": 10}, True), ({"min_score": 0.5}, True),
    ({"min_score": 0}, False), ({"min_score": -3}, False),
    ({"min_score": "1"}, False),          # string-encoded number
    ({"min_score": True}, False),         # bool is not a score
    ({"min_score": None}, False),         # missing value
    ({"minimum": 1}, False),              # differently-named field (old 'min' bug)
    ({}, False),                          # missing min_score entirely
    (["min"], False), (3, False),
]
for spec, want in cases:
    _, ok, why = smoke._parse_bronze_floor(spec)
    check(f"parse {spec!r} -> {'PASS' if want else 'FAIL'}", ok == want, why[:70])
special = [float("nan"), float("inf"), float("-inf")]
for v in special:
    _, ok, why = smoke._parse_bronze_floor({"min_score": v})
    check(f"parse min_score={v!r} -> FAIL (non-finite)", ok is False, why[:70])

# ------------------------------------------------- B. deep-pollution compare
print("B. deep-pollution compare (negative control: None must never PASS)")
same = {"total_agents": 36, "row_count": 36, "rows_sha256": "a" * 64}
v1, d1 = smoke.compare_states(same, dict(same))
check("identical snapshots -> PASS", v1 == "PASS", d1[:60])
v2, d2 = smoke.compare_states(None, same)
check("before=None -> UNVERIFIABLE/FAIL", v2 in ("UNVERIFIABLE", "FAIL"), d2[:60])
v3, d3 = smoke.compare_states(same, None)
check("after=None -> UNVERIFIABLE/FAIL", v3 in ("UNVERIFIABLE", "FAIL"), d3[:60])
v4, d4 = smoke.compare_states(None, None)
check("both=None (full outage) -> UNVERIFIABLE/FAIL", v4 in ("UNVERIFIABLE", "FAIL"), d4[:60])
diff = dict(same, total_agents=37)
v5, d5 = smoke.compare_states(same, diff)
check("changed snapshot -> FAIL", v5 == "FAIL", d5[:60])

# --------------------------- C+D. write accounting & abnormal success (stubbed)
print("C. write accounting (a sent POST is never reported as zero writes)")
# stub: unreachable (no HTTP response)
res = smoke.Result()
calls = []


def stub_unreachable(method, path, body=None, timeout=25):
    calls.append((method, path))
    return 0, "conn refused", {}


smoke.negative_credential_probe(res, caller=stub_unreachable)
check("unreachable POST: attempted==1", res.writes_attempted == 1, str(res.write_counters()))
check("unreachable POST: responded==0", res.writes_responses == 0)
check("unreachable POST: verdict FAIL", bool(res.failed))
check("stub really sent a POST", calls and calls[0][0] == "POST")

print("D. abnormal success (2xx on bogus token = FAIL + side effect)")
res2 = smoke.Result()


def stub_2xx(method, path, body=None, timeout=25):
    return 200, '{"ok": true}', {}


smoke.negative_credential_probe(res2, caller=stub_2xx)
check("2xx bogus token -> FAIL", bool(res2.failed))
check("2xx bogus token -> confirmed_side_effects==1",
      res2.writes_side_effects == 1, str(res2.write_counters()))
check("2xx bogus token -> responded==1", res2.writes_responses == 1)

res3 = smoke.Result()


def stub_401(method, path, body=None, timeout=25):
    return 401, '{"error": "unauthorized"}', {}


smoke.negative_credential_probe(res3, caller=stub_401)
check("401 bogus token -> PASS (proper rejection)", not res3.failed)
check("401: attempted==1 responded==1 side_effects==0",
      res3.writes_attempted == 1 and res3.writes_responses == 1
      and res3.writes_side_effects == 0, str(res3.write_counters()))

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
            self._reply(200, __import__("json").dumps(MockHandler.info_body).encode())
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

    # F1: URL not in allowlist -> refusal, ZERO requests
    MockHandler.requests.clear()
    allowlist([])
    ok, why, _ = smoke.verify_isolated_env(mock_base)
    check("F1 non-allowlisted -> denied", not ok, why[:60])
    check("F1 ZERO requests reached the server", MockHandler.requests == [],
          f"server saw: {MockHandler.requests}")

    # F2: allowlisted but marker handshake fails -> only GET /info, zero POST
    MockHandler.requests.clear()
    MockHandler.info_body = {}
    allowlist([{"base": mock_base}])
    ok, why, _ = smoke.verify_isolated_env(mock_base)
    check("F2 marker mismatch -> denied", not ok, why[:60])
    check("F2 only GET /info emitted, ZERO POSTs",
          MockHandler.requests == [("GET", "/uibc/v1/info")],
          f"server saw: {MockHandler.requests}")

    # F3: allowlisted + marker OK -> gate opens; POST path + abnormal-success work
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
    check("F3 counters attempted==1 responded==1 side_effects==1",
          res5.write_counters() == {"attempted": 1, "responded": 1,
                                    "confirmed_side_effects": 1},
          str(res5.write_counters()))
finally:
    smoke.ISOLATED_ENV_FILE = orig_file
    server.shutdown()

# ----------------------------------------------------------------------------
print(f"\n{len(FAILURES)} failure(s)" + (f": {FAILURES}" if FAILURES else " — all negative controls hold"))
sys.exit(1 if FAILURES else 0)
