from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .arbitrage import Plan, SUPPORTED_SYMBOLS, build_plan, execute_plan
from .runtime import DryRunState, bump_counter, get_state, record_incident

LOGGER = logging.getLogger(__name__)
ARTIFACT_PATH = Path("artifacts/last_plan.json")


@dataclass
class DryRunMetrics:
    symbol: str
    direction: str | None
    spread_usdt: float
    spread_bps: float
    total_fees_usdt: float
    total_fees_bps: float
    est_pnl_usdt: float
    est_pnl_bps: float

    def as_dict(self) -> Dict[str, float | str | None]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "spread_usdt": self.spread_usdt,
            "spread_bps": self.spread_bps,
            "total_fees_usdt": self.total_fees_usdt,
            "total_fees_bps": self.total_fees_bps,
            "est_pnl_usdt": self.est_pnl_usdt,
            "est_pnl_bps": self.est_pnl_bps,
        }


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    symbol_upper = symbol.upper()
    if symbol_upper in SUPPORTED_SYMBOLS:
        return symbol_upper
    if symbol_upper.endswith("-USDT-SWAP"):
        return symbol_upper.replace("-USDT-SWAP", "USDT")
    return None


def _plan_direction(plan: Plan) -> str | None:
    if len(plan.legs) < 2:
        return None
    return f"{plan.legs[0].exchange}->{plan.legs[1].exchange}"


def compute_metrics(plan: Plan) -> DryRunMetrics:
    total_fees = sum(leg.fee_usdt for leg in plan.legs)
    notional = plan.notional if plan.notional else 0.0
    spread_usdt = plan.est_pnl_usdt + total_fees
    spread_bps = (spread_usdt / notional) * 10_000 if notional else 0.0
    fee_bps = (total_fees / notional) * 10_000 if notional else 0.0
    return DryRunMetrics(
        symbol=plan.symbol,
        direction=_plan_direction(plan),
        spread_usdt=spread_usdt,
        spread_bps=spread_bps,
        total_fees_usdt=total_fees,
        total_fees_bps=fee_bps,
        est_pnl_usdt=plan.est_pnl_usdt,
        est_pnl_bps=plan.est_pnl_bps,
    )


def _ensure_dryrun_state() -> DryRunState:
    state = get_state()
    if state.dryrun is None:
        state.dryrun = DryRunState()
    return state.dryrun


def select_cycle_symbol() -> str:
    state = get_state()
    override = _normalise_symbol(state.control.loop_pair)
    if override:
        return override
    cfg = state.config.data.derivatives
    if cfg and cfg.arbitrage and cfg.arbitrage.pairs:
        for pair in cfg.arbitrage.pairs:
            symbol = _normalise_symbol(pair.long.symbol)
            if symbol:
                return symbol
            symbol = _normalise_symbol(pair.short.symbol)
            if symbol:
                return symbol
    return "BTCUSDT"


class DryRunScheduler:
    def __init__(self, artifact_path: Path | None = None) -> None:
        self.artifact_path = artifact_path or ARTIFACT_PATH
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)

    def run_once(self) -> Dict[str, Any]:
        ts = _ts()
        state = get_state()
        state.control.dry_run = True
        dryrun_state = _ensure_dryrun_state()
        symbol = select_cycle_symbol()
        notional = state.control.order_notional_usdt
        slippage = state.control.max_slippage_bps
        payload: Dict[str, Any] = {"ok": False, "ts": ts}
        try:
            plan = build_plan(symbol, notional, slippage)
            execution = execute_plan(plan)
            metrics = compute_metrics(plan)
            meets_threshold = metrics.spread_bps >= dryrun_state.min_spread_bps
            plan_dict = plan.as_dict()
            exec_dict = execution.as_dict()
            payload.update(
                {
                    "ok": True,
                    "symbol": symbol,
                    "plan": plan_dict,
                    "execution": exec_dict,
                    "metrics": metrics.as_dict(),
                    "viable": plan.viable,
                    "meets_threshold": meets_threshold,
                }
            )
            dryrun_state.last_cycle_ts = ts
            dryrun_state.last_plan = plan_dict
            dryrun_state.last_execution = exec_dict
            dryrun_state.last_error = None
            dryrun_state.last_spread_bps = metrics.spread_bps
            dryrun_state.last_spread_usdt = metrics.spread_usdt
            dryrun_state.last_fees_usdt = metrics.total_fees_usdt
            dryrun_state.cycles_completed += 1
            bump_counter("dry_run_cycles", 1)
            LOGGER.info(
                "dry-run cycle complete",
                extra={
                    "symbol": symbol,
                    "viable": plan.viable,
                    "spread_bps": metrics.spread_bps,
                    "pnl_usdt": metrics.est_pnl_usdt,
                    "direction": metrics.direction,
                },
            )
            artifact_payload = {
                "ts": ts,
                "result": payload,
            }
            self.artifact_path.write_text(
                json.dumps(artifact_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.exception("dry-run cycle failed")
            dryrun_state.last_cycle_ts = ts
            dryrun_state.last_error = str(exc)
            record_incident("dryrun", {"error": str(exc)})
            payload["error"] = str(exc)
        return payload

    def loop(self) -> None:
        while True:
            self.run_once()
            dryrun_state = _ensure_dryrun_state()
            interval = max(1, int(dryrun_state.poll_interval_sec))
            time.sleep(interval)


def run_once() -> Dict[str, Any]:
    scheduler = DryRunScheduler()
    return scheduler.run_once()


def run_loop() -> None:
    scheduler = DryRunScheduler()
    scheduler.loop()
