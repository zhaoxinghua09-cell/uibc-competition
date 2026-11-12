# RUBRIC v1 — the single source for UIBC-MEM scoring

Status: **in force for the public self-test board**; the committee may amend it, and an amendment is a new
file (`RUBRIC-v2.md`), never an edit in place — scores already awarded keep pointing at the file that produced
them.

## Dimensions and weights

| ID | Dimension | Weight | The question | Mechanical check |
|---|---|---|---|---|
| S1 | Portability | 40% | Can the memory be carried at all? | `entries` structure, `content` completeness |
| S2 | Faithfulness | 30% | Does it still recognise itself? | timestamps parseable and monotonic, duplication ratio |
| S3 | Accountability | 30% | Can errors be audited? | `source` / `origin` / `agent_id` / `schema_version` plus manifest |

`score = 0.40*S1 + 0.30*S2 + 0.30*S3`, each sub-score normalised to 0–100.

## Verdict rules

- A submission that cannot be evaluated is `INCONCLUSIVE`. **`INCONCLUSIVE` is never rounded up to a pass**,
  gets no medal, and is excluded from ranking rather than placed at the bottom.
- Fractions a scorer cannot verify are not silently treated as zero; a missing field fails the check it
  belongs to, an unreadable file fails S1.
- Every score carries `scorer_version`. A new scorer version does not rewrite historical scores.

## Data sets

- The public board scores **self-test** submissions: the submitter runs the same mechanical checks and gets
  the same number, so the board is recomputable by anyone.
- The official ranking uses a **private blind set**, never published. Its size and sampling method are
  published in this file when the committee fixes them; until that happens this file states them as
  **not yet fixed** rather than quoting a number that cannot be defended.
- Anti-cheating: exact duplicates of a prior submission are rejected outright (SHA-256 of the normalised
  package); near-duplicates are **flagged, not rejected**, and go to human adjudication (SimHash). Flagging
  without auto-rejection is deliberate: a near-duplicate can be a legitimate iteration.

## Receipt

A scored submission yields a `.uibc` package that verifies offline (`pip install uibc-core`, then
`uibc verify <package> --pubkey <key>`) and online (the hosted `verify` endpoint, when reachable). Both paths
must be exercised per season; a path that has never been exercised is reported as unverified, not assumed.
