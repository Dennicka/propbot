from __future__ import annotations

import os
import re


def test_no_conflict_markers() -> None:
    bad: list[str] = []
    for root, _, files in os.walk("."):
        for name in files:
            if name.endswith((".py", ".md", ".yaml", ".yml", ".json", ".txt")):
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
                if re.search(r"<<<<<<<|=======|>>>>>>>", text):
                    bad.append(path)
                if "TODO" in text or "FIXME" in text:
                    bad.append(path)
    assert not bad, f"Unresolved markers/TODOs in: {bad}"
