from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, MutableMapping
from decimal import Decimal, InvalidOperation, getcontext
from functools import lru_cache
from typing import Any

from ..config import load_app_config
from ..risk.core import FeatureFlags
from ..tca.cost_model import TierInfo, TierTable

_DEFAULT_CONFIG_PATHS = {
    "paper": "configs/config.paper.yaml",
    "testnet": "configs/config.testnet.yaml",
    "live": "configs/config.live.yaml",
}


getcontext().prec = 28

_LOGGER = logging.getLogger(__name__)


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


@lru_cache(maxsize=None)
def _load_tier_table_for_path(config_path: str | None) -> TierTable | None:
    if not config_path:
        return None
    try:
        loaded = load_app_config(config_path)
    except Exception as exc:
        _LOGGER.warning("tca.config.load_failed", extra={"path": config_path, "reason": str(exc)})
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


def _load_tier_table() -> TierTable | None:
    config_path = _resolve_config_path()
    return _load_tier_table_for_path(config_path)


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        try:
            return Decimal(str(float(value)))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")


def _normalise_name(value: object, fallback: str) -> str:
    name = str(value or "").strip()
    return name or fallback


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
            return [
                dict(value, name=key)
                for key, value in payload.items()
                if isinstance(value, Mapping)
            ]
        return [dict(payload)]
    if isinstance(payload, list | tuple | set):
        entries: list[Mapping[str, Any]] = []
        for item in payload:
            if isinstance(item, Mapping):
                entries.append(dict(item))
        return entries
    return []


def _filter_sim(entries: Iterable[Mapping[str, Any]] | None, exclude: bool) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    if entries is None:
        return filtered
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        simulated = bool(entry.get("simulated")) or bool(entry.get("dry_run"))
        if exclude and simulated:
            continue
        filtered.append(dict(entry))
    return filtered


def _initial_bucket() -> dict[str, Decimal]:
    return {
        "realized": Decimal("0"),
        "unrealized": Decimal("0"),
        "fees": Decimal("0"),
        "rebates": Decimal("0"),
        "funding": Decimal("0"),
        "net": Decimal("0"),
    }


def _format_decimal(value: Decimal) -> float:
    as_float = float(value)
    if abs(as_float) <= 1e-12:
        return 0.0
    return float(round(as_float, 12))


def _finalise_bucket(bucket: MutableMapping[str, Decimal]) -> None:
    bucket["net"] = (
        bucket["realized"]
        + bucket["unrealized"]
        + bucket["fees"]
        + bucket["rebates"]
        + bucket["funding"]
    )


def _serialise(
    container: Mapping[str, MutableMapping[str, Decimal]]
) -> dict[str, dict[str, float]]:
    serialised: dict[str, dict[str, float]] = {}
    for key in sorted(container):
        bucket = container[key]
        _finalise_bucket(bucket)
        serialised[key] = {
            "realized": _format_decimal(bucket["realized"]),
            "unrealized": _format_decimal(bucket["unrealized"]),
            "fees": _format_decimal(bucket["fees"]),
            "rebates": _format_decimal(bucket["rebates"]),
            "funding": _format_decimal(bucket["funding"]),
            "net": _format_decimal(bucket["net"]),
        }
    return serialised


def calc_attribution(
    trades: Iterable[Mapping[str, Any]] | None,
    fees: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    rebates: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    funding_events: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    exclude_sim: bool | None = None,
) -> dict[str, Any]:
    """Aggregate realised/unrealised PnL with fees, rebates and funding adjustments."""

    if exclude_sim is None:
        try:
            exclude_sim = FeatureFlags.exclude_dry_run_from_pnl()
        except Exception as exc:
            _LOGGER.debug(
                "failed to resolve feature flag for attribution",
                extra={"error": str(exc)},
            )
            exclude_sim = _env_flag("EXCLUDE_DRY_RUN_FROM_PNL", True)
    exclude_simulated = bool(exclude_sim)
    tier_table = _load_tier_table()

    trades_filtered = _filter_sim(
        (dict(entry) for entry in trades or [] if isinstance(entry, Mapping)), exclude_simulated
    )
    fees_filtered = _filter_sim(list(_iter_event_entries(fees)), exclude_simulated)
    rebates_filtered = _filter_sim(list(_iter_event_entries(rebates)), exclude_simulated)
    funding_filtered = _filter_sim(list(_iter_event_entries(funding_events)), exclude_simulated)

    strategy_buckets = defaultdict(_initial_bucket)
    venue_buckets = defaultdict(_initial_bucket)
    totals: MutableMapping[str, Decimal] = _initial_bucket()

    def apply(
        strategy: str,
        venue: str,
        *,
        realized: Decimal = Decimal("0"),
        unrealized: Decimal = Decimal("0"),
        fees_value: Decimal = Decimal("0"),
        rebate_value: Decimal = Decimal("0"),
        funding_value: Decimal = Decimal("0"),
    ) -> None:
        strategy_bucket = strategy_buckets[strategy]
        venue_bucket = venue_buckets[venue]
        for bucket in (strategy_bucket, venue_bucket, totals):
            bucket["realized"] += realized
            bucket["unrealized"] += unrealized
            bucket["fees"] += fees_value
            bucket["rebates"] += rebate_value
            bucket["funding"] += funding_value

    for trade in trades_filtered:
        if not isinstance(trade, Mapping):
            continue
        strategy = _normalise_name(trade.get("strategy"), "unknown")
        venue = _normalise_name(trade.get("venue"), "unknown")
        realized = _to_decimal(
            trade.get("realized")
            or trade.get("realized_pnl")
            or trade.get("realized_pnl_usd")
            or trade.get("realized_pnl_usdt")
        )
        unrealized = _to_decimal(
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
        role = (
            trade.get("liquidity")
            or trade.get("role")
            or trade.get("execution")
            or trade.get("side")
        )
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
            fees_value=-_to_decimal(fee_amount),
            rebate_value=_to_decimal(rebate_amount),
        )

    for entry in fees_filtered:
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        if amount >= 0.0:
            apply(strategy, venue, fees_value=-_to_decimal(amount))
        else:
            apply(strategy, venue, rebate_value=_to_decimal(-amount))

    for entry in rebates_filtered:
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        if amount >= 0.0:
            apply(strategy, venue, rebate_value=_to_decimal(amount))
        else:
            apply(strategy, venue, fees_value=-_to_decimal(-amount))

    for entry in funding_filtered:
        amount = _coerce_float(entry.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(entry.get("strategy"), "unknown")
        venue = _normalise_name(entry.get("venue"), "unknown")
        apply(strategy, venue, funding_value=_to_decimal(amount))

    _finalise_bucket(totals)

    by_strategy = _serialise(strategy_buckets)
    by_venue = _serialise(venue_buckets)
    result_totals = {
        "realized": _format_decimal(totals["realized"]),
        "unrealized": _format_decimal(totals["unrealized"]),
        "fees": _format_decimal(totals["fees"]),
        "rebates": _format_decimal(totals["rebates"]),
        "funding": _format_decimal(totals["funding"]),
        "net": _format_decimal(totals["net"]),
    }

    if _env_flag("PNL_ATTRIB_DEBUG", False) and by_strategy:
        sample_name = next(iter(sorted(by_strategy)))
        sample_bucket = by_strategy[sample_name]
        _LOGGER.info(
            "PnL attribution debug sample=%s realized=%s unrealized=%s fees=%s rebates=%s funding=%s net=%s",
            sample_name,
            sample_bucket["realized"],
            sample_bucket["unrealized"],
            sample_bucket["fees"],
            sample_bucket["rebates"],
            sample_bucket["funding"],
            sample_bucket["net"],
        )

    meta = {
        "exclude_simulated": exclude_simulated,
        "tier_table_loaded": tier_table is not None,
    }

    return {
        "by_strategy": by_strategy,
        "by_venue": by_venue,
        "totals": result_totals,
        "meta": meta,
        "simulated_excluded": exclude_simulated,
    }


__all__ = ["calc_attribution"]
