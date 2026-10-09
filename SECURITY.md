# Security Policy

## Scope

This repository hosts the **UIBC（International Autonomy Race）competition
specification and materials**. It contains documents, methodology text,
knowledge-base content and small utility scripts.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for an
unfixed vulnerability.

- Email: **zhaoxinghua09@gmail.com**
- Include: affected file/path, a minimal reproduction, and the impact you
  believe it has.
- We aim to acknowledge reports within a few business days.

## What counts as a security issue here

- A path that lets a participant or third party **write to the production
  leaderboard** outside the documented submission flow.
- A credential / token leak in the repository or its CI.
- A dependency or script that executes untrusted code from a submission.
- Any way to **forge a certificate / verification result** accepted by the
  verifier.

## Non-issues

- Broken relative links, typos, or documentation gaps — please open a normal
  issue using the bug-report template.

## Disclosure

We follow coordinated disclosure: please give us reasonable time to fix the
issue before any public write-up.

---

© 2026 赵兴华 / Steven Zhao·China。保留所有权利（All Rights Reserved）。
