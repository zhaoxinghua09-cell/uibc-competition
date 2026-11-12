# Independent re-run register

Anyone can verify a `.uibc` package without our permission (`pip install uibc-core`, offline verify). This
register exists so that independent verification is **visible** rather than private.

Rules:

1. You run the verifier yourself and open an issue with the printed result; we record the entry here.
2. An entry is **self-reported verification, not an endorsement by us**. Nothing here may be quoted as
   "partner", "certified", or "endorsed".
3. A disagreement between your result and ours is recorded as a disagreement, not resolved by whichever
   party is louder.

| # | Date | Who | What was run | Result | Disagreement |
|---|---|---|---|---|---|
| 1 | 2026-11-12 | VATE (Poke-nushi) | Independent Ed25519 reader over the receiving example pinned to `dc9eba3`; no UIBC import | All 66 selected comparisons across 22 conditions agree; resealed case yields `not_assessed` without an expected key | none reported |

Note on entry 1: it also surfaced a real defect — the package's hash-pinned lock no longer matches the
`cffi` wheel PyPI serves, so `--require-hashes` installs fail on a clean machine. Recorded as a defect to
fix, not as a point in our favour.
