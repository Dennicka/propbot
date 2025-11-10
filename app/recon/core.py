"""Core reconciliation utilities for comparing local and remote state."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Mapping, NamedTuple, Sequence

from .. import ledger
from ..services import runtime
from ..util.venues import VENUE_ALIASES

LOGGER = logging.getLogger(__name__)

_CONFIG_OVERRIDE: _ReconConfig | None = None


class ReconIssue(NamedTuple):
    """Detected divergence between local state and the exchange."""

    kind: str  # "POSITION" | "BALANCE" | "ORDER" | "PNL"
    venue: str
    symbol: str | None
    severity: str  # "INFO" | "WARN" | "CRITICAL"
    code: str
    details: str


class ReconResult(NamedTuple):
    """Result of a reconciliation sweep."""

    ts: float
    issues: list[ReconIssue]


@dataclass(slots=True)
class _ReconConfig:
    epsilon_position: Decimal
    epsilon_balance: Decimal
    epsilon_notional: Decimal
    auto_hold_on_critical: bool


_DEFAULT_CONFIG = _ReconConfig(
    epsilon_position=Decimal("0.0001"),
    epsilon_balance=Decimal("0.5"),
    epsilon_notional=Decimal("5.0"),
    auto_hold_on_critical=True,
)


@dataclass(slots=True)
class _PositionEntry:
    venue: str
    symbol: str
    qty: Decimal
    notional: Decimal | None


@dataclass(slots=True)
class _OrderEntry:
    venue: str
    symbol: str
    order_id: str
    qty: Decimal
    price: Decimal | None
    notional: Decimal | None


def compare_positions(
    local: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
    remote: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
) -> list[ReconIssue]:
    """Compare local vs remote positions and return detected divergences."""

    config = _get_recon_config()
    local_entries = _normalise_positions(local)
    remote_entries = _normalise_positions(remote)

    issues: list[ReconIssue] = []
    keys = set(local_entries) | set(remote_entries)
    for venue, symbol in sorted(keys):
        local_entry = local_entries.get((venue, symbol))
        remote_entry = remote_entries.get((venue, symbol))

        local_qty = local_entry.qty if local_entry else Decimal("0")
        remote_qty = remote_entry.qty if remote_entry else Decimal("0")
        delta = remote_qty - local_qty
        abs_qty = abs(delta)

        if abs_qty <= config.epsilon_position:
            continue

        notional_delta = _position_notional_delta(local_entry, remote_entry, delta)
        abs_notional = abs(notional_delta) if notional_delta is not None else abs_qty

        severity = _severity_for_delta(
            abs_qty,
            abs_notional,
            config,
            missing=local_entry is None or remote_entry is None,
        )
        issue = ReconIssue(
            kind="POSITION",
            venue=venue,
            symbol=symbol,
            severity=severity,
            code="POSITION_MISMATCH",
            details=(
                f"local_qty={local_qty:f} remote_qty={remote_qty:f} "
                f"delta={delta:f} notional={abs_notional:f}"
            ),
        )
        issues.append(issue)
    return issues


def compare_balances(
    local: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
    remote: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
) -> list[ReconIssue]:
    """Compare wallet balances between local snapshot and remote data."""

    config = _get_recon_config()
    local_entries = _normalise_balances(local)
    remote_entries = _normalise_balances(remote)

    issues: list[ReconIssue] = []
    keys = set(local_entries) | set(remote_entries)
    for venue, asset in sorted(keys):
        local_value = local_entries.get((venue, asset), Decimal("0"))
        remote_value = remote_entries.get((venue, asset), Decimal("0"))
        delta = remote_value - local_value
        abs_delta = abs(delta)
        if abs_delta <= config.epsilon_balance:
            continue
        severity = _severity_for_delta(
            abs_delta,
            abs_delta,
            config,
            missing=(local_value == 0 or remote_value == 0),
        )
        issue = ReconIssue(
            kind="BALANCE",
            venue=venue,
            symbol=asset,
            severity=severity,
            code="BALANCE_MISMATCH",
            details=(
                f"local_total={local_value:f} remote_total={remote_value:f} delta={delta:f}"
            ),
        )
        issues.append(issue)
    return issues


def compare_open_orders(
    local: Sequence[Mapping[str, object]] | Mapping[tuple[str, str], Mapping[str, object]] | None,
    remote: Sequence[Mapping[str, object]] | Mapping[tuple[str, str], Mapping[str, object]] | None,
) -> list[ReconIssue]:
    """Compare open order books and flag desynchronisation."""

    config = _get_recon_config()
    local_entries = _normalise_orders(local)
    remote_entries = _normalise_orders(remote)

    issues: list[ReconIssue] = []

    for key, remote_entry in remote_entries.items():
        local_entry = local_entries.get(key)
        if local_entry is None:
            abs_notional = _order_notional(remote_entry)
            severity = _severity_for_delta(
                abs(remote_entry.qty),
                abs_notional,
                config,
                missing=True,
            )
            issues.append(
                ReconIssue(
                    kind="ORDER",
                    venue=remote_entry.venue,
                    symbol=remote_entry.symbol,
                    severity=severity,
                    code="ORDER_DESYNC",
                    details=(
                        f"remote_only order_id={remote_entry.order_id} qty={remote_entry.qty:f} "
                        f"price={_format_decimal(remote_entry.price)} notional={abs_notional:f}"
                    ),
                )
            )
            continue

        qty_delta = remote_entry.qty - local_entry.qty
        if abs(qty_delta) > config.epsilon_position:
            abs_notional = _order_notional(remote_entry) or _order_notional(local_entry)
            severity = _severity_for_delta(
                abs(qty_delta),
                abs_notional or abs(qty_delta),
                config,
            )
            issues.append(
                ReconIssue(
                    kind="ORDER",
                    venue=remote_entry.venue,
                    symbol=remote_entry.symbol,
                    severity=severity,
                    code="ORDER_SIZE_MISMATCH",
                    details=(
                        f"order_id={remote_entry.order_id} local_qty={local_entry.qty:f} "
                        f"remote_qty={remote_entry.qty:f} delta={qty_delta:f}"
                    ),
                )
            )

    for key, local_entry in local_entries.items():
        if key in remote_entries:
            continue
        abs_notional = _order_notional(local_entry)
        severity = "WARN"
        if abs_notional is not None and abs_notional >= config.epsilon_notional:
            severity = "CRITICAL"
        issues.append(
            ReconIssue(
                kind="ORDER",
                venue=local_entry.venue,
                symbol=local_entry.symbol,
                severity=severity,
                code="ORDER_LOCAL_ONLY",
                details=(
                    f"local_only order_id={local_entry.order_id} qty={local_entry.qty:f} "
                    f"price={_format_decimal(local_entry.price)} notional={_format_decimal(abs_notional)}"
                ),
            )
        )

    return issues


def reconcile_once(ctx: object | None = None) -> ReconResult:
    """Fetch ledger/runtime snapshots and run reconciliation once."""

    config = _get_recon_config(ctx)
    ts = time.time()

    local_positions = _load_local_positions(ctx)
    remote_positions = _load_remote_positions(ctx)
    local_balances = _load_local_balances(ctx)
    remote_balances = _load_remote_balances(ctx)
    local_orders = _load_local_orders(ctx)
    remote_orders = _load_remote_orders(ctx)

    global _CONFIG_OVERRIDE
    previous_override = _CONFIG_OVERRIDE
    _CONFIG_OVERRIDE = config
    try:
        issues: list[ReconIssue] = []
        issues.extend(compare_positions(local_positions, remote_positions))
        issues.extend(compare_balances(local_balances, remote_balances))
        issues.extend(compare_open_orders(local_orders, remote_orders))
    finally:
        _CONFIG_OVERRIDE = previous_override

    return ReconResult(ts=ts, issues=issues)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_positions(
    payload: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
) -> dict[tuple[str, str], _PositionEntry]:
    entries: dict[tuple[str, str], _PositionEntry] = {}
    if payload is None:
        return entries

    if isinstance(payload, Mapping):
        iterable = payload.items()
        for key, value in iterable:
            venue, symbol = key
            entry = _coerce_position(venue, symbol, value)
            if entry is not None:
                entries[(entry.venue, entry.symbol)] = entry
        return entries

    for row in payload:
        if not isinstance(row, Mapping):
            continue
        venue = row.get("venue") or row.get("exchange")
        symbol = row.get("symbol") or row.get("instrument")
        entry = _coerce_position(venue, symbol, row)
        if entry is not None:
            entries[(entry.venue, entry.symbol)] = entry
    return entries


def _coerce_position(venue: object, symbol: object, value: object) -> _PositionEntry | None:
    venue_name = _normalise_venue(venue)
    symbol_name = _normalise_symbol(symbol)
    if not venue_name or not symbol_name:
        return None

    qty = Decimal("0")
    notional: Decimal | None = None

    if isinstance(value, Mapping):
        qty = _to_decimal(
            value.get("qty")
            or value.get("base_qty")
            or value.get("position_amt")
            or value.get("position")
            or value.get("size"),
            default=Decimal("0"),
        )
        notional_value = value.get("notional") or value.get("notional_usd")
        if notional_value is None:
            price = _to_decimal(value.get("price") or value.get("mark_price"), default=None)
            if price is not None:
                notional_value = abs(qty) * price
        if notional_value is not None:
            notional = _to_decimal(notional_value, default=None)
    else:
        qty = _to_decimal(value, default=Decimal("0"))

    return _PositionEntry(venue=venue_name, symbol=symbol_name, qty=qty, notional=notional)


def _position_notional_delta(
    local_entry: _PositionEntry | None,
    remote_entry: _PositionEntry | None,
    qty_delta: Decimal,
) -> Decimal | None:
    local_notional = local_entry.notional if local_entry else None
    remote_notional = remote_entry.notional if remote_entry else None

    if local_notional is None and remote_notional is None:
        if remote_entry and remote_entry.notional is not None:
            remote_notional = remote_entry.notional
        elif local_entry and local_entry.notional is not None:
            local_notional = local_entry.notional
        else:
            return abs(qty_delta)

    return (remote_notional or Decimal("0")) - (local_notional or Decimal("0"))


def _normalise_balances(
    payload: Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]] | None,
) -> dict[tuple[str, str], Decimal]:
    entries: dict[tuple[str, str], Decimal] = {}
    if payload is None:
        return entries
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            venue, asset = key
            venue_name = _normalise_venue(venue)
            asset_name = _normalise_symbol(asset)
            if not venue_name or not asset_name:
                continue
            entries[(venue_name, asset_name)] = _to_decimal(value, default=Decimal("0"))
        return entries

    for row in payload:
        if not isinstance(row, Mapping):
            continue
        venue_name = _normalise_venue(row.get("venue") or row.get("exchange"))
        asset_name = _normalise_symbol(
            row.get("asset") or row.get("currency") or row.get("symbol")
        )
        if not venue_name or not asset_name:
            continue
        amount = (
            row.get("total")
            or row.get("qty")
            or row.get("balance")
            or row.get("amount")
            or row.get("walletBalance")
            or row.get("equity")
            or row.get("free")
            or row.get("availableBalance")
        )
        entries[(venue_name, asset_name)] = entries.get((venue_name, asset_name), Decimal("0")) + _to_decimal(
            amount,
            default=Decimal("0"),
        )
    return entries


def _normalise_orders(
    payload: Sequence[Mapping[str, object]] | Mapping[tuple[str, str], Mapping[str, object]] | None,
) -> dict[tuple[str, str], _OrderEntry]:
    entries: dict[tuple[str, str], _OrderEntry] = {}
    if payload is None:
        return entries

    rows: list[Mapping[str, object]] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            venue, order_id = key
            if not isinstance(value, Mapping):
                continue
            row = dict(value)
            row.setdefault("venue", venue)
            row.setdefault("id", order_id)
            rows.append(row)
    else:
        rows = [row for row in payload if isinstance(row, Mapping)]

    for row in rows:
        venue = _normalise_venue(row.get("venue") or row.get("exchange"))
        order_id = _normalise_order_id(row)
        symbol = _normalise_symbol(row.get("symbol") or row.get("instrument"))
        if not venue or not order_id:
            continue
        qty = _to_decimal(
            row.get("qty")
            or row.get("quantity")
            or row.get("origQty")
            or row.get("size"),
            default=Decimal("0"),
        )
        price = _to_decimal(
            row.get("price")
            or row.get("limit_price")
            or row.get("avgPrice"),
            default=None,
        )
        notional_val = row.get("notional") or row.get("notional_usd")
        if notional_val is None and price is not None:
            notional_val = abs(qty) * price
        notional = _to_decimal(notional_val, default=None)
        entries[(venue, order_id)] = _OrderEntry(
            venue=venue,
            symbol=symbol or None,
            order_id=order_id,
            qty=qty,
            price=price,
            notional=notional,
        )
    return entries


def _order_notional(entry: _OrderEntry | None) -> Decimal | None:
    if entry is None:
        return None
    if entry.notional is not None:
        return abs(entry.notional)
    if entry.price is not None:
        return abs(entry.qty) * entry.price
    return abs(entry.qty)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_local_positions(ctx: object | None) -> Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "local_positions")
    if provider is not None:
        return provider
    try:
        return ledger.fetch_positions()
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("recon.fetch_local_positions_failed")
        return []


def _load_remote_positions(ctx: object | None) -> Mapping[tuple[str, str], object] | Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "remote_positions")
    if provider is not None:
        return provider
    try:
        from .reconciler import Reconciler

        reconciler = Reconciler()
        return reconciler.fetch_exchange_positions()
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("recon.fetch_remote_positions_failed")
        return {}


def _load_local_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "local_balances")
    if provider is not None:
        return provider
    try:
        return ledger.fetch_balances()
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("recon.fetch_local_balances_failed")
        return []


def _load_remote_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "remote_balances")
    if provider is not None:
        return provider
    return []


def _load_local_orders(ctx: object | None) -> Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "local_orders")
    if provider is not None:
        return provider
    try:
        return ledger.fetch_open_orders()
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("recon.fetch_local_orders_failed")
        return []


def _load_remote_orders(ctx: object | None) -> Sequence[Mapping[str, object]]:
    provider = _resolve_provider(ctx, "remote_orders")
    if provider is not None:
        return provider
    return []


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _get_recon_config(ctx: object | None = None) -> _ReconConfig:
    if ctx is None and _CONFIG_OVERRIDE is not None:
        return _CONFIG_OVERRIDE
    source = None
    if ctx is not None:
        source = getattr(ctx, "cfg", None)
    if source is None:
        state = runtime.get_state()
        source = getattr(getattr(state, "config", None), "data", None)
    recon_cfg = None
    if isinstance(source, Mapping):
        recon_cfg = source.get("recon")
    else:
        recon_cfg = getattr(source, "recon", None)
    if recon_cfg is None and ctx is not None:
        recon_cfg = getattr(ctx, "recon", None)
    config = _DEFAULT_CONFIG
    if recon_cfg is None:
        return config

    epsilon_position = _coerce_decimal(_cfg_get(recon_cfg, "epsilon_position"), config.epsilon_position)
    epsilon_balance = _coerce_decimal(_cfg_get(recon_cfg, "epsilon_balance"), config.epsilon_balance)
    epsilon_notional = _coerce_decimal(_cfg_get(recon_cfg, "epsilon_notional"), config.epsilon_notional)

    auto_hold = _cfg_get(recon_cfg, "auto_hold_on_critical")
    auto_hold_bool = bool(auto_hold) if auto_hold is not None else config.auto_hold_on_critical

    return _ReconConfig(
        epsilon_position=epsilon_position or config.epsilon_position,
        epsilon_balance=epsilon_balance or config.epsilon_balance,
        epsilon_notional=epsilon_notional or config.epsilon_notional,
        auto_hold_on_critical=auto_hold_bool,
    )


def _cfg_get(cfg: object, name: str) -> object | None:
    if isinstance(cfg, Mapping):
        if name in cfg:
            return cfg[name]
        return None
    return getattr(cfg, name, None)


def _coerce_decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
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


def _severity_for_delta(
    abs_qty: Decimal,
    abs_notional: Decimal,
    config: _ReconConfig,
    *,
    missing: bool = False,
) -> str:
    if missing and abs_qty >= config.epsilon_position:
        return "CRITICAL"
    if abs_notional >= config.epsilon_notional:
        return "CRITICAL"
    if abs_qty >= config.epsilon_position:
        return "WARN"
    return "INFO"


def _resolve_provider(ctx: object | None, name: str):
    if ctx is None:
        return None
    value = getattr(ctx, name, None)
    if callable(value):
        try:
            return value()
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("recon.provider_failed", extra={"provider": name})
            return None
    return value


def _normalise_venue(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    canonical = VENUE_ALIASES.get(text, text)
    return canonical.replace("_", "-")


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.replace("-", "").replace("_", "") if text else ""


def _to_decimal(value: object, *, default: Decimal | None) -> Decimal | None:
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


def _normalise_order_id(row: Mapping[str, object]) -> str:
    for key in ("id", "order_id", "client_order_id", "idemp_key", "orderId", "origClientOrderId"):
        value = row.get(key)
        if value in (None, ""):
            continue
        return str(value)
    return ""


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:f}"


__all__ = [
    "ReconIssue",
    "ReconResult",
    "compare_positions",
    "compare_balances",
    "compare_open_orders",
    "reconcile_once",
]
