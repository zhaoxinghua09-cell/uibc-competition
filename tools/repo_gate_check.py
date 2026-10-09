#!/usr/bin/env python3
"""repo_gate_check — the real check behind a required status check
(`repo-gate` for release repos, `lgd-ci-gate` for the LGD family).

Origin: the LGD gate (tools/lgd_gate_check.py) proved the pattern — a required
status check that cannot fail proves nothing. This is that gate generalised to
the whole family and extended with the checks the family was missing.

Checks (check mode).  Each is required only when its inputs are present, so the
gate is deployable repo-wide without false reds:
  1. Required files exist and are non-empty: README.md, LICENSE.
  2. CITATION.cff (when present) carries non-empty title / version /
     date-released / authors.  An **empty** value (`version: ""`) is a failure,
     not a pass.
  3. CITATION.cff version == newest released CHANGELOG.md heading, where
     "newest" = the **maximum** version heading present (normalized numeric
     compare: "v1.6.0" == "1.6.0" == "[1.6.0]").  Taking merely the *first*
     heading is a real bypass: a stray newer heading lower down was missed.
  4. Newest released CHANGELOG version has a matching git tag.
  5. `--expect-tag REF` (release-time guard).
  6. Relative markdown links in README.md / TLDR.md resolve **inside the tree**.
     Handles: inline links, optional `"title"` / `'title'` / `(title)` suffix,
     `<...>` wrapped destinations (may contain spaces), bare destinations that
     contain spaces, and reference-style `[label]: url` definitions.  A link
     that escapes the tree via `..` is a failure.
  7. License family consistency: the family declared in CITATION.cff `license:`
     must equal the family of the LICENSE text (All-Rights-Reserved / MIT /
     Apache-2.0 / CC-BY-4.0).  Real defect 2026-10-09: LICENSE reserved all
     rights while CITATION.cff still granted CC-BY-4.0.

Round-2 hardening (2026-10-09): the first release of this gate was attacked by
an independent red-team seat, which found six bypass classes (B1 spaces in path,
B2 title suffix, B3 reference-style, B4 `..` escape, B5 non-max changelog
heading, B6 empty metadata value).  Each is now a permanent negative control in
`selftest`, so the gate cannot silently regress.

Selftest mode builds a known-good fixture (must PASS) and known-broken fixtures
(each must FAIL). If any broken fixture is not rejected, selftest exits
non-zero: the gate has gone decorative and the run must be red.

Standard library only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REQUIRED_FILES = ["README.md", "LICENSE"]
CFF_REQUIRED_KEYS = ["title", "version", "date-released", "authors"]
LINK_FILES = ["README.md", "TLDR.md"]

# `## v1.6.0`, `## 1.6.0`, `## [0.2.1] — 2026-09-17` ; NOT `## [Unreleased]`
CHANGELOG_VER_RE = re.compile(r"^##(?!#)\s*\[?\s*[vV]?(\d+\.\d+(?:\.\d+)?)\s*\]?")


def _unfenced_lines(text: str) -> list[str]:
    """Drop lines inside ``` / ~~~ code fences: a fenced `## v9.9.9` is docs,
    not a release heading (treating it as one is a false red)."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else line)
    return out


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)
        print(f"  PASS  {msg}")

    def bad(self, msg: str) -> None:
        self.failed.append(msg)
        print(f"  FAIL  {msg}")

    def skip(self, msg: str) -> None:
        self.skipped.append(msg)
        print(f"  SKIP  {msg}")


def norm_ver(v: str) -> str:
    v = v.strip().strip('"').strip("[]").lstrip("vV").strip()
    parts = v.split(".")
    try:
        return ".".join(str(int(p)) for p in parts)
    except ValueError:
        return v


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in norm_ver(v).split("."))
    except ValueError:
        return (0,)


def cff_value(text: str, key: str) -> str | None:
    """Inline value of a top-level CFF key (may be '' for a block scalar)."""
    m = re.search(rf"^{re.escape(key)}\s*:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def cff_value_status(text: str, key: str) -> tuple[str | None, str]:
    """Return (value, status) where status in {'missing','empty','ok'}.

    A YAML block scalar (value on following indented lines, e.g. `authors:`)
    counts as present/non-empty.
    """
    m = re.search(rf"^{re.escape(key)}\s*:(.*)$", text, re.M)
    if not m:
        return None, "missing"
    inline = m.group(1).strip()
    if inline and inline not in ('""', "''"):
        return inline, "ok"
    # look ahead for an indented block belonging to this key
    rest = text[m.end():]
    for line in rest.splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t"):
            return "<block>", "ok"
        break
    return inline, "empty"


def changelog_newest(text: str) -> str | None:
    """The MAXIMUM released version heading (not merely the first one)."""
    vers: list[str] = []
    for line in _unfenced_lines(text):
        m = CHANGELOG_VER_RE.match(line)
        if m:
            vers.append(norm_ver(m.group(1)))
    if not vers:
        return None
    return max(vers, key=_ver_key)


def version_has_tag(tag_names: list[str], version: str) -> bool:
    want = norm_ver(version)
    return any(norm_ver(t) == want for t in tag_names)


# ------------------------------------------------------------------ links --
def _inline_targets(text: str) -> list[str]:
    """Extract destinations of inline markdown links `]( … )`.

    Tolerates a `<...>` wrapped destination (may contain spaces) and an
    optional `"title"` / `'title'` / `(title)` suffix.  A bare destination
    that itself contains spaces (lenient / non-CommonMark renderers) is kept
    whole rather than silently dropped.
    """
    out: list[str] = []
    for m in re.finditer(r"\]\(", text):
        start = m.end()
        end = text.find(")", start)
        if end == -1:
            continue
        inner = text[start:end].strip()
        if inner.startswith("<"):
            close = inner.find(">")
            if close != -1:
                out.append(inner[1:close].strip())
                continue
        titled = re.match(r"(\S+)(?:\s+[\"'(].*[\"')])?$", inner)
        if titled:
            out.append(titled.group(1))
        else:
            out.append(inner)  # bare destination containing spaces
    return out


def _reference_targets(text: str) -> list[str]:
    """Destinations of reference-style link definitions: `[label]: url`."""
    return [m.group(1) for m in
            re.finditer(r"^\s*\[[^\]]+\]:\s*(\S+)", text, re.M)]


def _resolve_inside(root: Path, path_part: str) -> tuple[bool, bool]:
    """Return (escapes, exists).

    `escapes` is True if the link leaves the tree at ANY point on the way down,
    not merely if its final resolved path is outside. This closes the
    "escape-and-return" bypass `../<this-repo>/docs/x.md`, which resolves back
    inside the tree yet is a dead link on GitHub (a `..` above the repo root is
    a 404).

    `exists` is checked segment-by-segment against an exact-case directory
    listing, so the verdict does not depend on the host filesystem's case
    sensitivity (Windows/macOS would otherwise hide a case-only mismatch that
    Linux CI reds).
    """
    segs = [s for s in path_part.replace("\\", "/").split("/") if s not in ("", ".")]
    depth = 0
    cur = root
    for s in segs:
        if s == "..":
            if depth == 0:
                return True, False
            depth -= 1
            cur = cur.parent
            continue
        depth += 1
        try:
            names = {e.name for e in os.scandir(cur)}
        except OSError:
            return False, False
        if s not in names:
            return False, False
        cur = cur / s
    return False, True


def check_links(root: Path, rep: Report) -> None:
    seen_dead = 0
    seen_live = 0
    checked: list[str] = []
    for rel in LINK_FILES:
        f = root / rel
        if not f.is_file():
            continue
        checked.append(rel)
        text = f.read_text(encoding="utf-8")
        for target in _inline_targets(text) + _reference_targets(text):
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_part = target.split("#", 1)[0].split("?", 1)[0].strip()
            if not path_part:
                continue
            escapes, exists = _resolve_inside(root, path_part)
            if escapes:
                seen_dead += 1
                rep.bad(f"{rel}: relative link escapes the tree -> {target}")
            elif exists:
                seen_live += 1
            else:
                seen_dead += 1
                rep.bad(f"{rel}: dead relative link -> {target}")
    if seen_dead == 0:
        rep.ok(f"all relative links resolve ({seen_live} checked in "
               f"{', '.join(checked) or 'n/a'})")


# ------------------------------------------------------------------ license --
# --- license logic -------------------------------------------------------
#
# Two hard lessons (both found by independent review on 2026-10-09):
#  (i) "All rights reserved" is *boilerplate* in many MIT/Apache LICENSE files.
#      Reading it first made the gate (a) false-red a genuine MIT license and
#      (b) false-green a LICENSE that grants MIT while CITATION claims ARR.
#      Fix: an explicit standard-license marker always wins; the reserved-rights
#      phrase is consulted only when no standard header is present.
#  (ii) CFF 1.2.0 `license` is a *strict enum* of SPDX ids: `LicenseRef-*`,
#      `All Rights Reserved`, `NONE`, `NOASSERTION` are NOT valid values. When a
#      work is not under an SPDX-listed license, the spec's fallback is to omit
#      `license:` and provide `license-url:`. This gate enforces that.
_STANDARD_MARKERS = [
    ("apache-2.0", r"apache\s+license[^\n]{0,40}2\.0"),
    ("mit", r"\bmit\s+license\b"),
    ("bsd-3-clause", r"bsd\s+3-?clause"),
    ("bsd-2-clause", r"bsd\s+2-?clause"),
    ("gpl-3.0", r"gnu\s+general\s+public\s+license[^\n]{0,40}(version\s*3|v3)"),
    ("lgpl-3.0", r"gnu\s+lesser\s+general\s+public\s+license"),
    ("mpl-2.0", r"mozilla\s+public\s+license[^\n]{0,40}(2\.0|version\s*2)"),
    ("isc", r"\bisc\s+license\b"),
    ("unlicense", r"\bthe\s+unlicense\b|this\s+is\s+free\s+and\s+unencumbered"),
    ("cc0-1.0", r"cc0[\s\-]1\.0|creative\s+commons\s+zero"),
    ("cc-by-4.0", r"creative\s+commons\s+attribution[^\n]{0,40}4\.0|cc[-\s]by[-\s]4\.0"),
]

# A curated subset of the CFF 1.2.0 `license-enum` (SPDX ids).  Kept in a
# lowercased lookup so comparison is case-insensitive.  The authoritative check
# is the schema validation CI step; this is the offline-deterministic subset.
_CFF_ENUM: set[str] = {
    x.lower() for x in [
        "0BSD", "AFL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "Apache-1.1",
        "Apache-2.0", "Artistic-2.0", "Beerware", "BSD-2-Clause", "BSD-3-Clause",
        "BSD-3-Clause-Clear", "BSD-4-Clause", "BSL-1.0", "CC-BY-3.0", "CC-BY-4.0",
        "CC-BY-NC-4.0", "CC-BY-NC-ND-4.0", "CC-BY-NC-SA-4.0", "CC-BY-ND-4.0",
        "CC-BY-SA-4.0", "CC0-1.0", "CECILL-2.1", "EPL-1.0", "EPL-2.0", "EUPL-1.1",
        "EUPL-1.2", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
        "GPL-3.0-or-later", "ISC", "LGPL-2.1-only", "LGPL-2.1-or-later",
        "LGPL-3.0-only", "LGPL-3.0-or-later", "MIT", "MIT-0", "MPL-1.1", "MPL-2.0",
        "MS-PL", "MS-RL", "NCSA", "ODbL-1.0", "OFL-1.1", "OSL-3.0", "PostgreSQL",
        "Python-2.0", "Unlicense", "UPL-1.0", "Vim", "WTFPL", "X11", "Zlib",
    ]
}


def license_family_from_text(text: str) -> str | None:
    """Classify a LICENSE document into a comparable family, or None.

    An explicit standard-license marker always wins; the "All rights reserved"
    phrase (common boilerplate inside MIT/Apache files) is only consulted when
    no standard header is present.
    """
    t = text.lower()
    for fam, pat in _STANDARD_MARKERS:
        if re.search(pat, t):
            return fam
    if "all rights reserved" in t or "保留所有权利" in text:
        return "arr"
    return None


def license_family_from_spdx(expr: str) -> str | None:
    """Map a CITATION.cff `license:` SPDX expression to a family.

    Returns 'multi' for an OR/AND expression spanning >1 family (ambiguous: the
    gate must not silently collapse it), else the single family or None.
    """
    parts = re.split(r"\s+(?:OR|AND)\s+", expr.strip().strip('"'), flags=re.I)
    fams: set[str] = set()
    for p in parts:
        pl = p.strip().strip("()").lower()
        if not pl:
            continue
        if "allrightsreserved" in pl or "all rights reserved" in pl or pl == "arr":
            fams.add("arr")
        elif pl.startswith("licenseref-"):
            fams.add(f"?{pl}")
        elif pl in {"none", "noassertion"}:
            fams.add(f"?{pl}")
        else:
            got = next((f for f, pat in _STANDARD_MARKERS
                        if re.search(pat, pl) or pl == f or pl.startswith(f + "-")), None)
            fams.add(got or f"?{pl}")
    if len(fams) == 1:
        return next(iter(fams))
    if len(fams) > 1:
        return "multi"
    return None


def check_license_sync(root: Path, rep: Report) -> None:
    lic = root / "LICENSE"
    cff = root / "CITATION.cff"
    if not lic.is_file() or not cff.is_file():
        rep.skip("license sync not applicable (LICENSE or CITATION.cff absent)")
        return
    text = cff.read_text(encoding="utf-8")
    expr, status = cff_value_status(text, "license")
    fam_lic = license_family_from_text(lic.read_text(encoding="utf-8"))
    if status == "missing":
        url = cff_value_status(text, "license-url")[0]
        if url:
            rep.ok("license declared by URL only (license-url) — correct CFF 1.2.0 "
                   "fallback when the license is not an SPDX enum id")
        else:
            rep.bad("CITATION.cff declares neither `license:` nor `license-url:` — "
                    "machine-readable metadata must state the license")
        return
    if status == "empty":
        rep.bad("CITATION.cff `license:` is empty")
        return
    assert expr is not None
    _tokens = re.split(r"\s+(?:OR|AND)\s+", expr.strip().strip('"'), flags=re.I)
    if not all(t.strip().strip("()").lower() in _CFF_ENUM for t in _tokens):
        rep.bad(f"CITATION.cff license {expr!r} is not a valid CFF 1.2.0 value "
                "(strict SPDX enum; LicenseRef-*, free text and 'All Rights "
                "Reserved' are NOT members); omit `license:` and use "
                "`license-url:` instead")
        return
    fam_cff = license_family_from_spdx(expr)
    if fam_lic is None:
        rep.bad("LICENSE family unrecognized — add a recognizable marker "
                "(All Rights Reserved / Apache 2.0 / MIT / Creative Commons)")
        return
    if fam_cff == "multi":
        rep.bad(f"CITATION.cff license {expr!r} spans multiple families — "
                "ambiguous, resolve to a single SPDX expression")
        return
    if fam_cff is None or (fam_cff or "").startswith("?"):
        rep.bad(f"CITATION.cff license {expr!r} is not a CFF 1.2.0 enum value "
                "(LicenseRef-*/free text are not enum ids); use `license-url:` instead")
        return
    if fam_lic == fam_cff:
        rep.ok(f"license family consistent: {fam_lic} "
               f"(LICENSE == CITATION.cff {expr})")
    else:
        rep.bad(f"LICENSE METADATA CONFLICT: LICENSE says {fam_lic} but "
                f"CITATION.cff declares {expr} ({fam_cff})")


# ------------------------------------------------------------------- checks --
def check_required_files(root: Path, rep: Report) -> None:
    for rel in REQUIRED_FILES:
        p = root / rel
        if p.is_file() and p.stat().st_size > 0:
            rep.ok(f"required file present: {rel}")
        else:
            rep.bad(f"required file missing or empty: {rel}")
    sec = root / "SECURITY.md"
    if sec.is_file() and sec.stat().st_size > 0:
        rep.ok("SECURITY.md present and non-empty")
    else:
        rep.skip("SECURITY.md absent (recommended: add a security contact)")


def check_cff_keys(root: Path, rep: Report) -> None:
    cff = root / "CITATION.cff"
    if not cff.is_file():
        rep.skip("CITATION.cff absent (no citation metadata to check)")
        return
    text = cff.read_text(encoding="utf-8")
    for key in CFF_REQUIRED_KEYS:
        _val, status = cff_value_status(text, key)
        if status == "ok":
            rep.ok(f"CITATION.cff has non-empty {key}")
        elif status == "empty":
            rep.bad(f"CITATION.cff key is empty: {key}")
        else:
            rep.bad(f"CITATION.cff missing key: {key}")


def check_version_sync(root: Path, rep: Report) -> str | None:
    chlog = root / "CHANGELOG.md"
    if not chlog.is_file():
        rep.skip("CHANGELOG.md absent (no release history to check)")
        return None
    text = chlog.read_text(encoding="utf-8")
    newest = changelog_newest(text)
    if not newest:
        if "[unreleased]" in text.lower():
            rep.skip("CHANGELOG has no released version yet (only [Unreleased])")
        else:
            rep.bad("CHANGELOG.md has neither a released version heading nor [Unreleased]")
        return None
    cff = root / "CITATION.cff"
    if not cff.is_file():
        rep.ok(f"CHANGELOG newest released version = {newest}")
        return newest
    cff_ver = cff_value(cff.read_text(encoding="utf-8"), "version")
    if not cff_ver:
        rep.bad("CITATION.cff has no version value")
        return newest
    if norm_ver(cff_ver) == newest:
        rep.ok(f"CITATION.cff version {cff_ver} matches CHANGELOG newest {newest}")
    else:
        rep.bad(f"CITATION.cff version {cff_ver} != CHANGELOG newest {newest} "
                "(citation metadata is stale)")
    return newest


def check_tag(root: Path, rep: Report, version: str | None,
              repo: str | None, expect_tag: str | None) -> None:
    if version is None:
        rep.skip("tag consistency not applicable (no CHANGELOG version)")
        return
    if expect_tag:
        if norm_ver(expect_tag) == version:
            rep.ok(f"release tag {expect_tag} matches CHANGELOG newest {version}")
        else:
            rep.bad(f"release tag {expect_tag} != CHANGELOG newest {version} "
                    "(release/CHANGELOG out of step)")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    tags: list[str] | None = None
    src = ""
    if repo:
        try:
            tags = fetch_tags_via_api(repo, token)
            src = f"API {repo}"
        except Exception as e:  # noqa: BLE001
            rep.skip(f"tag fetch via API failed ({e}); trying local git")
    if tags is None:
        tags = fetch_tags_local(root)
        src = "local git tag --list"
    if not tags:
        rep.skip(f"no tags available from {src} — cannot verify tag for {version}")
        return
    if version_has_tag(tags, version):
        rep.ok(f"CHANGELOG newest {version} has a matching tag (source: {src})")
    else:
        rep.bad(f"CHANGELOG newest {version} has NO matching tag "
                f"(source: {src}; tags seen: {sorted(tags)[:12]})")


def fetch_tags_via_api(repo: str, token: str) -> list[str]:
    names: list[str] = []
    url = f"https://api.github.com/repos/{repo}/tags?per_page=100"
    while url:
        page, url = _api_get(url, token)
        names.extend(t.get("name", "") for t in page)
        if len(page) < 100:
            break
    return names


def _api_get(url: str, token: str) -> tuple[list, str | None]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        link = resp.headers.get("Link", "")
    nxt = None
    for part in link.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                nxt = m.group(1)
    return data, nxt


def fetch_tags_local(root: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "tag", "--list"], text=True, timeout=15
        )
        return [t for t in out.splitlines() if t.strip()]
    except Exception:
        return []


def run_checks(root: Path, repo: str | None = None,
               expect_tag: str | None = None) -> Report:
    rep = Report()
    print(f"repo-gate: checking {root}")
    check_required_files(root, rep)
    check_cff_keys(root, rep)
    version = check_version_sync(root, rep)
    check_tag(root, rep, version, repo, expect_tag)
    check_license_sync(root, rep)
    check_links(root, rep)
    print(f"repo-gate: {len(rep.passed)} passed, {len(rep.failed)} failed, "
          f"{len(rep.skipped)} skipped")
    return rep


# ---------------------------------------------------------------- selftest --

GOOD_FIXTURE: dict[str, str] = {
    "README.md": ("# T\n\nSee [LICENSE](LICENSE), [DETAILS](docs/details.md), "
                  "[TITLED](docs/details.md \"the details\"), "
                  "[REF][ref] and [WRAPPED](<docs/details.md>).\n\n[ref]: docs/details.md\n"),
    "LICENSE": "MIT License\n\nCopyright (c) 2026 Test\n",
    "SECURITY.md": "s\n",
    "CITATION.cff": (
        'cff-version: 1.2.0\ntitle: "T"\nversion: "0.2.1"\n'
        'date-released: "2026-09-28"\nlicense: MIT\n'
        'authors:\n  - family-names: Z\n'
    ),
    "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n## [0.2.1] — 2026-09-28\n- latest\n\n## [0.2.0] — old\n",
    "docs/details.md": "d\n",
}

# (label, mutated files, extra kwargs, expected-to-catch substring)
BROKEN_CASES = [
    ("missing README", {"README.md": None}, {}, "required file"),
    ("missing LICENSE", {"LICENSE": None}, {}, "required file"),
    ("stale CITATION version",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace('"0.2.1"', '"0.1.0"')},
     {}, "version"),
    ("dead link", {"README.md": "# T\n\nSee [GONE](docs/gone.md).\n"}, {},
     "dead relative link"),
    # --- B1..B6: the six bypass classes an independent red-team seat found ---
    ("B1 dead link whose path contains spaces",
     {"README.md": "# T\n\nSee [GONE](docs/gone file.md).\n"}, {},
     "dead relative link"),
    ("B2 dead link with a title suffix",
     {"README.md": "# T\n\nSee [GONE](docs/gone.md \"Title\").\n"}, {},
     "dead relative link"),
    ("B3 dead reference-style link",
     {"README.md": "# T\n\nSee [GONE][g].\n\n[g]: docs/gone.md\n"}, {},
     "dead relative link"),
    ("B4 link escapes the tree via ..",
     {"README.md": "# T\n\nSee [OUT](../outside.md).\n"}, {},
     "escapes the tree"),
    ("B5 newer CHANGELOG heading lower down (must take the max, not the first)",
     {"CHANGELOG.md": "# Changelog\n\n## [0.2.1] — 2026-09-28\n- old\n\n"
                       "## [9.9.9] — 2030-01-01\n- future\n"},
     {}, "NO matching tag"),
    ("B6 empty CITATION value",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace('"0.2.1"', '""')},
     {}, "empty"),
    ("changelog head has no tag",
     {"CHANGELOG.md": "# Changelog\n\n## [9.9.9] — 2030-01-01\n- future\n"}, {},
     "NO matching tag"),
    ("tag mismatches changelog head", {},
     {"expect_tag": "v9.9.9"}, "out of step"),
    ("citation missing a required key",
     {"CITATION.cff": 'cff-version: 1.2.0\ntitle: "T"\nversion: "0.2.1"\n'},
     {}, "missing key"),
    ("malformed changelog (no version, no Unreleased)",
     {"CHANGELOG.md": "# Changelog\n\nsome notes with no heading\n\n"}, {},
     "neither a released version heading nor"),
    ("license metadata conflict",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace("license: MIT",
                                                           "license: CC-BY-4.0")},
     {}, "LICENSE METADATA CONFLICT"),
    ("license field dropped from CITATION",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace("license: MIT\n", "")},
     {}, "neither `license:` nor `license-url:`"),
    ("LICENSE reserves all rights while CITATION grants CC-BY-4.0",
     {"LICENSE": "Copyright (c) 2026 X\n\nAll Rights Reserved.\n"},
     {}, "LICENSE METADATA CONFLICT"),
    # --- round-3: findings from the independent license/IP auditor ---
    # a CITATION `license:` value outside the CFF 1.2.0 enum (LicenseRef-*) is
    # invalid machine metadata, even though it is valid SPDX syntax
    ("CITATION license is a LicenseRef (not a CFF enum id)",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace(
         "license: MIT", "license: LicenseRef-AllRightsReserved")},
     {}, "CFF 1.2.0"),
    # an MIT LICENSE whose boilerplate says \"All rights reserved\" must be read
    # as MIT, so a CITATION claiming CC-BY-4.0 is a genuine conflict
    ("MIT LICENSE with ARR boilerplate must not be misread as ARR",
     {"LICENSE": "MIT License\n\nCopyright (c) 2026 X. All rights reserved.\n",
      "CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace("license: MIT",
                                                           "license: CC-BY-4.0")},
     {}, "LICENSE METADATA CONFLICT"),
    # an ambiguous OR expression spanning two families must be rejected
    ("ambiguous OR license expression",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace(
         "license: MIT", "license: MIT OR CC-BY-4.0")},
     {}, "spans multiple families"),
    # PROBE B (red-team round 2): a spacing-less heading `##[v0.2.1]` must still
    # be parsed — otherwise the version check fail-opens to SKIP
    ("spacing-less CHANGELOG heading must not fail-open to SKIP",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace('"0.2.1"', '"0.1.0"'),
      "CHANGELOG.md": "# Changelog\n\n##[v0.2.1] — 2026-09-28\n- latest\n"},
     {}, "version"),
]

# fixture variants that MUST pass (positive controls — a gate that always reds is
# as useless as one that never reds)
GOOD_VARIANTS = [
    ("license declared by URL only (no enum id) — the CFF-sanctioned fallback",
     {"CITATION.cff": GOOD_FIXTURE["CITATION.cff"].replace(
         "license: MIT\n", "license-url: \"https://example.invalid/LICENSE\"\n")}),
    ("MIT LICENSE with ARR boilerplate + CITATION MIT (must NOT false-red)",
     {"LICENSE": "MIT License\n\nCopyright (c) 2026 X. All rights reserved.\n"}),
    # PROBE C (red-team round 2): a version-looking heading inside a code fence
    # is documentation, not a release — treating it as one is a false red
    ("version-looking heading inside a code fence must NOT be read as a release",
     {"CHANGELOG.md": "# Changelog\n\n## [0.2.1] — 2026-09-28\n- latest\n\n"
                       "```\n## v9.9.9\n```\n"}),
]


def build_fixture(mutations: dict[str, str | None]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="repo-gate-fixture-"))
    for rel, content in GOOD_FIXTURE.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in mutations.items():
        p = tmp / rel
        if content is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                        "commit", "-q", "-m", "x"], cwd=tmp, check=True)
        subprocess.run(["git", "tag", "v0.2.1"], cwd=tmp, check=True)
    except Exception as e:  # noqa: BLE001
        print("  note: could not init git fixture:", e)
    return tmp


def selftest() -> int:
    print("selftest: proving the gate can fail (and can pass)")
    rc = 0

    good = build_fixture({})
    if not run_checks(good, repo=None, expect_tag="v0.2.1").failed:
        print("  PASS  good fixture passes")
    else:
        print("  FAIL  good fixture did NOT pass — checker is broken")
        rc = 1
    shutil.rmtree(good, ignore_errors=True)

    for label, mutations, kwargs, expect in BROKEN_CASES:
        bad = build_fixture(mutations)
        rep = run_checks(bad, repo=None, **kwargs)
        if any(expect in f for f in rep.failed):
            print(f"  PASS  broken fixture rejected: {label}")
        else:
            print(f"  FAIL  broken fixture NOT rejected ({label}) — gate is decorative")
            rc = 1
        shutil.rmtree(bad, ignore_errors=True)

    for label, mutations in GOOD_VARIANTS:
        okv = build_fixture(mutations)
        rep = run_checks(okv, repo=None, expect_tag="v0.2.1")
        if not rep.failed:
            print(f"  PASS  good variant accepted: {label}")
        else:
            print(f"  FAIL  good variant FALSELY rejected ({label}) — gate over-reds")
            rc = 1
        shutil.rmtree(okv, ignore_errors=True)

    # N6 (red-team round 2): escape-and-return `../<this-repo>/docs/details.md`
    # resolves back inside the tree yet is a dead link on GitHub. Needs the
    # dynamic fixture name, so it is built here rather than in BROKEN_CASES.
    n6 = build_fixture({})
    (n6 / "README.md").write_text(
        f"# T\n\nSee [SELF](../{n6.name}/docs/details.md).\n", encoding="utf-8")
    rep = run_checks(n6, repo=None, expect_tag="v0.2.1")
    if any("escapes the tree" in f for f in rep.failed):
        print("  PASS  broken fixture rejected: N6 escape-and-return via ../<repo>/")
    else:
        print("  FAIL  broken fixture NOT rejected (N6 escape-and-return) — gate is decorative")
        rc = 1
    shutil.rmtree(n6, ignore_errors=True)

    # case-only mismatch must red on every host (Windows/macOS are otherwise blind)
    cm = build_fixture({})
    (cm / "README.md").write_text("# T\n\nSee [U](DOCS/details.md).\n", encoding="utf-8")
    rep = run_checks(cm, repo=None, expect_tag="v0.2.1")
    if any("dead relative link" in f for f in rep.failed):
        print("  PASS  broken fixture rejected: case-only path mismatch")
    else:
        print("  FAIL  broken fixture NOT rejected (case-only mismatch) — host-dependent")
        rc = 1
    shutil.rmtree(cm, ignore_errors=True)

    # pure-function logic, no network
    if version_has_tag(["v0.2.1", "v0.2.0"], "0.2.1") and not version_has_tag(
            ["v1.5.0", "v1.4.0"], "1.6.0"):
        print("  PASS  version_has_tag logic correct (matches present, rejects absent)")
    else:
        print("  FAIL  version_has_tag logic wrong")
        rc = 1
    if changelog_newest("## [0.2.1]\n## [9.9.9]\n") == "9.9.9":
        print("  PASS  changelog_newest takes the max, not the first heading")
    else:
        print("  FAIL  changelog_newest does not take the max")
        rc = 1

    print("selftest:", "OK — gate can fail and can pass" if rc == 0 else "BROKEN")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--root", default=".")
    c.add_argument("--repo", default=None, help="OWNER/REPO for tag lookup via API")
    c.add_argument("--expect-tag", default=None, help="release-time guard ref")
    sub.add_parser("selftest")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()
    rep = run_checks(Path(args.root).resolve(), repo=args.repo, expect_tag=args.expect_tag)
    if rep.failed:
        print("repo-gate: RED — " + str(len(rep.failed)) + " defect(s)")
        return 1
    print("repo-gate: GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
