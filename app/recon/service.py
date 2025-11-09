"""Collect reconciliation snapshots comparing local and remote state."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal, Mapping, MutableMapping, Sequence

from .. import ledger
from ..recon.reconciler import Reconciler

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconDiff:
    kind: Literal["position", "balance"]
    venue: str
    symbol: str | None
    local: float
    remote: float
    diff_abs: float
    diff_rel: float | None


def collect_recon_snapshot(ctx: object | None = None) -> list[ReconDiff]:
    """Collect reconciliation differences for positions and balances."""

    reconciler = Reconciler()
    diffs: list[ReconDiff] = []
    try:
        position_diffs = reconciler.diff()
    except Exception:
        LOGGER.exception("collect_recon_snapshot.positions_failed")
        position_diffs = []

    for entry in position_diffs:
        venue = str(entry.get("venue") or "")
        symbol = str(entry.get("symbol") or "")
        local_qty = float(entry.get("ledger_qty") or 0.0)
        remote_qty = float(entry.get("exch_qty") or 0.0)
        delta = float(entry.get("delta") or (remote_qty - local_qty))
        notional = float(entry.get("notional_usd") or 0.0)
        abs_usd = abs(notional) if notional else abs(delta)
        rel = _relative(delta, local_qty, remote_qty)
        diffs.append(
            ReconDiff(
                kind="position",
                venue=venue,
                symbol=symbol or None,
                local=local_qty,
                remote=remote_qty,
                diff_abs=abs_usd,
                diff_rel=rel,
            )
        )

    balance_diffs = _collect_balance_diffs(ctx)
    diffs.extend(balance_diffs)
    return diffs


def _relative(delta: float, local: float, remote: float) -> float | None:
    base = max(abs(remote), abs(local), 1e-9)
    if base <= 0:
        return None
    return abs(delta) / base


def _collect_balance_diffs(ctx: object | None) -> list[ReconDiff]:
    local_rows = _resolve_local_balances(ctx)
    remote_rows = _resolve_remote_balances(ctx)
    price_lookup = _resolve_price_lookup(ctx)

    local_map = _normalise_balances(local_rows)
    remote_map = _normalise_balances(remote_rows)

    seen_keys = set(local_map) | set(remote_map)
    diffs: list[ReconDiff] = []
    for venue, asset in sorted(seen_keys):
        local_value = local_map.get((venue, asset), 0.0)
        remote_value = remote_map.get((venue, asset), 0.0)
        delta = remote_value - local_value
        if math.isclose(delta, 0.0, abs_tol=1e-9):
            continue
        multiplier = _asset_usd_multiplier(asset, price_lookup)
        diff_abs = abs(delta) * multiplier if multiplier is not None else abs(delta)
        rel = _relative(delta, local_value, remote_value)
        diffs.append(
            ReconDiff(
                kind="balance",
                venue=venue,
                symbol=asset,
                local=local_value,
                remote=remote_value,
                diff_abs=diff_abs,
                diff_rel=rel,
            )
        )
    return diffs


def _resolve_local_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    if ctx is not None:
        candidate = getattr(ctx, "local_balances", None)
        rows = _maybe_rows(candidate)
        if rows is not None:
            return rows
        candidate = getattr(ctx, "ledger", None)
        rows = _maybe_rows(getattr(candidate, "fetch_balances", None))
        if rows is not None:
            return rows
    return ledger.fetch_balances()


def _resolve_remote_balances(ctx: object | None) -> Sequence[Mapping[str, object]]:
    if ctx is not None:
        candidate = getattr(ctx, "remote_balances", None)
        rows = _maybe_rows(candidate)
        if rows is not None:
            return rows
    if ctx is not None:
        candidate = getattr(ctx, "runtime", None)
        rows = _maybe_rows(getattr(candidate, "remote_balances", None))
        if rows is not None:
            return rows
    return ledger.fetch_balances()


def _maybe_rows(candidate) -> Sequence[Mapping[str, object]] | None:
    if candidate is None:
        return None
    if callable(candidate):
        try:
            result = candidate()
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("collect_recon_snapshot.balance_source_failed")
            return None
    else:
        result = candidate
    if isinstance(result, Sequence):
        return [row for row in result if isinstance(row, Mapping)]
    return None


def _normalise_balances(rows: Sequence[Mapping[str, object]]) -> MutableMapping[tuple[str, str], float]:
    mapping: MutableMapping[tuple[str, str], float] = {}
    for row in rows:
        venue_raw = row.get("venue")
        asset_raw = row.get("asset") or row.get("currency") or row.get("symbol")
        venue = str(venue_raw or "").lower()
        asset = str(asset_raw or "").upper()
        if not venue or not asset:
            continue
        total = _extract_balance_amount(row)
        mapping[(venue, asset)] = mapping.get((venue, asset), 0.0) + total
    return mapping


def _extract_balance_amount(row: Mapping[str, object]) -> float:
    for key in ("total", "qty", "balance", "amount", "walletBalance", "equity"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    free = row.get("free") or row.get("availableBalance")
    if free not in (None, ""):
        try:
            return float(free)
        except (TypeError, ValueError):
            pass
    return 0.0


def _resolve_price_lookup(ctx: object | None):
    if ctx is None:
        return None
    candidate = getattr(ctx, "asset_prices", None)
    if isinstance(candidate, Mapping):
        return {str(k).upper(): float(v) for k, v in candidate.items() if isinstance(v, (int, float))}
    candidate = getattr(ctx, "get_asset_price", None)
    if callable(candidate):
        return candidate
    return None


def _asset_usd_multiplier(asset: str, lookup) -> float | None:
    stable = {"USD", "USDT", "USDC", "BUSD", "USDP", "DAI", "TUSD"}
    if asset in stable:
        return 1.0
    if lookup is None:
        return None
    if isinstance(lookup, Mapping):
        value = lookup.get(asset)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    try:
        price = lookup(asset)
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


__all__ = ["ReconDiff", "collect_recon_snapshot"]


__all__ = ["ReconDiff", "collect_recon_snapshot"]

