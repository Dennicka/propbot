"""Utilities for computing per-strategy trade performance snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from .. import ledger
from ..persistence import order_store
from ..strategies.registry import StrategyId
from ..utils.decimal import to_decimal
from .ledger import PnLLedger, TradeFill
from .models import StrategyPerformanceSnapshot


_DECIMAL_ZERO = Decimal("0")


@dataclass(slots=True)
class TradeRecord:
    """Normalised trade record enriched with strategy metadata."""

    strategy_id: StrategyId
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    ts: float
    request_id: str | None = None

    @property
    def notional(self) -> Decimal:
        return (self.price * self.qty).copy_abs()


def _normalise_side(value: str) -> str:
    text = (value or "").strip().upper()
    if text in {"SELL", "SHORT", "ASK"}:
        return "SELL"
    return "BUY"


def _parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).timestamp()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            cleaned = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                parsed = datetime.fromisoformat(cleaned)
            except ValueError:
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.timestamp()
    return 0.0


def compute_max_drawdown(equity_curve: Sequence[Decimal]) -> Decimal:
    """Return the peak-to-trough drawdown for an equity curve."""

    if not equity_curve:
        return _DECIMAL_ZERO
    peak = equity_curve[0]
    max_drawdown = _DECIMAL_ZERO
    for point in equity_curve:
        if point > peak:
            peak = point
            continue
        drawdown = peak - point
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def build_strategy_performance(
    trades: Iterable[TradeRecord],
) -> list[StrategyPerformanceSnapshot]:
    """Aggregate trade history into performance metrics per strategy."""

    buckets: dict[StrategyId, list[TradeRecord]] = {}
    for trade in trades:
        strategy_id = (trade.strategy_id or "").strip()
        if not strategy_id:
            continue
        bucket = buckets.setdefault(strategy_id, [])
        bucket.append(trade)

    snapshots: list[StrategyPerformanceSnapshot] = []

    for strategy_id, records in sorted(buckets.items()):
        records.sort(key=lambda trade: trade.ts)
        ledger_state = PnLLedger()
        for trade in records:
            if trade.qty <= _DECIMAL_ZERO:
                continue
            fill = TradeFill(
                venue="internal",
                symbol=trade.symbol,
                side=_normalise_side(trade.side),
                qty=trade.qty.copy_abs(),
                price=trade.price,
                fee=trade.fee,
                fee_asset="USD",
                ts=trade.ts,
                is_simulated=False,
            )
            ledger_state.apply_fill(fill, exclude_simulated=False)

        entries = list(ledger_state.iter_entries())
        if not entries:
            snapshots.append(
                StrategyPerformanceSnapshot(
                    strategy_id=strategy_id,
                    trades_count=0,
                    winning_trades=0,
                    losing_trades=0,
                    gross_pnl=_DECIMAL_ZERO,
                    net_pnl=_DECIMAL_ZERO,
                    average_trade_pnl=_DECIMAL_ZERO,
                    winrate=0.0,
                    turnover_notional=_DECIMAL_ZERO,
                    max_drawdown=_DECIMAL_ZERO,
                )
            )
            continue

        trades_count = 0
        winning_trades = 0
        losing_trades = 0
        gross_pnl = _DECIMAL_ZERO
        net_pnl = _DECIMAL_ZERO
        turnover = _DECIMAL_ZERO
        equity_curve: list[Decimal] = []
        running_net = _DECIMAL_ZERO

        for entry in entries:
            trades_count += 1
            gross_pnl += entry.realized_pnl
            net = entry.realized_pnl - entry.fee + entry.rebate + entry.funding
            net_pnl += net
            turnover += entry.notional
            if net > _DECIMAL_ZERO:
                winning_trades += 1
            elif net < _DECIMAL_ZERO:
                losing_trades += 1
            running_net += net
            equity_curve.append(running_net)

        average_trade_pnl = net_pnl / trades_count if trades_count else _DECIMAL_ZERO
        winrate = winning_trades / trades_count if trades_count else 0.0
        max_drawdown = compute_max_drawdown(equity_curve)

        snapshots.append(
            StrategyPerformanceSnapshot(
                strategy_id=strategy_id,
                trades_count=trades_count,
                winning_trades=winning_trades,
                losing_trades=losing_trades,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                average_trade_pnl=average_trade_pnl,
                winrate=winrate,
                turnover_notional=turnover,
                max_drawdown=max_drawdown,
            )
        )

    return snapshots


def _normalise_symbol(value: object) -> str:
    return (str(value or "")).strip().upper() or "UNKNOWN"


def _normalise_fee(value: object) -> Decimal:
    return to_decimal(value, default=_DECIMAL_ZERO)


def _normalise_qty(value: object) -> Decimal:
    qty = to_decimal(value, default=_DECIMAL_ZERO)
    return qty.copy_abs()


def _normalise_price(value: object) -> Decimal:
    return to_decimal(value, default=_DECIMAL_ZERO)


def get_recent_trades(limit: int = 1000) -> list[TradeRecord]:
    """Fetch and normalise recent ledger fills into trade records."""

    rows = ledger.fetch_recent_fills(limit)
    if not rows:
        return []

    request_ids = {
        str(row.get("idemp_key")).strip()
        for row in rows
        if isinstance(row.get("idemp_key"), (str, int, float))
    }
    request_ids = {value for value in request_ids if value}

    strategy_by_request: dict[str, str | None]
    if request_ids:
        with order_store.session_scope() as session:
            strategy_by_request = order_store.strategies_by_request_ids(session, request_ids)
    else:
        strategy_by_request = {}

    trades: list[TradeRecord] = []
    for row in rows:
        request_id_raw = row.get("idemp_key")
        request_id = str(request_id_raw).strip() if request_id_raw is not None else ""
        strategy_id = strategy_by_request.get(request_id)
        if not strategy_id:
            continue
        qty = _normalise_qty(row.get("qty"))
        if qty <= _DECIMAL_ZERO:
            continue
        price = _normalise_price(row.get("price"))
        fee = _normalise_fee(row.get("fee"))
        symbol = _normalise_symbol(row.get("symbol"))
        side = _normalise_side(str(row.get("side") or ""))
        ts = _parse_timestamp(row.get("ts"))
        trades.append(
            TradeRecord(
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                fee=fee,
                ts=ts,
                request_id=request_id or None,
            )
        )

    trades.sort(key=lambda trade: trade.ts)
    return trades


__all__ = [
    "TradeRecord",
    "build_strategy_performance",
    "compute_max_drawdown",
    "get_recent_trades",
]
