from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

BASE_PATTERNS: dict[str, re.Pattern[str]] = {
    "eval": re.compile(r"eval\(", re.MULTILINE),
    "exec": re.compile(r"exec\(", re.MULTILINE),
    "pickle": re.compile(r"pickle\\.(loads|dumps)", re.MULTILINE),
    "yaml_load": re.compile(r"yaml\\.load\(", re.MULTILINE),
    "except_pass": re.compile(r"except[^\n]*:\s*\n\s+pass", re.MULTILINE),
}

TARGETS: tuple[Path, ...] = (
    Path("app/services"),
    Path("app/runtime_state_store.py"),
    Path("app/auto_hedge_daemon.py"),
    Path("app/routers"),
    Path("services"),
)


def _iter_files(base_paths: Iterable[Path]) -> Iterable[Path]:
    for path in base_paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.suffix == ".py" and path.exists():
            yield path


def _check_http_timeouts(text: str) -> list[tuple[int, str]]:
    issues: list[tuple[int, str]] = []
    for match in re.finditer(r"requests\\.(get|post|put|delete|request)\(", text):
        depth = 1
        index = match.end()
        while index < len(text) and depth > 0:
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        call_source = text[match.start():index]
        if "timeout" not in call_source:
            line = text.count("\n", 0, match.start()) + 1
            snippet = call_source.splitlines()[0]
            issues.append((line, snippet.strip()))
    return issues


def test_security_sweep_patterns() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for file_path in _iter_files((repo_root / target) for target in TARGETS):
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = file_path.relative_to(repo_root)
        for name, pattern in BASE_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                snippet = match.group(0).strip()
                failures.append(
                    f"{relative}:{line} contains disallowed pattern '{name}' ({snippet})"
                )
        for line, snippet in _check_http_timeouts(text):
            failures.append(
                f"{relative}:{line} performs a requests call without explicit timeout ({snippet})"
            )
    assert not failures, "\n".join(failures)
