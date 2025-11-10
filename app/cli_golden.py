"""CLI helpers for managing golden replay baselines."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .golden.logger import normalise_events

_CURRENT_RUN_PATH = Path("data/golden/current_run.jsonl")
_BASELINE_PATH = Path("data/golden/baseline.jsonl")


@dataclass(slots=True)
class GoldenCheckResult:
    matched: bool
    message: str = ""


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if isinstance(payload, dict):
                records.append(payload)
            else:
                raise ValueError(f"invalid record type in {path}: {type(payload)!r}")
    return records


def snapshot_baseline(
    current_path: Path = _CURRENT_RUN_PATH,
    baseline_path: Path = _BASELINE_PATH,
) -> Path:
    current_path = Path(current_path)
    baseline_path = Path(baseline_path)
    if not current_path.exists():
        raise FileNotFoundError(f"current run log missing: {current_path}")
    records = _load_jsonl(current_path)
    normalised = normalise_events(records)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    with baseline_path.open("w", encoding="utf-8") as handle:
        for entry in normalised:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return baseline_path


def _render_diff(
    baseline: Sequence[dict[str, object]], current: Sequence[dict[str, object]]
) -> str:
    import difflib

    baseline_lines = [json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in baseline]
    current_lines = [json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in current]
    diff_lines = list(
        difflib.unified_diff(
            baseline_lines,
            current_lines,
            fromfile="baseline",
            tofile="current",
            lineterm="",
        )
    )
    if not diff_lines:
        return "golden replay mismatch"
    return "\n".join(diff_lines)


def run_check(
    current_path: Path = _CURRENT_RUN_PATH,
    baseline_path: Path = _BASELINE_PATH,
) -> GoldenCheckResult:
    current_path = Path(current_path)
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        return GoldenCheckResult(True, "baseline missing — skipping golden replay check")
    current_records = normalise_events(_load_jsonl(current_path))
    baseline_records = normalise_events(_load_jsonl(baseline_path))
    if current_records == baseline_records:
        return GoldenCheckResult(True, "golden replay check passed")
    diff = _render_diff(baseline_records, current_records)
    return GoldenCheckResult(False, diff)


def _cmd_snapshot_baseline(_: argparse.Namespace) -> int:
    try:
        snapshot_baseline()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_check(_: argparse.Namespace) -> int:
    result = run_check()
    if result.message:
        print(result.message)
    return 0 if result.matched else 1


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli_golden")
    sub = parser.add_subparsers(dest="command", required=True)
    sub_snapshot = sub.add_parser("snapshot-baseline", help="capture the current run as baseline")
    sub_snapshot.set_defaults(func=_cmd_snapshot_baseline)
    sub_check = sub.add_parser("check", help="compare current run with baseline")
    sub_check.set_defaults(func=_cmd_check)
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 1
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
