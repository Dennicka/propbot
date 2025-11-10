"""Core reconciliation logic for comparing local and exchange state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import time
from types import SimpleNamespace
from typing import Mapping, MutableMapping, Sequence, Literal

from .. import ledger
from ..metrics.recon import PNL_LEDGER_REALIZED_TODAY
from ..services import runtime
from .reconciler import Reconciler


LOGGER = logging.getLogger(__name__)

_DEFAULT_WARN = Decimal("5")
_DEFAULT_CRITICAL = Decimal("25")


@dataclass(slots=True, frozen=True)
class ReconSnapshot:
    """Snapshot of a reconciliation check for a single venue/symbol/asset."""

    venue: str
    asset: str
    symbol: str | None
    side: str | None
    exch_position: Decimal | None
    local_position: Decimal | None
    exch_balance: Decimal | None
    local_balance: Decimal | None
    diff_abs: Decimal
    status: Literal["OK", "WARN", "CRITICAL"]
    reason: str
    ts: float


@dataclass(slots=True, frozen=True)
class ReconSettings:
    warn_notional_usd: Decimal = _DEFAULT_WARN
    critical_notional_usd: Decimal = _DEFAULT_CRITICAL


def reconcile_once(ctx: object | None = None) -> list[ReconSnapshot]:
    """Perform a single reconciliation sweep and return detailed snapshots."""

    settings = _resolve_settings(ctx)
    timestamp = time.time()

    reconciler = Reconciler()
    exchange_positions = _fetch_exchange_positions(reconciler)
    ledger_positions = _fetch_ledger_positions(reconciler)
    state = _resolve_state(ctx)

    position_snapshots = _build_position_snapshots(
        exchange_positions,
        ledger_positions,
        state,
        settings,
        timestamp,
    )

    balance_snapshots = _build_balance_snapshots(ctx, settings, timestamp)

    _record_ledger_realized_today(timestamp)

    return position_snapshots + balance_snapshots


# ---------------------------------------------------------------------------
# Helpers – configuration & state resolution


def _resolve_settings(ctx: object | None) -> ReconSettings:
    warn = _DEFAULT_WARN
    critical = _DEFAULT_CRITICAL

    if ctx is not None:
        cfg = getattr(ctx, "cfg", None)
        recon_cfg = getattr(cfg, "recon", None) if cfg is not None else None
        warn, critical = _extract_thresholds(recon_cfg, warn, critical)
    else:
        state = _resolve_state(ctx)
        recon_cfg = getattr(getattr(state, "config", None), "data", None)
        recon_cfg = getattr(recon_cfg, "recon", None) if recon_cfg is not None else None
        warn, critical = _extract_thresholds(recon_cfg, warn, critical)

    return ReconSettings(warn_notional_usd=warn, critical_notional_usd=critical)


def _extract_thresholds(
    recon_cfg: object | None, warn_default: Decimal, crit_default: Decimal
) -> tuple[Decimal, Decimal]:
    warn = warn_default
    critical = crit_default
    if recon_cfg is None:
        return warn, critical

    warn_raw = getattr(recon_cfg, "warn_notional_usd", None)
    critical_raw = getattr(recon_cfg, "critical_notional_usd", None)
    if warn_raw is None:
        warn_raw = getattr(recon_cfg, "diff_abs_usd_warn", None)
    if critical_raw is None:
        critical_raw = getattr(recon_cfg, "diff_abs_usd_crit", None)

    warn = _coerce_decimal(warn_raw, warn)
    critical = _coerce_decimal(critical_raw, critical)
    if critical < warn:
        critical = warn
    return warn, critical


def _resolve_state(ctx: object | None) -> object:
    if ctx is not None:
        candidate = getattr(ctx, "state", None)
        if candidate is not None:
            return candidate
        candidate = getattr(ctx, "runtime", None)
        state_getter = getattr(candidate, "get_state", None)
        if callable(state_getter):
            try:
                return state_getter()
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("ctx.runtime.get_state failed", exc_info=True)
    return runtime.get_state()


def _coerce_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return default
    return default


def _record_ledger_realized_today(now: float) -> None:
    today_key = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
    try:
        ledger_obj = ledger.build_ledger_from_history(None, now - 7 * 24 * 60 * 60)
    except Exception:  # pragma: no cover - defensive
        PNL_LEDGER_REALIZED_TODAY.set(0.0)
        return
    try:
        snapshots = ledger_obj.daily_snapshots()
    except AttributeError:
        PNL_LEDGER_REALIZED_TODAY.set(0.0)
        return
    realized = Decimal("0")
    for snapshot in snapshots:
        if getattr(snapshot, "date", None) == today_key:
            realized += getattr(snapshot, "realized_pnl", Decimal("0"))
    PNL_LEDGER_REALIZED_TODAY.set(float(realized))


# ---------------------------------------------------------------------------
# Position reconciliation helpers


def _fetch_exchange_positions(reconciler: Reconciler) -> Mapping[tuple[str, str], Decimal]:
    payload = reconciler.fetch_exchange_positions()
    return {
        (venue, symbol): _coerce_decimal(qty)
        for (venue, symbol), qty in payload.items()
    }


def _fetch_ledger_positions(reconciler: Reconciler) -> Mapping[tuple[str, str], dict[str, Decimal]]:
    entries = reconciler.fetch_ledger_positions()
    converted: dict[tuple[str, str], dict[str, Decimal]] = {}
    for key, entry in entries.items():
        qty = _coerce_decimal(entry.get("qty"), Decimal("0"))
        avg_price = _coerce_decimal(entry.get("avg_price"), Decimal("0"))
        converted[key] = {"qty": qty, "avg_price": avg_price}
    return converted


def _build_position_snapshots(
    exchange_positions: Mapping[tuple[str, str], Decimal],
    ledger_positions: Mapping[tuple[str, str], Mapping[str, Decimal]],
    state: object,
    settings: ReconSettings,
    ts: float,
) -> list[ReconSnapshot]:
    keys = set(exchange_positions) | set(ledger_positions)
    if not keys:
        return []

    price_context = _build_price_context(exchange_positions, ledger_positions, state)
    snapshots: list[ReconSnapshot] = []

    for venue, symbol in sorted(keys):
        exch_qty = exchange_positions.get((venue, symbol), Decimal("0"))
        ledger_entry = ledger_positions.get((venue, symbol))
        local_qty = ledger_entry.get("qty") if ledger_entry else Decimal("0")
        delta = exch_qty - local_qty
        notional = _estimate_notional(symbol, delta, price_context)
        status = _classify(notional, settings)
        side = _resolve_side(exch_qty, local_qty)
        reason = "position_mismatch" if not _is_close(delta) else "position_ok"
        snapshots.append(
            ReconSnapshot(
                venue=venue,
                asset=_derive_asset(symbol),
                symbol=symbol,
                side=side,
                exch_position=exch_qty,
                local_position=local_qty,
                exch_balance=None,
                local_balance=None,
                diff_abs=notional,
                status=status,
                reason=reason,
                ts=ts,
            )
        )
    return snapshots


def _build_price_context(
    exchange_positions: Mapping[tuple[str, str], Decimal],
    ledger_positions: Mapping[tuple[str, str], Mapping[str, Decimal]],
    state: object,
) -> SimpleNamespace:
    risk_snapshot = getattr(getattr(state, "safety", None), "risk_snapshot", {})
    exposures = risk_snapshot.get("exposure_by_symbol") if isinstance(risk_snapshot, Mapping) else {}
    risk_notional: dict[str, Decimal] = {}
    if isinstance(exposures, Mapping):
        for symbol, value in exposures.items():
            risk_notional[str(symbol).upper()] = _coerce_decimal(value, Decimal("0"))

    symbol_totals: dict[str, Decimal] = {}
    symbol_prices: dict[str, Decimal] = {}
    for (venue, symbol), entry in ledger_positions.items():
        qty = abs(entry.get("qty", Decimal("0")))
        if qty <= Decimal("0"):
            continue
        symbol_totals[symbol] = symbol_totals.get(symbol, Decimal("0")) + qty
        avg_price = entry.get("avg_price", Decimal("0"))
        symbol_prices[symbol] = symbol_prices.get(symbol, Decimal("0")) + qty * avg_price

    exchange_totals: dict[str, Decimal] = {}
    for (_, symbol), qty in exchange_positions.items():
        exchange_totals[symbol] = exchange_totals.get(symbol, Decimal("0")) + abs(qty)

    weighted_price: dict[str, Decimal] = {}
    for symbol, total in symbol_totals.items():
        if total > Decimal("0"):
            avg = symbol_prices[symbol] / total
            weighted_price[symbol] = avg

    symbol_candidates: dict[str, set[str]] = {}
    for venue, symbol in set(exchange_positions) | set(ledger_positions):
        symbol_candidates.setdefault(symbol, set()).add(venue)

    reconciler = Reconciler()
    mark_prices_float = reconciler._fetch_mark_prices(symbol_candidates)  # type: ignore[attr-defined]
    mark_prices: dict[str, Decimal] = {}
    for symbol, price in mark_prices_float.items():
        mark_prices[symbol] = _coerce_decimal(price, Decimal("0"))

    return SimpleNamespace(
        risk_notional=risk_notional,
        weighted_price=weighted_price,
        ledger_totals=symbol_totals,
        exchange_totals=exchange_totals,
        mark_prices=mark_prices,
    )


def _estimate_notional(symbol: str, delta: Decimal, ctx: SimpleNamespace) -> Decimal:
    abs_qty = abs(delta)
    if abs_qty == 0:
        return Decimal("0")

    price = _resolve_price(symbol, ctx)
    if price <= 0:
        return abs_qty
    return abs_qty * price


def _resolve_price(symbol: str, ctx: SimpleNamespace) -> Decimal:
    norm_symbol = symbol
    risk_notional = ctx.risk_notional.get(norm_symbol, Decimal("0"))
    ledger_total = ctx.ledger_totals.get(norm_symbol, Decimal("0"))
    exchange_total = ctx.exchange_totals.get(norm_symbol, Decimal("0"))
    weighted_price = ctx.weighted_price.get(norm_symbol, Decimal("0"))
    mark_price = ctx.mark_prices.get(norm_symbol, Decimal("0"))

    if risk_notional > 0 and ledger_total > 0:
        return risk_notional / ledger_total
    if risk_notional > 0 and exchange_total > 0:
        return risk_notional / exchange_total
    if weighted_price > 0:
        return weighted_price
    if mark_price > 0:
        return mark_price
    if risk_notional > 0:
        return risk_notional
    return Decimal("0")


def _resolve_side(exchange_qty: Decimal, local_qty: Decimal) -> str | None:
    for qty in (exchange_qty, local_qty):
        if qty is not None and qty != 0:
            return "LONG" if qty > 0 else "SHORT"
    return None


def _derive_asset(symbol: str) -> str:
    clean = str(symbol or "").upper()
    if not clean:
        return "UNKNOWN"
    for separator in ("-", "/", ":"):
        if separator in clean:
            parts = clean.split(separator)
            if len(parts) > 1:
                return parts[-1]
    if clean.endswith("USDT"):
        return "USDT"
    if clean.endswith("USD"):
        return "USD"
    return clean


def _is_close(delta: Decimal) -> bool:
    return abs(delta) <= Decimal("0")


def _classify(value: Decimal, settings: ReconSettings) -> Literal["OK", "WARN", "CRITICAL"]:
    if value >= settings.critical_notional_usd:
        return "CRITICAL"
    if value >= settings.warn_notional_usd:
        return "WARN"
    return "OK"


# ---------------------------------------------------------------------------
# Balance reconciliation helpers


def _build_balance_snapshots(
    ctx: object | None,
    settings: ReconSettings,
    ts: float,
) -> list[ReconSnapshot]:
    local_rows = _resolve_local_balances(ctx)
    remote_rows = _resolve_remote_balances(ctx)
    price_lookup = _resolve_price_lookup(ctx)

    local_map = _normalise_balances(local_rows)
    remote_map = _normalise_balances(remote_rows)

    keys = set(local_map) | set(remote_map)
    if not keys:
        return []

    snapshots: list[ReconSnapshot] = []
    for venue, asset in sorted(keys):
        local_value = local_map.get((venue, asset), Decimal("0"))
        remote_value = remote_map.get((venue, asset), Decimal("0"))
        delta = remote_value - local_value
        abs_delta = abs(delta)
        multiplier = _asset_usd_multiplier(asset, price_lookup)
        notional = abs_delta * multiplier if multiplier is not None else abs_delta
        status = _classify(notional, settings)
        reason = "balance_mismatch" if abs_delta > Decimal("0") else "balance_ok"
        snapshots.append(
            ReconSnapshot(
                venue=venue,
                asset=asset,
                symbol=None,
                side=None,
                exch_position=None,
                local_position=None,
                exch_balance=remote_value,
                local_balance=local_value,
                diff_abs=notional,
                status=status,
                reason=reason,
                ts=ts,
            )
        )
    return snapshots


def _resolve_local_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    if ctx is not None:
        candidate = getattr(ctx, "local_balances", None)
        rows = _maybe_rows(candidate)
        if rows is not None:
            return rows
        ledger_candidate = getattr(ctx, "ledger", None)
        rows = _maybe_rows(getattr(ledger_candidate, "fetch_balances", None))
        if rows is not None:
            return rows
    return ledger.fetch_balances()


def _resolve_remote_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    if ctx is not None:
        candidate = getattr(ctx, "remote_balances", None)
        rows = _maybe_rows(candidate)
        if rows is not None:
            return rows
        runtime_candidate = getattr(ctx, "runtime", None)
        rows = _maybe_rows(getattr(runtime_candidate, "remote_balances", None))
        if rows is not None:
            return rows
    return []


def _maybe_rows(candidate) -> Sequence[Mapping[str, object]] | None:
    if candidate is None:
        return None
    if callable(candidate):
        try:
            result = candidate()
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("recon.balance_source_failed", exc_info=True)
            return None
    else:
        result = candidate
    if isinstance(result, Sequence):
        rows: list[Mapping[str, object]] = []
        for row in result:
            if isinstance(row, Mapping):
                rows.append(row)
        return rows
    return None


def _normalise_balances(
    rows: Sequence[Mapping[str, object]]
) -> MutableMapping[tuple[str, str], Decimal]:
    mapping: MutableMapping[tuple[str, str], Decimal] = {}
    for row in rows:
        venue = str(row.get("venue") or row.get("exchange") or "").lower()
        asset = str(row.get("asset") or row.get("currency") or row.get("symbol") or "").upper()
        if not venue or not asset:
            continue
        amount = _coerce_decimal(_extract_balance_amount(row), Decimal("0"))
        mapping[(venue, asset)] = mapping.get((venue, asset), Decimal("0")) + amount
    return mapping


def _extract_balance_amount(row: Mapping[str, object]) -> object:
    for key in ("total", "qty", "balance", "amount", "walletBalance", "equity"):
        if key in row:
            value = row.get(key)
            if value not in (None, ""):
                return value
    for key in ("free", "availableBalance"):
        if key in row:
            value = row.get(key)
            if value not in (None, ""):
                return value
    return 0


def _resolve_price_lookup(ctx: object | None):
    if ctx is None:
        return None
    candidate = getattr(ctx, "asset_prices", None)
    if isinstance(candidate, Mapping):
        lookup: dict[str, Decimal] = {}
        for key, value in candidate.items():
            try:
                lookup[str(key).upper()] = _coerce_decimal(value, Decimal("0"))
            except InvalidOperation:
                continue
        return lookup
    candidate = getattr(ctx, "get_asset_price", None)
    if callable(candidate):
        return candidate
    return None


def _asset_usd_multiplier(asset: str, lookup) -> Decimal | None:
    stable = {"USD", "USDT", "USDC", "BUSD", "USDP", "DAI", "TUSD"}
    if asset in stable:
        return Decimal("1")
    if lookup is None:
        return None
    if isinstance(lookup, Mapping):
        value = lookup.get(asset)
        if value is None:
            return None
        return _coerce_decimal(value, Decimal("0"))
    try:
        price = lookup(asset)
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return _coerce_decimal(price, Decimal("0"))
    except InvalidOperation:
        return None


__all__ = ["ReconSnapshot", "ReconSettings", "reconcile_once"]

