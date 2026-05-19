#!/usr/bin/env python3
"""Build the filtered file list for public-repo publishing.

Reads tracked files (NUL-delimited from `git ls-files -z --full-name`), applies
an allowlist (from a Markdown file with `- path` bullets) and a deny list
(plain text, one rule per line, `#` comments), and emits the filtered set on
stdout.

Rule syntax (anchored at repo root):
  - exact file:   `path/to/file`            matches that one tracked path
  - dir prefix:   `path/to/dir/`            matches any file under that root
  - simple glob:  `*.log`, `tmp-*/`         `*` is wildcard, no `**` support

A path is included iff it matches at least one allow rule AND no deny rule.
The skill's default deny list is always applied on top of any project file.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SKILL_DEFAULT_DENIES = [
    ".planning/",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/design/",
    "docs/internal/",
    "rfc/",
    "adr/",
    "notes/",
]


def parse_text_list(path: Path) -> list[str]:
    rules: list[str] = []
    if not path or not path.exists():
        return rules
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rules.append(line)
    return rules


def parse_allowlist_md(path: Path) -> list[str]:
    rules: list[str] = []
    if not path.exists():
        return rules
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if s.startswith("- "):
            rules.append(s[2:].strip())
    return rules


def _glob_to_regex(rule: str) -> re.Pattern[str]:
    pat = "^" + re.escape(rule).replace(r"\*", "[^/]*") + "$"
    return re.compile(pat)


def matches(rule: str, path: str) -> bool:
    if rule.endswith("/"):
        return path.startswith(rule)
    if "*" in rule:
        rx = _glob_to_regex(rule)
        if rx.match(path):
            return True
        return rx.match(path.rsplit("/", 1)[-1]) is not None
    return path == rule


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow", required=True, help="path to allowlist .md")
    ap.add_argument("--deny", required=False, help="path to project deny file")
    ap.add_argument("--tracked-null", required=True, help="NUL-delimited git ls-files output")
    ap.add_argument("--null", action="store_true", help="emit NUL-delimited output")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Alias for default behavior — this script is dry-run by design "
            "(writes to stdout, no side effects)."
        ),
    )
    args = ap.parse_args()

    allow_rules = parse_allowlist_md(Path(args.allow))
    if not allow_rules:
        print(f"ERROR: allowlist {args.allow} is empty", file=sys.stderr)
        return 2

    deny_rules = list(SKILL_DEFAULT_DENIES)
    if args.deny:
        deny_rules.extend(parse_text_list(Path(args.deny)))

    raw = Path(args.tracked_null).read_bytes()
    files = [p.decode() for p in raw.split(b"\x00") if p]

    out: list[str] = []
    for f in files:
        if not any(matches(r, f) for r in allow_rules):
            continue
        if any(matches(r, f) for r in deny_rules):
            continue
        out.append(f)

    sep = b"\0" if args.null else b"\n"
    sys.stdout.buffer.write(sep.join(s.encode() for s in out))
    if not args.null and out:
        sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
