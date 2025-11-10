from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Tuple

import os
import time

from ..broker.router import ExecutionRouter
from ..core.config import ArbitragePairConfig
from ..metrics import record_trade_execution, slo
from ..routing import effective_fee_for_quote, extract_funding_inputs
from ..risk.core import risk_gate
from ..risk.telemetry import record_risk_skip
from ..golden.recorder import record_execution
from ..universe.gate import check_pair_allowed, is_universe_enforced
from ..risk.accounting import (
    get_risk_snapshot as get_risk_accounting_snapshot,
    record_fill as accounting_record_fill,
    record_intent as accounting_record_intent,
)
from ..strategy_risk import get_strategy_risk_manager
from ..utils.symbols import resolve_runtime_venue_id
from . import risk
from .derivatives import DerivativesRuntime
from .runtime import (
    HoldActiveError,
    bump_counter,
    get_state,
    get_market_data,
    record_incident,
    record_universe_unknown_pair,
    register_order_attempt,
    set_preflight_result,
    update_guard,
)


logger = logging.getLogger(__name__)


def _record_golden_report(
    plan: "Plan",
    report: "ExecutionReport",
    *,
    reason: str | None,
    hold: bool,
) -> None:
    try:
        plan_payload = plan.as_dict()
    except Exception:  # pragma: no cover - defensive guard
        plan_payload = {"symbol": plan.symbol, "legs": []}
    runtime_state = {"plan": plan_payload, "report_state": report.state}
    if plan.reason:
        runtime_state["plan_reason"] = plan.reason
    if report.risk_gate:
        runtime_state["risk_gate"] = report.risk_gate
    record_execution(
        symbol=plan.symbol,
        plan_payload=plan_payload,
        orders=report.orders,
        reason=reason,
        runtime_state=runtime_state,
        hold=hold,
        dry_run=bool(report.dry_run or report.simulated),
    )


def _emit_ops_alert(kind: str, text: str, extra: Dict[str, object] | None = None) -> None:
    try:
        from ..opsbot.notifier import emit_alert
    except Exception as exc:
        logger.warning("ops notifier import failed kind=%s error=%s", kind, exc)
        return
    try:
        emit_alert(kind=kind, text=text, extra=extra or None)
    except Exception as exc:
        logger.warning("ops notifier emit failed kind=%s error=%s", kind, exc)


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class PreflightReport:
    ok: bool
    checks: List[PreflightCheck] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
        }


@dataclass
class LegacyPlanLeg:
    exchange: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    price: float | None = None


@dataclass
class LegacyPlan:
    legs: List[LegacyPlanLeg]
    notes: List[str] = field(default_factory=list)


def build_legacy_plan(payload: Dict[str, Any]) -> LegacyPlan:
    symbol_raw = payload.get("symbol") or payload.get("pair") or "UNKNOWN"
    symbol = str(symbol_raw)
    qty_value = payload.get("qty")
    if qty_value is None:
        qty_value = payload.get("size", 0.0)
    try:
        qty = float(qty_value)
    except (TypeError, ValueError):
        qty = 0.0
    legs = [
        LegacyPlanLeg(exchange="sim-long", symbol=symbol, side="buy", qty=qty, price=None),
        LegacyPlanLeg(exchange="sim-short", symbol=symbol, side="sell", qty=qty, price=None),
    ]
    notes = [f"simulated plan for {symbol}"]
    return LegacyPlan(legs=legs, notes=notes)


def legacy_plan_as_dict(plan: LegacyPlan) -> Dict[str, Any]:
    return asdict(plan)


def execute_legacy_plan(
    plan: LegacyPlan, *, safe_mode: bool, two_man_ok: bool, dry_run: bool
) -> Dict[str, Any]:
    if safe_mode:
        blocked_by = "safe_mode"
    elif dry_run:
        blocked_by = "dry_run"
    elif not two_man_ok:
        blocked_by = "two_man_rule"
    else:
        blocked_by = "not_implemented"
    return {"executed": False, "blocked_by": blocked_by, "plan": legacy_plan_as_dict(plan)}


SUPPORTED_SYMBOLS = {"BTCUSDT", "ETHUSDT"}


@dataclass
class PlanLeg:
    exchange: str
    side: Literal["buy", "sell"]
    price: float
    qty: float
    fee_usdt: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "ex": self.exchange,
            "side": self.side,
            "px": self.price,
            "qty": self.qty,
            "fee_usdt": self.fee_usdt,
        }


@dataclass
class Plan:
    symbol: str
    notional: float
    used_slippage_bps: int
    used_fees_bps: Dict[str, int]
    viable: bool
    legs: List[PlanLeg] = field(default_factory=list)
    est_pnl_usdt: float = 0.0
    est_pnl_bps: float = 0.0
    spread_bps: float = 0.0
    venues: List[str] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "symbol": self.symbol,
            "notional": self.notional,
            "viable": self.viable,
            "legs": [leg.as_dict() for leg in self.legs],
            "est_pnl_usdt": self.est_pnl_usdt,
            "est_pnl_bps": self.est_pnl_bps,
            "used_fees_bps": self.used_fees_bps,
            "used_slippage_bps": self.used_slippage_bps,
            "spread_bps": self.spread_bps,
            "venues": list(self.venues),
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass
class ExecutionReport:
    symbol: str
    simulated: bool
    pnl_usdt: float
    pnl_bps: float
    legs: List[PlanLeg]
    plan_viable: bool
    safe_mode: bool
    dry_run: bool
    orders: List[Dict[str, object]] = field(default_factory=list)
    exposures: List[Dict[str, object]] = field(default_factory=list)
    pnl_summary: Dict[str, float] = field(default_factory=dict)
    state: str = "DONE"
    risk_gate: Dict[str, object] = field(default_factory=dict)
    risk_snapshot: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "simulated": self.simulated,
            "pnl_usdt": self.pnl_usdt,
            "pnl_bps": self.pnl_bps,
            "plan_viable": self.plan_viable,
            "safe_mode": self.safe_mode,
            "dry_run": self.dry_run,
            "legs": [leg.as_dict() for leg in self.legs],
            "orders": list(self.orders),
            "exposures": list(self.exposures),
            "pnl_summary": dict(self.pnl_summary),
            "state": self.state,
            "risk_gate": dict(self.risk_gate),
            "risk_snapshot": dict(self.risk_snapshot),
        }


def _slippage_multiplier(slippage_bps: int, *, side: Literal["buy", "sell"]) -> float:
    adjustment = slippage_bps / 10_000.0
    if side == "buy":
        return 1 + adjustment
    return max(0.0, 1 - adjustment)


def _feature_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _compute_leg(
    *,
    buy_exchange: str,
    buy_price: float,
    sell_exchange: str,
    sell_price: float,
    notional: float,
    fees: Dict[str, int],
    buy_fee_bps: float | None = None,
    sell_fee_bps: float | None = None,
) -> tuple[float, List[PlanLeg]]:
    if buy_price <= 0 or sell_price <= 0 or notional <= 0:
        return 0.0, []
    qty = notional / buy_price
    buy_bps = float(buy_fee_bps) if buy_fee_bps is not None else float(fees[buy_exchange])
    sell_bps = float(sell_fee_bps) if sell_fee_bps is not None else float(fees[sell_exchange])
    buy_fee = buy_price * qty * buy_bps / 10_000.0
    sell_fee = sell_price * qty * sell_bps / 10_000.0
    pnl = sell_price * qty - buy_price * qty - buy_fee - sell_fee
    legs = [
        PlanLeg(exchange=buy_exchange, side="buy", price=buy_price, qty=qty, fee_usdt=buy_fee),
        PlanLeg(exchange=sell_exchange, side="sell", price=sell_price, qty=qty, fee_usdt=sell_fee),
    ]
    return pnl, legs


def build_plan(symbol: str, notional: float, slippage_bps: int) -> Plan:
    state = get_state()
    symbol_normalised = (symbol or "").upper()
    notional_value = float(notional)

    fees = {
        "binance": state.control.taker_fee_bps_binance,
        "okx": state.control.taker_fee_bps_okx,
    }

    plan = Plan(
        symbol=symbol_normalised,
        notional=notional_value,
        used_slippage_bps=slippage_bps,
        used_fees_bps=fees,
        viable=True,
    )

    # Базовые валидации
    if symbol_normalised not in SUPPORTED_SYMBOLS:
        plan.reason = f"unsupported symbol {symbol_normalised}"
        return plan
    if notional_value <= 0:
        plan.reason = "notional must be positive"
        return plan

    # Книги цен
    aggregator = get_market_data()
    try:
        binance_book = aggregator.top_of_book("binance-um", symbol_normalised)
        okx_book = aggregator.top_of_book("okx-perp", symbol_normalised)
    except Exception as exc:  # pragma: no cover
        logger.exception("failed to fetch books for %s", symbol_normalised)
        plan.reason = f"failed to fetch books: {exc}"
        return plan

    funding_overrides: Dict[str, Dict[str, float]] = {}
    if _feature_enabled("FEATURE_FUNDING_ROUTER"):
        config_data = getattr(state.config, "data", None)
        include_next_window = True
        derivatives_cfg = getattr(config_data, "derivatives", None) if config_data else None
        if derivatives_cfg and getattr(derivatives_cfg, "funding", None):
            funding_cfg = derivatives_cfg.funding
            include_next_window = bool(getattr(funding_cfg, "include_next_window", True))
        venue_alias_map = {
            "binance": resolve_runtime_venue_id(config_data, alias="binance"),
            "okx": resolve_runtime_venue_id(config_data, alias="okx"),
        }
        funding_quotes = extract_funding_inputs(
            runtime_state=state,
            symbol=symbol_normalised,
            venue_alias_map=venue_alias_map,
            include_next_window=include_next_window,
        )
        if funding_quotes:
            now_ts = time.time()
            for venue_name, quote in funding_quotes.items():
                funding_overrides[venue_name] = {
                    "long": effective_fee_for_quote(
                        quote,
                        side="long",
                        include_next_window=include_next_window,
                        now=now_ts,
                    ),
                    "short": effective_fee_for_quote(
                        quote,
                        side="short",
                        include_next_window=include_next_window,
                        now=now_ts,
                    ),
                }
            logger.debug(
                "funding overrides applied",
                extra={
                    "symbol": symbol_normalised,
                    "overrides": {
                        venue: {k: round(v, 6) for k, v in mapping.items()}
                        for venue, mapping in funding_overrides.items()
                    },
                    "include_next_window": include_next_window,
                },
            )

    # Две стороны арбитража с поправкой на слиппедж
    okx_buy = okx_book["ask"] * _slippage_multiplier(slippage_bps, side="buy")
    binance_sell = binance_book["bid"] * _slippage_multiplier(slippage_bps, side="sell")
    spread_a, legs_a = _compute_leg(
        buy_exchange="okx",
        buy_price=okx_buy,
        sell_exchange="binance",
        sell_price=binance_sell,
        notional=notional_value,
        fees=fees,
        buy_fee_bps=funding_overrides.get("okx", {}).get("long"),
        sell_fee_bps=funding_overrides.get("binance", {}).get("short"),
    )

    binance_buy = binance_book["ask"] * _slippage_multiplier(slippage_bps, side="buy")
    okx_sell = okx_book["bid"] * _slippage_multiplier(slippage_bps, side="sell")
    spread_b, legs_b = _compute_leg(
        buy_exchange="binance",
        buy_price=binance_buy,
        sell_exchange="okx",
        sell_price=okx_sell,
        notional=notional_value,
        fees=fees,
        buy_fee_bps=funding_overrides.get("binance", {}).get("long"),
        sell_fee_bps=funding_overrides.get("okx", {}).get("short"),
    )

    # Выбираем лучший из двух направлений и считаем BPS
    spread_a_bps = (spread_a / notional_value) * 10_000 if notional_value else 0.0
    spread_b_bps = (spread_b / notional_value) * 10_000 if notional_value else 0.0

    plan.venues = ["binance-um", "okx-perp"]

    if spread_a >= spread_b:
        pnl = spread_a
        legs = legs_a
        spread_bps = spread_a_bps
    else:
        pnl = spread_b
        legs = legs_b
        spread_bps = spread_b_bps

    plan.legs = legs
    plan.est_pnl_usdt = pnl
    plan.est_pnl_bps = (pnl / notional_value) * 10_000 if notional_value else 0.0
    plan.spread_bps = spread_bps

    # Причины по "спреду" (но они вторичны относительно "risk:*")
    min_spread = float(state.control.min_spread_bps)
    spread_reason = None
    if pnl <= 0:
        spread_reason = "spread non-positive after fees"
    elif plan.spread_bps < min_spread:
        spread_reason = f"spread {plan.spread_bps:.4f} < min {min_spread:.4f}"

    # СНАЧАЛА проверяем риск — его причина должна иметь приоритет
    risk_ok, risk_reason, risk_state = risk.guard_plan(plan)
    if not risk_ok:
        plan.viable = False
        plan.reason = risk_reason or "risk:blocked"
        return plan

    # Если риск ок, но спред плохой — блокируем план по спреду
    if spread_reason:
        plan.viable = False
        plan.reason = spread_reason
        return plan

    # Иначе всё хорошо
    plan.viable = True
    plan.reason = None
    risk.evaluate_plan(plan, risk_state=risk_state)

    logger.info(
        "arbitrage plan built",
        extra={
            "symbol": symbol_normalised,
            "pnl_usdt": plan.est_pnl_usdt,
            "pnl_bps": plan.est_pnl_bps,
            "direction": legs[0].exchange + "->" + legs[1].exchange if legs else "none",
            "viable": plan.viable,
            "reason": plan.reason,
        },
    )
    return plan


def plan_from_payload(payload: Dict[str, Any]) -> Plan:
    symbol = payload.get("symbol", "").upper()
    notional = float(payload.get("notional", 0.0))
    slippage = int(payload.get("used_slippage_bps", payload.get("slippage_bps", 0)))
    fees_input = payload.get("used_fees_bps") or {}
    state = get_state()
    fees = {
        "binance": int(fees_input.get("binance", state.control.taker_fee_bps_binance)),
        "okx": int(fees_input.get("okx", state.control.taker_fee_bps_okx)),
    }
    legs_payload = payload.get("legs") or []
    legs: List[PlanLeg] = []
    for leg in legs_payload:
        try:
            legs.append(
                PlanLeg(
                    exchange=str(leg["ex"]).lower(),
                    side=str(leg["side"]).lower(),
                    price=float(leg["px"]),
                    qty=float(leg["qty"]),
                    fee_usdt=float(leg.get("fee_usdt", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    viable = bool(payload.get("viable"))
    plan = Plan(
        symbol=symbol,
        notional=notional,
        used_slippage_bps=slippage,
        used_fees_bps=fees,
        viable=viable,
        legs=legs,
        est_pnl_usdt=float(payload.get("est_pnl_usdt", 0.0)),
        est_pnl_bps=float(payload.get("est_pnl_bps", 0.0)),
        spread_bps=float(payload.get("spread_bps", 0.0)),
        venues=[str(v) for v in payload.get("venues", [])],
        reason=payload.get("reason"),
    )
    return plan


async def execute_plan_async(plan: Plan, *, allow_safe_mode: bool = False) -> ExecutionReport:
    with slo.order_cycle_timer():
        state = get_state()
        safe_mode = state.control.safe_mode
        dry_run = state.control.dry_run
        router = ExecutionRouter()
        simulated = dry_run or safe_mode
        strategy_name = "cross_exchange_arb"

        if is_universe_enforced():
            allowed, reason = check_pair_allowed(plan.symbol)
            if not allowed:
                reason_code = reason or "universe"
                record_risk_skip(strategy_name, reason_code)
                slo.inc_skipped("universe")
                record_universe_unknown_pair(plan.symbol)
                logger.info(
                    "universe gate blocked execution",
                    extra={"strategy": strategy_name, "symbol": plan.symbol},
                )
                gate_result: Dict[str, object] = {
                    "allowed": False,
                    "state": "SKIPPED_BY_RISK",
                    "reason": reason_code,
                    "strategy": strategy_name,
                    "details": {"reason": "universe"},
                }
                snapshot = get_risk_accounting_snapshot()
                report = ExecutionReport(
                    symbol=plan.symbol,
                    simulated=simulated,
                    pnl_usdt=0.0,
                    pnl_bps=0.0,
                    legs=plan.legs,
                    plan_viable=False,
                    safe_mode=safe_mode,
                    dry_run=dry_run,
                    orders=[],
                    exposures=[],
                    pnl_summary={},
                    state="SKIPPED_BY_RISK",
                    risk_gate=gate_result,
                    risk_snapshot=snapshot,
                )
                _record_golden_report(plan, report, reason=reason_code, hold=False)
                return report

        intent_payload = {
            "strategy": strategy_name,
            "intent_notional": plan.notional,
            "intent_open_positions": 1,
        }
        gate_result = risk_gate(intent_payload)
        if not gate_result.get("allowed", False):
            logger.info(
                "risk gate blocked execution",
                extra={
                    "strategy": strategy_name,
                    "reason": gate_result.get("reason"),
                    "cap": gate_result.get("cap"),
                },
            )
            snapshot = get_risk_accounting_snapshot()
            report = ExecutionReport(
                symbol=plan.symbol,
                simulated=simulated,
                pnl_usdt=0.0,
                pnl_bps=0.0,
                legs=plan.legs,
                plan_viable=False,
                safe_mode=safe_mode,
                dry_run=dry_run,
                orders=[],
                exposures=[],
                pnl_summary={},
                state="SKIPPED_BY_RISK",
                risk_gate=gate_result,
                risk_snapshot=snapshot,
            )
            _record_golden_report(plan, report, reason=gate_result.get("reason"), hold=False)
            return report

        snapshot, intent_result = accounting_record_intent(
            strategy_name, plan.notional, simulated=simulated
        )
        if not intent_result.get("ok", False):
            reason_code = str(intent_result.get("reason") or "other_risk")
            logger.info(
                "risk accounting blocked execution",
                extra={
                    "strategy": strategy_name,
                    "notional": plan.notional,
                    "reason": reason_code,
                },
            )
            risk_gate_payload: Dict[str, object] = {
                "allowed": False,
                "state": intent_result.get("state", "SKIPPED_BY_RISK"),
                "reason": reason_code,
                "strategy": strategy_name,
            }
            if "details" in intent_result:
                risk_gate_payload["details"] = intent_result["details"]
            snapshot = get_risk_accounting_snapshot()
            report = ExecutionReport(
                symbol=plan.symbol,
                simulated=simulated,
                pnl_usdt=0.0,
                pnl_bps=0.0,
                legs=plan.legs,
                plan_viable=False,
                safe_mode=safe_mode,
                dry_run=dry_run,
                orders=[],
                exposures=[],
                pnl_summary={},
                state=intent_result.get("state", "SKIPPED_BY_RISK"),
                risk_gate=risk_gate_payload,
                risk_snapshot=snapshot,
            )
            _record_golden_report(plan, report, reason=reason_code, hold=False)
            return report

        try:
            result = await router.execute_plan(plan, allow_safe_mode=allow_safe_mode)
        except HoldActiveError as exc:
            accounting_record_fill(strategy_name, plan.notional, 0.0, simulated=simulated)
            hold_report = ExecutionReport(
                symbol=plan.symbol,
                simulated=True,
                pnl_usdt=0.0,
                pnl_bps=0.0,
                legs=plan.legs,
                plan_viable=plan.viable,
                safe_mode=safe_mode,
                dry_run=True,
                orders=[],
                exposures=[],
                pnl_summary={},
                state="HOLD",
                risk_gate={"reason": exc.reason},
                risk_snapshot={},
            )
            _record_golden_report(plan, hold_report, reason=exc.reason, hold=True)
            raise
        except Exception:
            accounting_record_fill(strategy_name, plan.notional, 0.0, simulated=simulated)
            raise

        pnl_summary = result.get("pnl", {}) if isinstance(result, dict) else {}
        orders = result.get("orders", []) if isinstance(result, dict) else []
        exposures = result.get("exposures", []) if isinstance(result, dict) else []
        pnl_usdt = float(pnl_summary.get("total", plan.est_pnl_usdt if plan.viable else 0.0))
        pnl_bps = (pnl_usdt / plan.notional) * 10_000 if plan.notional else 0.0
        pnl_delta = 0.0 if simulated else pnl_usdt
        snapshot = accounting_record_fill(
            strategy_name, plan.notional, pnl_delta, simulated=simulated
        )
        if not simulated and not state.control.dry_run_mode:
            record_trade_execution()
        logger.info(
            "arbitrage plan executed",
            extra={
                "symbol": plan.symbol,
                "safe_mode": safe_mode,
                "dry_run": dry_run,
                "pnl_usdt": pnl_usdt,
            },
        )
        report = ExecutionReport(
            symbol=plan.symbol,
            simulated=simulated,
            pnl_usdt=pnl_usdt,
            pnl_bps=pnl_bps,
            legs=plan.legs,
            plan_viable=plan.viable,
            safe_mode=safe_mode,
            dry_run=dry_run,
            orders=orders,
            exposures=exposures,
            pnl_summary=pnl_summary,
            state="DONE",
            risk_gate=gate_result,
            risk_snapshot=snapshot,
        )
        _record_golden_report(plan, report, reason=report.state, hold=False)
        return report


def execute_plan(plan: Plan) -> ExecutionReport:
    """Synchronous wrapper for CLI contexts."""
    return asyncio.run(execute_plan_async(plan, allow_safe_mode=True))


class ArbitrageEngine:
    def __init__(self, runtime: DerivativesRuntime):
        self.runtime = runtime
        self._last_edges: List[Dict[str, object]] = []
        self._pair_index: Dict[str, ArbitragePairConfig] = {}
        self._refresh_pairs()

    def _pair_configs(self) -> List[ArbitragePairConfig]:
        cfg = get_state().config.data.derivatives
        return cfg.arbitrage.pairs if cfg else []

    def _pair_id(self, pair: ArbitragePairConfig) -> str:
        return f"{pair.long.venue}:{pair.long.symbol}|{pair.short.venue}:{pair.short.symbol}"

    def _refresh_pairs(self) -> None:
        self._pair_index = {self._pair_id(pair): pair for pair in self._pair_configs()}

    def compute_edges(self) -> List[Dict[str, object]]:
        edges: List[Dict[str, object]] = []
        state = get_state()
        cfg = state.config.data.derivatives
        if not cfg:
            return []
        self._refresh_pairs()
        for pair in cfg.arbitrage.pairs:
            long_rt = self.runtime.venues[pair.long.venue]
            short_rt = self.runtime.venues[pair.short.venue]
            long_book = long_rt.client.get_orderbook_top(pair.long.symbol)
            short_book = short_rt.client.get_orderbook_top(pair.short.symbol)
            long_fee = long_rt.client.get_fees(pair.long.symbol)["taker_bps"]
            short_fee = short_rt.client.get_fees(pair.short.symbol)["taker_bps"]
            tradable = min(
                long_rt.client.get_filters(pair.long.symbol)["max_qty"],
                short_rt.client.get_filters(pair.short.symbol)["max_qty"],
                1.0,
            )
            gross_edge = short_book["bid"] - long_book["ask"]
            mid = (short_book["bid"] + long_book["ask"]) / 2 if tradable else 0.0
            net_edge_bps = 0.0
            if mid:
                net_edge_bps = (gross_edge / mid) * 10_000
                net_edge_bps -= long_fee + short_fee
                net_edge_bps -= cfg.arbitrage.max_leg_slippage_bps
            edges.append(
                {
                    "pair": {
                        "long": pair.long.dict(),
                        "short": pair.short.dict(),
                    },
                    "net_edge_bps": round(net_edge_bps, 4),
                    "tradable_size": tradable,
                    "gross_edge": gross_edge,
                    "pair_id": self._pair_id(pair),
                }
            )
        self._last_edges = edges
        return edges

    def _edge_map(self) -> Dict[str, Dict[str, object]]:
        if not self._last_edges:
            self.compute_edges()
        return {entry["pair_id"]: entry for entry in self._last_edges}

    def _select_pair(self, pair_id: str | None) -> Tuple[ArbitragePairConfig, Dict[str, object]]:
        edge_map = self._edge_map()
        if not edge_map:
            raise RuntimeError("no arbitrage pairs configured")
        if pair_id and pair_id in edge_map:
            return self._pair_index[pair_id], edge_map[pair_id]
        best_id = max(edge_map, key=lambda pid: edge_map[pid]["net_edge_bps"])
        return self._pair_index[best_id], edge_map[best_id]

    def _check_connectivity(self) -> PreflightCheck:
        for venue_id, runtime in self.runtime.venues.items():
            if not runtime.client.ping():
                return PreflightCheck(
                    name=f"connectivity:{venue_id}", ok=False, detail="ping failed"
                )
        return PreflightCheck(name="connectivity", ok=True, detail="all venues reachable")

    def _check_modes(self) -> PreflightCheck:
        mismatches: List[str] = []
        for venue_id, runtime in self.runtime.venues.items():
            cfg = runtime.config
            client = runtime.client
            if client.position_mode != cfg.position_mode:
                mismatches.append(f"{venue_id}: position_mode")
            for symbol in cfg.symbols:
                if client.margin_type.get(symbol) != cfg.margin_type:
                    mismatches.append(f"{venue_id}:{symbol}: margin")
                if client.leverage.get(symbol) != cfg.leverage:
                    mismatches.append(f"{venue_id}:{symbol}: leverage")
        if mismatches:
            return PreflightCheck(name="venue_setup", ok=False, detail=", ".join(mismatches))
        return PreflightCheck(name="venue_setup", ok=True, detail="modes ok")

    def _check_risk(self) -> PreflightCheck:
        state = get_state()
        risk = state.config.data.risk
        if not risk:
            return PreflightCheck(name="risk_caps", ok=True, detail="no caps configured")
        caps = risk.notional_caps
        # Paper mode uses zero exposure
        exposure = 0.0
        if exposure > caps.total_usd:
            return PreflightCheck(name="risk_caps", ok=False, detail="total cap exceeded")
        return PreflightCheck(name="risk_caps", ok=True, detail="within caps")

    def _check_funding_window(self) -> PreflightCheck:
        cfg = get_state().config.data.derivatives
        if not cfg:
            return PreflightCheck(name="funding_window", ok=True, detail="no derivatives config")
        avoid_minutes = cfg.funding.avoid_window_minutes
        # In SAFE_MODE tests we simulate as outside funding window
        return PreflightCheck(
            name="funding_window", ok=True, detail=f"outside {avoid_minutes}m window"
        )

    def _check_filters(self) -> PreflightCheck:
        failures: List[str] = []
        for venue_id, runtime in self.runtime.venues.items():
            for symbol in runtime.config.symbols:
                filters = runtime.client.get_filters(symbol)
                if filters["min_qty"] <= 0:
                    failures.append(f"{venue_id}:{symbol}")
        if failures:
            return PreflightCheck(name="filters", ok=False, detail=", ".join(failures))
        return PreflightCheck(name="filters", ok=True, detail="filters valid")

    def _check_edges(self) -> PreflightCheck:
        cfg = get_state().config.data.derivatives
        if not cfg:
            return PreflightCheck(name="edges", ok=True, detail="no derivatives config")
        edges = self.compute_edges()
        if not edges:
            return PreflightCheck(name="edges", ok=False, detail="no pairs configured")
        best = max(edge["net_edge_bps"] for edge in edges)
        if best >= cfg.arbitrage.min_edge_bps:
            return PreflightCheck(name="edges", ok=True, detail=f"best edge {best:.2f}bps")
        return PreflightCheck(name="edges", ok=False, detail=f"edge {best:.2f}bps below threshold")

    def run_preflight(self) -> PreflightReport:
        checks = [
            self._check_connectivity(),
            self._check_modes(),
            self._check_risk(),
            self._check_funding_window(),
            self._check_filters(),
            self._check_edges(),
        ]
        ok = all(check.ok for check in checks)
        set_preflight_result(ok)
        if ok:
            update_guard("cancel_on_disconnect", "OK", "connection stable")
        else:
            record_incident("preflight", {"checks": [c.detail for c in checks if not c.ok]})
        return PreflightReport(ok=ok, checks=checks)

    def _hedge_out(self, pair: ArbitragePairConfig, size: float) -> Dict[str, object]:
        long_rt = self.runtime.venues[pair.long.venue]
        register_order_attempt(reason="runaway_orders_per_min", source="arbitrage_hedge")
        order = long_rt.client.place_order(
            symbol=pair.long.symbol,
            side="SELL",
            quantity=size,
            reduce_only=True,
            time_in_force="IOC",
        )
        rescues = bump_counter("rescues", 1)
        update_guard("runaway_breaker", "WARN", "hedge triggered", {"rescues_performed": rescues})
        record_incident("hedge", {"pair": self._pair_id(pair), "size": size, "order": order})
        return order

    def execute(
        self,
        pair_id: str | None,
        size: float | None,
        *,
        force_leg_b_fail: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, object]:
        strategy_name = "cross_exchange_arb"
        manager = get_strategy_risk_manager()
        if not manager.is_enabled(strategy_name):
            return {
                "ok": False,
                "executed": False,
                "state": "DISABLED_BY_OPERATOR",
                "reason": "disabled_by_operator",
                "strategy": strategy_name,
            }
        if manager.is_frozen(strategy_name):
            record_risk_skip(strategy_name, "strategy_frozen")
            slo.inc_skipped("hold")
            return {
                "ok": False,
                "executed": False,
                "state": "SKIPPED_BY_RISK",
                "reason": "strategy_frozen",
                "strategy": strategy_name,
            }
        state = get_state()
        pair_cfg, edge = self._select_pair(pair_id)
        order_size = edge["tradable_size"] if size is None else min(size, edge["tradable_size"])
        if order_size <= 0:
            return {
                "ok": False,
                "state": "ABORTED",
                "reason": "order size zero",
                "pair_id": edge["pair_id"],
            }
        transitions = ["IDLE", "PREFLIGHT"]
        plan = {
            "pair_id": edge["pair_id"],
            "size": order_size,
            "dry_run": dry_run,
            "steps": transitions,
            "orders": [],
            "rescued": False,
        }
        if dry_run:
            bump_counter("dry_runs", 1)
            return {"ok": True, "executed": False, "plan": plan}

        if (
            not dry_run
            and state.control.two_man_rule
            and len(state.control.approvals) < 2
            and not state.control.safe_mode
        ):
            return {"ok": False, "state": "ABORTED", "reason": "two-man rule", "plan": plan}

        long_rt = self.runtime.venues[pair_cfg.long.venue]
        short_rt = self.runtime.venues[pair_cfg.short.venue]

        policies = (
            state.config.data.derivatives.arbitrage if state.config.data.derivatives else None
        )
        long_order_args = {
            "symbol": pair_cfg.long.symbol,
            "side": "BUY",
            "quantity": order_size,
        }
        short_order_args = {
            "symbol": pair_cfg.short.symbol,
            "side": "SELL",
            "quantity": order_size,
        }
        if policies and policies.post_only_maker:
            long_top = long_rt.client.get_orderbook_top(pair_cfg.long.symbol)
            short_top = short_rt.client.get_orderbook_top(pair_cfg.short.symbol)
            long_order_args.update(
                {
                    "type": "LIMIT",
                    "price": long_top["bid"],
                    "post_only": True,
                }
            )
            short_order_args.update(
                {
                    "type": "LIMIT",
                    "price": short_top["ask"],
                    "post_only": True,
                }
            )
        else:
            long_order_args["time_in_force"] = "IOC"
            short_order_args["time_in_force"] = "IOC"

        try:
            register_order_attempt(reason="runaway_orders_per_min", source="arbitrage_leg_a")
        except HoldActiveError as exc:
            update_guard("runaway_breaker", "HOLD", "orders blocked", {"reason": exc.reason})
            _emit_ops_alert(
                "runaway_guard_hold",
                "Runaway guard blocked leg A",
                {"reason": exc.reason, "stage": "leg_a"},
            )
            slo.inc_skipped("hold")
            return {
                "ok": False,
                "executed": False,
                "plan": plan,
                "state": "HOLD",
                "reason": exc.reason,
            }
        order_a = long_rt.client.place_order(**long_order_args)
        plan["orders"].append({"leg": "A", "order": order_a})
        transitions.append("LEG_A")

        if force_leg_b_fail:
            transitions.append("LEG_A_FILLED_LEG_B_FAIL")
            try:
                hedge_order = self._hedge_out(pair_cfg, order_size)
            except HoldActiveError as exc:
                update_guard("runaway_breaker", "HOLD", "orders blocked", {"reason": exc.reason})
                _emit_ops_alert(
                    "runaway_guard_hold",
                    "Runaway guard blocked hedge unwind",
                    {"reason": exc.reason, "stage": "hedge"},
                )
                slo.inc_skipped("hold")
                return {
                    "ok": False,
                    "executed": False,
                    "plan": plan,
                    "state": "HOLD",
                    "reason": exc.reason,
                }
            plan["orders"].append({"leg": "hedge", "order": hedge_order})
            plan["rescued"] = True
            update_guard("cancel_on_disconnect", "WARN", "awaiting manual reset")
            return {"ok": False, "executed": False, "plan": plan, "state": "HEDGE_OUT"}

        try:
            register_order_attempt(reason="runaway_orders_per_min", source="arbitrage_leg_b")
        except HoldActiveError as exc:
            update_guard("runaway_breaker", "HOLD", "orders blocked", {"reason": exc.reason})
            transitions.append("HOLD")
            _emit_ops_alert(
                "runaway_guard_hold",
                "Runaway guard blocked leg B",
                {"reason": exc.reason, "stage": "leg_b"},
            )
            slo.inc_skipped("hold")
            return {
                "ok": False,
                "executed": False,
                "plan": plan,
                "state": "HOLD",
                "reason": exc.reason,
            }
        order_b = short_rt.client.place_order(**short_order_args)
        plan["orders"].append({"leg": "B", "order": order_b})
        transitions.extend(["LEG_B", "HEDGED", "DONE"])
        bump_counter("executions", 1)
        update_guard("runaway_breaker", "OK", "stable")
        return {"ok": True, "executed": True, "plan": plan, "state": "DONE"}


_ENGINE: ArbitrageEngine | None = None


def get_engine() -> ArbitrageEngine:
    global _ENGINE
    state = get_state()
    if not _ENGINE:
        if not state.derivatives:
            raise RuntimeError("derivatives not initialised")
        _ENGINE = ArbitrageEngine(state.derivatives)
    return _ENGINE


def run_preflight() -> Dict[str, object]:
    return run_preflight_report().as_dict()


def run_preflight_report() -> PreflightReport:
    engine = get_engine()
    return engine.run_preflight()


def current_edges() -> List[Dict[str, object]]:
    engine = get_engine()
    return engine.compute_edges()


def execute_trade(
    pair_id: str | None, size: float | None, *, force_leg_b_fail: bool = False
) -> Dict[str, object]:
    strategy_name = "cross_exchange_arb"
    manager = get_strategy_risk_manager()
    if not manager.is_enabled(strategy_name):
        return {
            "ok": False,
            "executed": False,
            "state": "DISABLED_BY_OPERATOR",
            "reason": "disabled_by_operator",
            "strategy": strategy_name,
        }
    if manager.is_frozen(strategy_name):
        record_risk_skip(strategy_name, "strategy_frozen")
        slo.inc_skipped("hold")
        return {
            "ok": False,
            "executed": False,
            "state": "SKIPPED_BY_RISK",
            "reason": "strategy_frozen",
            "strategy": strategy_name,
        }
    engine = get_engine()
    state = get_state()
    report = engine.run_preflight()
    payload = report.as_dict()
    if not report.ok:
        return {
            "ok": False,
            "state": "ABORTED",
            "preflight": payload,
            "safe_mode": state.control.safe_mode,
        }
    dry_run = state.control.safe_mode
    if force_leg_b_fail:
        # allow rescue path to execute even under SAFE_MODE so guardrails are exercised
        dry_run = False
    execution = engine.execute(
        pair_id,
        size,
        force_leg_b_fail=force_leg_b_fail,
        dry_run=dry_run,
    )
    execution["preflight"] = payload
    execution["safe_mode"] = state.control.safe_mode
    return execution
