"""Reconciliation helpers comparing internal and external exchange state."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Sequence

from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    ReconIssue,
    ReconSnapshot,
    VenueId,
)

from .engine_legacy import (
    _load_outbox,  # noqa: F401 - legacy compat
    _report_path,  # noqa: F401 - legacy compat
    ledger,  # noqa: F401 - legacy compat
    run_recon,  # noqa: F401 - legacy compat
)


def reconcile_balances(
    venue_id: VenueId,
    internal: Iterable[ExchangeBalanceSnapshot],
    external: Iterable[ExchangeBalanceSnapshot],
    *,
    tolerance: Decimal = Decimal("0.0001"),
) -> list[ReconIssue]:
    """Compare internal vs external balances and record mismatches."""

    internal_map: dict[tuple[str], ExchangeBalanceSnapshot] = {
        (balance.asset,): balance for balance in internal
    }
    external_map: dict[tuple[str], ExchangeBalanceSnapshot] = {
        (balance.asset,): balance for balance in external
    }

    issues: list[ReconIssue] = []
    all_assets = set(internal_map.keys()) | set(external_map.keys())

    for key in all_assets:
        asset = key[0]
        internal_balance = internal_map.get(key)
        external_balance = external_map.get(key)

        if internal_balance is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_internal",
                    venue_id=venue_id,
                    symbol=None,
                    asset=asset,
                    message="Balance present on exchange but missing internally",
                    internal_value=None,
                    external_value=str(external_balance.total) if external_balance else None,
                )
            )
            continue

        if external_balance is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_external",
                    venue_id=venue_id,
                    symbol=None,
                    asset=asset,
                    message="Balance present internally but missing on exchange",
                    internal_value=str(internal_balance.total),
                    external_value=None,
                )
            )
            continue

        diff = (internal_balance.total - external_balance.total).copy_abs()
        if diff > tolerance:
            issues.append(
                ReconIssue(
                    severity="error",
                    kind="balance_mismatch",
                    venue_id=venue_id,
                    symbol=None,
                    asset=asset,
                    message=f"Balance mismatch for asset={asset}",
                    internal_value=str(internal_balance.total),
                    external_value=str(external_balance.total),
                )
            )

    return issues


def reconcile_positions(
    venue_id: VenueId,
    internal: Iterable[ExchangePositionSnapshot],
    external: Iterable[ExchangePositionSnapshot],
    *,
    qty_tolerance: Decimal = Decimal("0.0001"),
    notional_tolerance: Decimal = Decimal("1"),
) -> list[ReconIssue]:
    """Compare internal vs external positions and record mismatches."""

    internal_map: dict[tuple[str], ExchangePositionSnapshot] = {
        (position.symbol,): position for position in internal
    }
    external_map: dict[tuple[str], ExchangePositionSnapshot] = {
        (position.symbol,): position for position in external
    }

    issues: list[ReconIssue] = []
    all_symbols = set(internal_map.keys()) | set(external_map.keys())

    for key in all_symbols:
        symbol = key[0]
        internal_position = internal_map.get(key)
        external_position = external_map.get(key)

        if internal_position is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_internal",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message="Position present on exchange but missing internally",
                    internal_value=None,
                    external_value=str(external_position.qty) if external_position else None,
                )
            )
            continue

        if external_position is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_external",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message="Position present internally but missing on exchange",
                    internal_value=str(internal_position.qty),
                    external_value=None,
                )
            )
            continue

        qty_diff = (internal_position.qty - external_position.qty).copy_abs()
        notional_diff = (internal_position.notional - external_position.notional).copy_abs()
        if qty_diff > qty_tolerance or notional_diff > notional_tolerance:
            issues.append(
                ReconIssue(
                    severity="error",
                    kind="position_mismatch",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message=f"Position mismatch for symbol={symbol}",
                    internal_value=f"qty={internal_position.qty} notional={internal_position.notional}",
                    external_value=f"qty={external_position.qty} notional={external_position.notional}",
                )
            )

    return issues


def reconcile_orders(
    venue_id: VenueId,
    internal: Iterable[ExchangeOrderSnapshot],
    external: Iterable[ExchangeOrderSnapshot],
) -> list[ReconIssue]:
    """Compare internal vs external open orders and record mismatches."""

    def _order_key(order: ExchangeOrderSnapshot) -> tuple[str, str | None]:
        identifier = order.client_order_id or order.exchange_order_id
        return (order.symbol, identifier)

    internal_map: dict[tuple[str, str | None], ExchangeOrderSnapshot] = {}
    for order in internal:
        internal_map[_order_key(order)] = order

    external_map: dict[tuple[str, str | None], ExchangeOrderSnapshot] = {}
    for order in external:
        external_map[_order_key(order)] = order

    issues: list[ReconIssue] = []
    all_keys = set(internal_map.keys()) | set(external_map.keys())

    for key in all_keys:
        internal_order = internal_map.get(key)
        external_order = external_map.get(key)
        symbol = key[0]

        if internal_order is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_internal",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message="Order present on exchange but missing internally",
                    internal_value=None,
                    external_value=_format_order_value(external_order),
                )
            )
            continue

        if external_order is None:
            issues.append(
                ReconIssue(
                    severity="warning",
                    kind="missing_external",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message="Order present internally but missing on exchange",
                    internal_value=_format_order_value(internal_order),
                    external_value=None,
                )
            )
            continue

        if (
            internal_order.qty != external_order.qty
            or internal_order.price != external_order.price
            or internal_order.side != external_order.side
        ):
            issues.append(
                ReconIssue(
                    severity="error",
                    kind="order_mismatch",
                    venue_id=venue_id,
                    symbol=symbol,
                    asset=None,
                    message=f"Order mismatch for symbol={symbol}",
                    internal_value=_format_order_value(internal_order),
                    external_value=_format_order_value(external_order),
                )
            )

    return issues


def _format_order_value(order: ExchangeOrderSnapshot | None) -> str | None:
    if order is None:
        return None
    identifier = order.client_order_id or order.exchange_order_id or "?"
    return f"id={identifier} side={order.side} qty={order.qty} price={order.price}"


def build_recon_snapshot(
    venue_id: VenueId,
    *,
    balances_internal: Sequence[ExchangeBalanceSnapshot],
    balances_external: Sequence[ExchangeBalanceSnapshot],
    positions_internal: Sequence[ExchangePositionSnapshot],
    positions_external: Sequence[ExchangePositionSnapshot],
    orders_internal: Sequence[ExchangeOrderSnapshot],
    orders_external: Sequence[ExchangeOrderSnapshot],
) -> ReconSnapshot:
    """Construct a reconciliation snapshot aggregating all issue types."""

    issues: list[ReconIssue] = []
    issues.extend(reconcile_balances(venue_id, balances_internal, balances_external))
    issues.extend(reconcile_positions(venue_id, positions_internal, positions_external))
    issues.extend(reconcile_orders(venue_id, orders_internal, orders_external))

    return ReconSnapshot(
        venue_id=venue_id,
        balances_internal=balances_internal,
        balances_external=balances_external,
        positions_internal=positions_internal,
        positions_external=positions_external,
        open_orders_internal=orders_internal,
        open_orders_external=orders_external,
        issues=issues,
    )


__all__ = [
    "reconcile_balances",
    "reconcile_positions",
    "reconcile_orders",
    "build_recon_snapshot",
    "run_recon",
]
