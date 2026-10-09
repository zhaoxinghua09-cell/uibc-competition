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
CHANGELOG_VER_RE = re.compile(r"^##\s+\[?\s*[vV]?(\d+\.\d+(?:\.\d+)?)\s*\]?")


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
    for line in text.splitlines():
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


def check_links(root: Path, rep: Report) -> None:
    root_res = root.resolve()
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
            resolved = (root / path_part).resolve()
            try:
                resolved.relative_to(root_res)
            except ValueError:
                seen_dead += 1
                rep.bad(f"{rel}: relative link escapes the tree -> {target}")
                continue
            if resolved.exists():
                seen_live += 1
            else:
                seen_dead += 1
                rep.bad(f"{rel}: dead relative link -> {target}")
    if seen_dead == 0:
        rep.ok(f"all relative links resolve ({seen_live} checked in "
               f"{', '.join(checked) or 'n/a'})")


# ------------------------------------------------------------------ license --
def license_family_from_text(text: str) -> str | None:
    """Classify a LICENSE document into a comparable family, or None."""
    t = text.lower()
    if "all rights reserved" in t or "保留所有权利" in text:
        return "arr"
    if "apache" in t and re.search(r"apache\s+license", t):
        return "apache-2.0"
    if ("creative commons" in t and "attribution" in t) or "cc-by-4.0" in t:
        return "cc-by-4.0"
    if re.search(r"\bmit license\b", t) or re.search(r"^\s*mit\s*$", t):
        return "mit"
    return None


def license_family_from_spdx(expr: str) -> str | None:
    s = expr.strip().strip('"').lower()
    if "allrightsreserved" in s or "all-rights-reserved" in s:
        return "arr"
    if "cc-by-4.0" in s or "cc-by-4" in s:
        return "cc-by-4.0"
    if "apache-2.0" in s:
        return "apache-2.0"
    if s == "mit" or "mit" in re.split(r"[\s()+&]", s):
        return "mit"
    return None


def check_license_sync(root: Path, rep: Report) -> None:
    lic = root / "LICENSE"
    cff = root / "CITATION.cff"
    if not lic.is_file() or not cff.is_file():
        rep.skip("license sync not applicable (LICENSE or CITATION.cff absent)")
        return
    fam_lic = license_family_from_text(lic.read_text(encoding="utf-8"))
    expr, status = cff_value_status(cff.read_text(encoding="utf-8"), "license")
    if status == "missing":
        rep.bad("CITATION.cff has no `license:` field — machine-readable "
                "metadata must state the license (or its absence) explicitly")
        return
    if status == "empty":
        rep.bad("CITATION.cff `license:` is empty")
        return
    fam_cff = license_family_from_spdx(expr or "")
    if fam_lic is None:
        rep.bad("LICENSE family unrecognized — add a recognizable marker "
                "(All Rights Reserved / Apache 2.0 / MIT / Creative Commons)")
        return
    if fam_cff is None:
        rep.bad(f"CITATION.cff license {expr!r} does not map to a known family")
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
     {}, "no `license:` field"),
    ("LICENSE reserves all rights while CITATION grants CC-BY-4.0",
     {"LICENSE": "Copyright (c) 2026 X\n\nAll Rights Reserved.\n"},
     {}, "LICENSE METADATA CONFLICT"),
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
