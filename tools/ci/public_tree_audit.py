"""Audit the public VocalizeAI tree candidate.

The private development tree can contain planning files before the public reset.
CI should audit the exported public candidate file list, not blindly assume the
current private checkout is already publishable.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_PATH_RULES = (
    ".planning/",
    "AGENTS.md",
    "CLAUDE.md",
    ".env",
    ".env.local",
    ".env.production",
    "infra/gpu-services/",
    "src/vocalize/stt/sensevoice.py",
    "src/vocalize/tts/cosyvoice.py",
    "docs/release/24h-stability-evidence.md",
    "docs/release/24h-stability-evidence-runs/",
)

CONTENT_BLOCKERS = (
    (re.compile(r"\bGPU_HOST\b"), "old GPU default deployment variable"),
    (re.compile(r"\bSenseVoice\b"), "old model-specific STT reference"),
    (re.compile(r"\bCosyVoice\b"), "old model-specific TTS reference"),
    (re.compile(r"\binfra/gpu-services\b"), "old GPU services path"),
    (re.compile(r"\bv1\.0\.0\b"), "old public release reference"),
    (re.compile(r"\b24h-stability\b"), "old release evidence reference"),
    (re.compile(r"\bPi orchestrator\b|\bRaspberry Pi\b"), "old Pi deployment reference"),
    (re.compile(r"\bTailscale\b"), "old tunnel deployment reference"),
)

SECRET_PATTERNS = (
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "possible OpenAI-compatible API key",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*"
            r"['\"]?(?!your_|example|placeholder|changeme|test|dummy|none|\$\{)"
            r"[A-Za-z0-9_/+=-]{12,}"
        ),
        "possible hard-coded secret",
    ),
)

TEXT_SUFFIXES = {
    ".bash",
    ".css",
    ".env",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

BINARY_OR_LOCK_SUFFIXES = {".DS_Store", ".lock", ".png", ".wav"}


@dataclass(frozen=True)
class Finding:
    path: str
    message: str
    line: int | None = None

    def format(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{location}: {self.message}"


def _normalize(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


def _matches_path_rule(path: str, rule: str) -> bool:
    if rule.endswith("/"):
        return path == rule[:-1] or path.startswith(rule)
    return path == rule


def _read_file_list(path: Path) -> list[str]:
    files = []
    for line in path.read_text().splitlines():
        normalized = _normalize(line)
        if normalized:
            files.append(normalized)
    return sorted(dict.fromkeys(files))


def _git_tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--full-name"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(
        p.decode()
        for p in result.stdout.split(b"\x00")
        if p and not p.decode().startswith(".git/")
    )


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        out.append(rel)
    return sorted(out)


def _is_text_candidate(path: str) -> bool:
    suffix = Path(path).suffix
    if suffix in BINARY_OR_LOCK_SUFFIXES:
        return False
    if Path(path).name == ".env.example":
        return True
    return suffix in TEXT_SUFFIXES


def _audit_paths(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        for rule in FORBIDDEN_PATH_RULES:
            if _matches_path_rule(path, rule):
                findings.append(Finding(path, f"forbidden public path: {rule}"))
    return findings


def _audit_contents(root: Path, paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if not _is_text_candidate(path):
            continue
        full_path = root / path
        if not full_path.exists() or full_path.is_dir():
            continue
        try:
            lines = full_path.read_text(errors="replace").splitlines()
        except OSError as exc:
            findings.append(Finding(path, f"could not read file: {exc}"))
            continue
        for number, line in enumerate(lines, 1):
            if not path.startswith("tests/"):
                for pattern, message in CONTENT_BLOCKERS:
                    if pattern.search(line):
                        findings.append(Finding(path, message, number))
            for pattern, message in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(path, message, number))
    return findings


def audit(root: Path, paths: list[str]) -> list[Finding]:
    normalized = sorted(dict.fromkeys(_normalize(path) for path in paths if _normalize(path)))
    return [*_audit_paths(normalized), *_audit_contents(root, normalized)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository or export root")
    parser.add_argument(
        "--file-list",
        help="newline-delimited public candidate files, relative to --root",
    )
    parser.add_argument(
        "--tracked",
        action="store_true",
        help="audit git tracked files under --root",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.file_list:
        paths = _read_file_list(Path(args.file_list))
    elif args.tracked:
        paths = _git_tracked_files(root)
    else:
        paths = _walk_files(root)

    findings = audit(root, paths)
    if findings:
        print("Public tree audit failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.format()}", file=sys.stderr)
        return 1

    print(f"Public tree audit passed: {len(paths)} files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
