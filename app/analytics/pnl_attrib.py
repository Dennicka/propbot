from __future__ import annotations

import os
from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable, Mapping, MutableMapping

from ..config import load_app_config
from ..tca.cost_model import TierInfo, TierTable

_DEFAULT_CONFIG_PATHS = {
    "paper": "configs/config.paper.yaml",
    "testnet": "configs/config.testnet.yaml",
    "live": "configs/config.live.yaml",
}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_config_path() -> str | None:
    override = os.getenv("APP_CONFIG_PATH")
    if override:
        return override
    profile = (
        os.getenv("PROFILE")
        or os.getenv("EXCHANGE_PROFILE")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
        or "paper"
    )
    return _DEFAULT_CONFIG_PATHS.get(profile.lower(), _DEFAULT_CONFIG_PATHS["paper"])


@lru_cache(maxsize=1)
def _load_tier_table() -> TierTable | None:
    config_path = _resolve_config_path()
    if not config_path:
        return None
    try:
        loaded = load_app_config(config_path)
    except Exception:
        return None
    tca_cfg = getattr(loaded.data, "tca", None)
    tiers_cfg = getattr(tca_cfg, "tiers", None) if tca_cfg else None
    if not tiers_cfg:
        return None
    serialised: dict[str, list[Mapping[str, Any]]] = {}
    for venue, entries in getattr(tiers_cfg, "items", lambda: [])():
        venue_entries: list[Mapping[str, Any]] = []
        for entry in entries or []:
            if isinstance(entry, Mapping):
                venue_entries.append(entry)
            elif hasattr(entry, "model_dump"):
                venue_entries.append(entry.model_dump())  # type: ignore[attr-defined]
            elif isinstance(entry, TierInfo):
                venue_entries.append(
                    {
                        "tier": entry.tier,
                        "maker_bps": entry.maker_bps,
                        "taker_bps": entry.taker_bps,
                        "rebate_bps": entry.rebate_bps,
                        "notional_from": entry.notional_from,
                    }
                )
        if venue_entries:
            serialised[str(venue)] = venue_entries
    if not serialised:
        return None
    return TierTable.from_mapping(serialised)


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalise_name(value: object, fallback: str) -> str:
    name = str(value or "").strip()
    return name or fallback


def _ensure_bucket(target: MutableMapping[str, float]) -> MutableMapping[str, float]:
    target.setdefault("realized", 0.0)
    target.setdefault("unrealized", 0.0)
    target.setdefault("fees", 0.0)
    target.setdefault("rebates", 0.0)
    target.setdefault("funding", 0.0)
    target.setdefault("net", 0.0)
    return target


def _pick_tier(table: TierTable | None, venue: str, rolling: float | None) -> TierInfo | None:
    if table is None or not venue:
        return None
    tier = table.pick_tier(venue, rolling)
    if tier:
        return tier
    entries = table.per_venue.get(venue)
    if entries:
        return entries[0]
    return None


def _compute_fee_components(
    *,
    tier_table: TierTable | None,
    venue: str,
    role: str,
    notional: float,
    fee_override: float | None,
    rebate_override: float | None,
    rolling_notional: float | None,
) -> tuple[float, float]:
    fee_value = 0.0 if fee_override is None else _coerce_float(fee_override)
    rebate_value = 0.0 if rebate_override is None else _coerce_float(rebate_override)
    if fee_override is not None or rebate_override is not None:
        return fee_value, rebate_value
    if notional <= 0.0:
        return 0.0, 0.0
    tier = _pick_tier(tier_table, venue, rolling_notional)
    if tier is None:
        return 0.0, 0.0
    role_normalised = str(role or "").strip().lower()
    if role_normalised in {"maker", "post_only", "maker_only"}:
        fee_bps = tier.maker_bps
    elif role_normalised in {"vip", "taker"}:
        fee_bps = tier.taker_bps
    else:
        fee_bps = tier.taker_bps
    rebate_bps = tier.rebate_bps
    fee_amount = notional * fee_bps / 10_000.0
    maker_roles = {"maker", "post_only", "maker_only"}
    rebate_amount = notional * rebate_bps / 10_000.0 if role_normalised in maker_roles else 0.0
    return fee_amount, rebate_amount


def _iter_event_entries(payload: object) -> Iterable[Mapping[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        if all(isinstance(value, Mapping) for value in payload.values()):
            return [dict(value, name=key) for key, value in payload.items() if isinstance(value, Mapping)]
        return [dict(payload)]
    if isinstance(payload, (list, tuple, set)):
        entries: list[Mapping[str, Any]] = []
        for item in payload:
            if isinstance(item, Mapping):
                entries.append(dict(item))
        return entries
    return []


def calc_attribution(
    trades: Iterable[Mapping[str, Any]] | None,
    fees: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    rebates: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    funding_events: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Aggregate realised/unrealised PnL with fees, rebates and funding adjustments."""

    exclude_simulated = _env_flag("EXCLUDE_DRY_RUN_FROM_PNL", True)
    tier_table = _load_tier_table()

    strategy_buckets: dict[str, MutableMapping[str, float]] = defaultdict(dict)
    venue_buckets: dict[str, MutableMapping[str, float]] = defaultdict(dict)
    totals: MutableMapping[str, float] = {}
    _ensure_bucket(totals)

    def apply(
        strategy: str,
        venue: str,
        *,
        realized: float = 0.0,
        unrealized: float = 0.0,
        fee: float = 0.0,
        rebate: float = 0.0,
        funding: float = 0.0,
    ) -> None:
        strategy_bucket = _ensure_bucket(strategy_buckets[strategy])
        venue_bucket = _ensure_bucket(venue_buckets[venue])
        for bucket in (strategy_bucket, venue_bucket, totals):
            bucket["realized"] += realized
            bucket["unrealized"] += unrealized
            bucket["fees"] += fee
            bucket["rebates"] += rebate
            bucket["funding"] += funding
            bucket["net"] = (
                bucket["realized"]
                + bucket["unrealized"]
                - bucket["fees"]
                + bucket["rebates"]
                + bucket["funding"]
            )

    for trade in trades or []:
        if not isinstance(trade, Mapping):
            continue
        simulated = bool(trade.get("simulated")) or bool(trade.get("dry_run"))
        if exclude_simulated and simulated:
            continue
        strategy = _normalise_name(trade.get("strategy"), "unknown")
        venue = _normalise_name(trade.get("venue"), "unknown")
        realized = _coerce_float(
            trade.get("realized")
            or trade.get("realized_pnl")
            or trade.get("realized_pnl_usd")
            or trade.get("realized_pnl_usdt")
        )
        unrealized = _coerce_float(
            trade.get("unrealized")
            or trade.get("unrealized_pnl")
            or trade.get("unrealized_pnl_usd")
            or trade.get("unrealized_pnl_usdt")
        )
        notional = abs(
            _coerce_float(
                trade.get("notional")
                or trade.get("notional_usd")
                or trade.get("notional_usdt")
                or trade.get("qty_notional")
            )
        )
        fee_override = trade.get("fee")
        rebate_override = trade.get("rebate")
        role = trade.get("liquidity") or trade.get("role") or trade.get("execution") or trade.get("side")
        rolling = trade.get("rolling_30d_notional")
        fee_amount, rebate_amount = _compute_fee_components(
            tier_table=tier_table,
            venue=venue,
            role=str(role or ""),
            notional=notional,
            fee_override=fee_override if fee_override is not None else None,
            rebate_override=rebate_override if rebate_override is not None else None,
            rolling_notional=_coerce_float(rolling) if rolling is not None else None,
        )
        if fee_override is not None:
            fee_amount = _coerce_float(fee_override)
        if rebate_override is not None:
            rebate_amount = _coerce_float(rebate_override)
        apply(
            strategy,
            venue,
            realized=realized,
            unrealized=unrealized,
            fee=fee_amount,
            rebate=rebate_amount,
        )

    for entry in _iter_event_entries(fees):
        simulated = bool(entry.get("simulated")) or bool(entry.get("dry_run"))
        if exclude_simulated and simulated:
            continue
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        if amount >= 0.0:
            apply(strategy, venue, fee=amount)
        else:
            apply(strategy, venue, rebate=-amount)

    for entry in _iter_event_entries(rebates):
        simulated = bool(entry.get("simulated")) or bool(entry.get("dry_run"))
        if exclude_simulated and simulated:
            continue
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        if amount >= 0.0:
            apply(strategy, venue, rebate=amount)
        else:
            apply(strategy, venue, fee=-amount)

    for entry in _iter_event_entries(funding_events):
        simulated = bool(entry.get("simulated")) or bool(entry.get("dry_run"))
        if exclude_simulated and simulated:
            continue
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        apply(strategy, venue, funding=amount)

    def _sorted_payload(source: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
        serialised: dict[str, dict[str, float]] = {}
        for key in sorted(source):
            bucket = source[key]
            serialised[key] = {
                "realized": float(bucket.get("realized", 0.0)),
                "unrealized": float(bucket.get("unrealized", 0.0)),
                "fees": float(bucket.get("fees", 0.0)),
                "rebates": float(bucket.get("rebates", 0.0)),
                "funding": float(bucket.get("funding", 0.0)),
                "net": float(bucket.get("net", 0.0)),
            }
        return serialised

    result_totals = {
        "realized": float(totals.get("realized", 0.0)),
        "unrealized": float(totals.get("unrealized", 0.0)),
        "fees": float(totals.get("fees", 0.0)),
        "rebates": float(totals.get("rebates", 0.0)),
        "funding": float(totals.get("funding", 0.0)),
        "net": float(totals.get("net", 0.0)),
    }

    meta = {
        "exclude_simulated": exclude_simulated,
        "tier_table_loaded": tier_table is not None,
    }

    return {
        "by_strategy": _sorted_payload(strategy_buckets),
        "by_venue": _sorted_payload(venue_buckets),
        "totals": result_totals,
        "meta": meta,
    }


__all__ = ["calc_attribution"]
