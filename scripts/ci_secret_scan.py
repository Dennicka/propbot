#!/usr/bin/env python3
"""Lightweight secret scanner for CI enforcement."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

PATTERN_DEFINITIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "high_entropy_assignment",
        re.compile(
            r"(?i)(api[_-]?key|secret|token|passphrase)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{32,}"
        ),
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN(?:[ A-Z]+)? PRIVATE KEY-----"),
    ),
)

ALLOWLIST = {
    Path("docs/SECURITY.md"),
}


def _repo_files(root: Path) -> Iterable[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        candidate = root / line
        if candidate.is_file():
            yield candidate


def _scan_file(path: Path) -> list[tuple[str, int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    findings: list[tuple[str, int, str]] = []
    for label, pattern in PATTERN_DEFINITIONS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            snippet = match.group(0).strip()[:120]
            findings.append((label, line, snippet))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for file_path in _repo_files(root):
        relative = file_path.relative_to(root)
        if relative in ALLOWLIST:
            continue
        findings = _scan_file(file_path)
        for label, line, snippet in findings:
            failures.append(f"{relative}:{line} -> {label}: {snippet}")
    if failures:
        print("Potential secrets detected:\n" + "\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
