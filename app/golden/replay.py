"""Offline golden replay harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Sequence

from ..services.arbitrage import ExecutionReport, plan_from_payload
from ..services.runtime import HoldActiveError
from .recorder import golden_record_enabled, golden_replay_enabled

LOGGER = logging.getLogger(__name__)

_TRACE_PATH = Path("data/golden_trace.log")


@dataclass(frozen=True)
class ReplayMismatch:
    key: str
    details: Dict[str, Any]


@dataclass(frozen=True)
class ReplaySummary:
    total_events: int
    total_groups: int
    mismatches: List[ReplayMismatch]

    @property
    def ok(self) -> bool:
        return not self.mismatches


async def _default_executor(plan) -> ExecutionReport:
    from ..services.arbitrage import execute_plan_async

    return await execute_plan_async(plan, allow_safe_mode=True)


def _load_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                LOGGER.debug("golden replay skipping malformed line", extra={"line": stripped})
                continue
            if isinstance(event, Mapping):
                events.append(dict(event))
    return events


def _group_events(events: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        key = str(event.get("runtime_state") or "{}")
        grouped.setdefault(key, []).append(dict(event))
    return grouped


def _order_signature(order: Mapping[str, Any]) -> Dict[str, Any]:
    venue = str(order.get("venue") or "unknown")
    side = str(order.get("side") or "unknown")
    qty_raw = order.get("qty")
    if qty_raw is None:
        qty_raw = order.get("quantity")
    if qty_raw is None:
        qty_raw = order.get("size")
    try:
        size = float(qty_raw)
    except (TypeError, ValueError):
        size = 0.0
    symbol_value = order.get("symbol")
    if symbol_value is None:
        symbol_value = ""
    return {"venue": venue, "side": side, "size": size, "symbol": str(symbol_value)}


def _normalise_orders(orders: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalised = [_order_signature(order) for order in orders]
    normalised.sort(key=lambda entry: (entry["venue"], entry["side"], entry["size"]))
    return normalised


async def replay_trace(
    path: Path | None = None,
    *,
    executor: Callable[[Any], Awaitable[ExecutionReport]] | None = None,
) -> ReplaySummary:
    trace_path = path or _TRACE_PATH
    events = _load_events(trace_path)
    grouped = _group_events(events)
    mismatches: List[ReplayMismatch] = []
    runner = executor or _default_executor

    for key, entries in grouped.items():
        runtime_state: Dict[str, Any]
        try:
            runtime_state = json.loads(key)
        except json.JSONDecodeError:
            LOGGER.debug("golden replay runtime state parse failed", extra={"key": key})
            runtime_state = {}
        plan_payload = runtime_state.get("plan") if isinstance(runtime_state, Mapping) else None
        if not isinstance(plan_payload, Mapping):
            LOGGER.debug("golden replay missing plan payload", extra={"key": key})
            continue
        plan = plan_from_payload(plan_payload)
        if "reason" in plan_payload:
            plan.reason = plan_payload.get("reason")

        expected_orders = _normalise_orders(entries)
        expected_hold = bool(entries[0].get("hold"))
        expected_reason = entries[0].get("reason")
        expected_dry_run = bool(entries[0].get("dry_run"))

        actual_orders: List[Dict[str, Any]]
        actual_hold = False
        actual_reason: str | None = None
        actual_dry_run = False
        try:
            report = await runner(plan)
        except HoldActiveError as exc:
            actual_orders = []
            actual_hold = True
            actual_reason = getattr(exc, "reason", "hold_active")
            actual_dry_run = True
        else:
            actual_orders = _normalise_orders(report.orders)
            actual_reason = getattr(report, "state", None)
            actual_dry_run = bool(getattr(report, "dry_run", False) or getattr(report, "simulated", False))

        mismatch_reasons: Dict[str, Any] = {}
        if actual_orders != expected_orders:
            mismatch_reasons["orders"] = {"expected": expected_orders, "actual": actual_orders}
        if actual_hold != expected_hold:
            mismatch_reasons["hold"] = {"expected": expected_hold, "actual": actual_hold}
        if actual_dry_run != expected_dry_run:
            mismatch_reasons["dry_run"] = {"expected": expected_dry_run, "actual": actual_dry_run}
        if actual_reason != expected_reason:
            mismatch_reasons["reason"] = {"expected": expected_reason, "actual": actual_reason}
        if mismatch_reasons:
            detail = {
                "symbol": plan.symbol,
                "runtime_state": runtime_state,
                "mismatch": mismatch_reasons,
            }
            LOGGER.error("golden mismatch", extra={"event": "golden_mismatch", "details": detail})
            mismatches.append(ReplayMismatch(key=key, details=detail))

    return ReplaySummary(total_events=len(events), total_groups=len(grouped), mismatches=mismatches)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden replay harness")
    parser.add_argument("--path", type=Path, default=_TRACE_PATH, help="Path to golden trace log")
    args = parser.parse_args(argv)

    if golden_record_enabled():
        LOGGER.warning("golden record is enabled; disable it for replay runs")
    if not golden_replay_enabled():
        LOGGER.info("GOLDEN_REPLAY_ENABLED is not set; proceeding in best-effort mode")

    summary = asyncio.run(replay_trace(path=args.path))
    if not summary.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
