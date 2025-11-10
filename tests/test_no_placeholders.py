"""Guardrails against placeholder code paths in critical modules."""

from __future__ import annotations

from pathlib import Path
import re

CRITICAL_DIRECTORIES = (
    Path("app/risk"),
    Path("app/router"),
    Path("app/broker"),
    Path("app/recon"),
)

PATTERNS = {
    "pass": re.compile(r"^\s*pass\b", re.MULTILINE),
    "NotImplementedError": re.compile(r"NotImplementedError"),
    "print(": re.compile(r"print\("),
    "eval(": re.compile(r"eval\("),
}


def _scan_file(path: Path) -> list[tuple[str, int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    findings: list[tuple[str, int, str]] = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            snippet = match.group(0).strip()
            findings.append((name, line, snippet))
    return findings


def test_no_placeholders_in_critical_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for directory in CRITICAL_DIRECTORIES:
        target = repo_root / directory
        for file_path in target.rglob("*.py"):
            findings = _scan_file(file_path)
            for name, line, snippet in findings:
                failures.append(
                    f"{file_path.relative_to(repo_root)}:{line} contains disallowed pattern '{name}' ({snippet})"
                )
    assert not failures, "\n".join(failures)
