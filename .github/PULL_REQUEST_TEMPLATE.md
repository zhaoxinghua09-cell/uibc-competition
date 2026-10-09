<!--
Thanks for the pull request. Keep it small and evidenced.

By submitting, you confirm you have the right to contribute this content and
agree it is licensed under the repository's LICENSE.
-->

## What this changes

<!-- One or two sentences. Link the issue it closes, if any. -->

## Why

<!-- The defect or gap this closes. If it is a "silent failure" (a check that
     passed when it should have failed), say so explicitly — those matter most. -->

## Evidence (required)

<!-- Paste the command you ran and its output. A claim without evidence is not
     yet a fix. If you cannot produce evidence, write NOT VERIFIED and say why. -->

```
```

## Checklist

- [ ] The repository's own gate passes locally (`.github/workflows/repo-gate.yml`
      logic: `python3 tools/repo_gate_check.py selftest && python3 tools/repo_gate_check.py check --root .`).
- [ ] If I changed a released version, I updated `CHANGELOG.md` **and** the tag
      correspondence stays true (`CITATION.cff` version == newest CHANGELOG heading).
- [ ] No secrets, tokens, or personal data are included.
- [ ] I did not remove or weaken an existing check without saying so above.
