"""Helpers for locating offline backtest reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

DEFAULT_REPORTS_DIR = Path("data/reports")
REPORTS_DIR_ENV = "BACKTEST_REPORTS_DIR"


def _reports_directory() -> Path:
    base = os.getenv(REPORTS_DIR_ENV)
    if base:
        return Path(base)
    return DEFAULT_REPORTS_DIR


def _list_report_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return sorted(
        directory.glob("backtest_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


@dataclass
class BacktestReport:
    json_path: str
    csv_path: str | None
    summary: Dict[str, Any]
    generated_at: str


def load_latest_summary() -> BacktestReport | None:
    directory = _reports_directory()
    for path in _list_report_files(directory):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        csv_path = path.with_suffix(".csv")
        generated_at = str(payload.get("generated_at") or "")
        if not generated_at:
            generated_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        return BacktestReport(
            json_path=str(path),
            csv_path=str(csv_path) if csv_path.exists() else None,
            summary=payload,
            generated_at=generated_at,
        )
    return None


__all__ = ["BacktestReport", "load_latest_summary"]

