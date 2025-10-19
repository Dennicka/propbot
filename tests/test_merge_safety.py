from __future__ import annotations

import os
import re


def test_no_conflict_markers() -> None:
    bad: list[str] = []
    todo_marker = "TO" + "DO"
    fixme_marker = "FIX" + "ME"
    conflict_pattern = r"<<<<<" + "<<" + r"|===" + "===" + r"|>>>>>" + ">>"
    for root, _, files in os.walk("."):
        for name in files:
            if name.endswith((".py", ".md", ".yaml", ".yml", ".json", ".txt")):
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
                if re.search(conflict_pattern, text):
                    bad.append(path)
                if todo_marker in text or fixme_marker in text:
                    bad.append(path)
    assert not bad, f"Unresolved markers/to-do items in: {bad}"
