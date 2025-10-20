from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

EXTS = (".py", ".md", ".yaml", ".yml", ".json", ".txt")
EXCLUDE_PARTS = {
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "site-packages",
    "dist-info",
}


def _iter_repo_files() -> list[str]:
    try:
        out = subprocess.check_output(["git", "ls-files", "-z"], stderr=subprocess.DEVNULL)
    except Exception:
        out = b""
    if out:
        files: list[str] = []
        for entry in out.decode("utf-8").split("\x00"):
            if not entry:
                continue
            path = Path(entry)
            if any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            if path.suffix in EXTS:
                files.append(str(path))
        return files

    files: list[str] = []
    for root, dirs, filenames in os.walk("."):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_PARTS]
        for name in filenames:
            if name.endswith(EXTS):
                files.append(os.path.join(root, name))
    return files


def test_no_conflict_markers() -> None:
    bad: list[str] = []
    todo_marker = "TO" + "DO"
    fixme_marker = "FIX" + "ME"
    conflict_pattern = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
    for path in _iter_repo_files():
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        if conflict_pattern.search(text) or todo_marker in text or fixme_marker in text:
            bad.append(path)
    assert not bad, f"Unresolved markers/to-do items in: {bad}"
