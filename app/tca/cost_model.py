"""Comprehensive transaction cost modelling for perp routing decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, MutableMapping, Sequence


@dataclass
class FeeInfo:
    """Per-venue fee configuration in basis points."""

    maker_bps: float = 0.0
    taker_bps: float = 0.0
    vip_rebate_bps: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "maker_bps": float(self.maker_bps),
            "taker_bps": float(self.taker_bps),
            "vip_rebate_bps": float(self.vip_rebate_bps),
        }


@dataclass
class FeeTable:
    """Container for per-venue fee metadata."""

    per_venue: MutableMapping[str, FeeInfo] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Mapping[str, float] | FeeInfo]) -> "FeeTable":
        table = cls()
        for venue, payload in mapping.items():
            if isinstance(payload, FeeInfo):
                table.per_venue[str(venue)] = payload
                continue
            if not isinstance(payload, Mapping):
                continue
            table.per_venue[str(venue)] = FeeInfo(
                maker_bps=float(payload.get("maker_bps", 0.0)),
                taker_bps=float(payload.get("taker_bps", 0.0)),
                vip_rebate_bps=float(payload.get("vip_rebate_bps", 0.0)),
            )
        return table

    def merge(self, mapping: Mapping[str, Mapping[str, float] | FeeInfo]) -> None:
        """Merge fee information for additional venues."""

        merged = self.per_venue
        for venue, payload in mapping.items():
            info = self.per_venue.get(str(venue), FeeInfo())
            if isinstance(payload, FeeInfo):
                info = payload
            elif isinstance(payload, Mapping):
                info = FeeInfo(
                    maker_bps=float(payload.get("maker_bps", info.maker_bps)),
                    taker_bps=float(payload.get("taker_bps", info.taker_bps)),
                    vip_rebate_bps=float(payload.get("vip_rebate_bps", info.vip_rebate_bps)),
                )
            merged[str(venue)] = info

    def get(self, venue: str) -> FeeInfo:
        return self.per_venue.get(str(venue), FeeInfo())

    def as_dict(self) -> Dict[str, Dict[str, float]]:
        return {venue: info.as_dict() for venue, info in self.per_venue.items()}


@dataclass(frozen=True)
class TierInfo:
    """Tier metadata loaded from configuration."""

    tier: str
    maker_bps: float
    taker_bps: float
    rebate_bps: float
    notional_from: float = 0.0


@dataclass
class TierTable:
    """Container for per-venue tier configuration."""

    per_venue: MutableMapping[str, Sequence[TierInfo]] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, Iterable[Mapping[str, float | str]] | Iterable[TierInfo]]
    ) -> "TierTable":
        table = cls()
        for venue, tiers in mapping.items():
            tier_entries: list[TierInfo] = []
            for payload in tiers or []:
                if isinstance(payload, TierInfo):
                    tier_entries.append(payload)
                    continue
                if not isinstance(payload, Mapping):
                    continue
                try:
                    tier_entries.append(
                        TierInfo(
                            tier=str(payload.get("tier", "")).strip() or "default",
                            maker_bps=float(payload.get("maker_bps", 0.0)),
                            taker_bps=float(payload.get("taker_bps", 0.0)),
                            rebate_bps=float(
                                payload.get("rebate_bps", payload.get("vip_rebate_bps", 0.0))
                            ),
                            notional_from=float(payload.get("notional_from", 0.0)),
                        )
                    )
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    continue
            tier_entries.sort(key=lambda item: item.notional_from)
            if tier_entries:
                table.per_venue[str(venue)] = tier_entries
        return table

    def pick_tier(
        self, venue: str, rolling_30d_notional: float | None
    ) -> TierInfo | None:
        tiers = self.per_venue.get(str(venue))
        if not tiers or rolling_30d_notional is None:
            return None
        notional_value = max(float(rolling_30d_notional), 0.0)
        candidate = tiers[0]
        for tier in tiers:
            if notional_value >= tier.notional_from:
                candidate = tier
            else:
                break
        return candidate


@dataclass
class ImpactModel:
    """Linear-quadratic impact estimator in basis points."""

    k: float = 0.0
    min_liquidity_usdt: float = 1e-9

    def impact_bps(
        self, qty: float, book_liquidity_usdt: float | None, k: float | None = None
    ) -> float:
        liquidity = float(book_liquidity_usdt or 0.0)
        if liquidity <= self.min_liquidity_usdt:
            return 0.0
        notional = max(float(qty), 0.0)
        if notional <= 0.0:
            return 0.0
        scale = max(float(k if k is not None else self.k), 0.0)
        if scale <= 0.0:
            return 0.0
        ratio = min(notional / liquidity, 10.0)
        impact = scale * (ratio + ratio * ratio)
        return max(float(impact), 0.0)


FUNDING_INTERVAL_HOURS = 8.0


def funding_bps_per_hour(rate: float, *, interval_hours: float = FUNDING_INTERVAL_HOURS) -> float:
    """Convert funding rate (per interval) into bps per hour."""

    hours = float(interval_hours) if interval_hours else FUNDING_INTERVAL_HOURS
    hours = hours if hours > 0 else FUNDING_INTERVAL_HOURS
    return float(rate) * 10_000.0 / hours


def _extract_fee_info(venue_meta: Mapping[str, object]) -> FeeInfo:
    if not venue_meta:
        return FeeInfo()
    fees_payload = venue_meta.get("fees")
    if isinstance(fees_payload, FeeInfo):
        return fees_payload
    if isinstance(fees_payload, Mapping):
        return FeeInfo(
            maker_bps=float(fees_payload.get("maker_bps", 0.0)),
            taker_bps=float(fees_payload.get("taker_bps", 0.0)),
            vip_rebate_bps=float(fees_payload.get("vip_rebate_bps", 0.0)),
        )
    fee_table = venue_meta.get("fee_table")
    venue_name = str(venue_meta.get("venue") or venue_meta.get("id") or "")
    if isinstance(fee_table, FeeTable) and venue_name:
        return fee_table.get(venue_name)
    return FeeInfo()


def effective_cost(
    side: str,
    qty: float,
    px: float,
    horizon_min: float,
    is_maker_possible: bool,
    venue_meta: Mapping[str, object] | None,
    *,
    tier_table: TierTable | None = None,
    rolling_30d_notional: float | None = None,
    impact_model: ImpactModel | None = None,
    book_liquidity_usdt: float | Mapping[str, float] | None = None,
) -> Dict[str, object]:
    """Return total expected cost in bps/usdt for the given leg."""

    side_normalised = str(side or "").strip().lower()
    if side_normalised not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell or long/short")

    notional = max(float(qty), 0.0) * max(float(px), 0.0)
    horizon_minutes = max(float(horizon_min), 0.0)
    horizon_hours = horizon_minutes / 60.0

    venue_meta_payload = venue_meta or {}
    venue_name = str(venue_meta_payload.get("venue") or venue_meta_payload.get("id") or "")
    fee_info = _extract_fee_info(venue_meta_payload)
    tier_info = None
    book_liquidity_value: float | None = None
    if isinstance(book_liquidity_usdt, Mapping):
        try:
            book_liquidity_value = float(book_liquidity_usdt.get(venue_name))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            book_liquidity_value = None
    elif book_liquidity_usdt is not None:
        book_liquidity_value = float(book_liquidity_usdt)
    if tier_table is not None and venue_name:
        tier_info = tier_table.pick_tier(venue_name, rolling_30d_notional)
        if tier_info is not None:
            fee_info = FeeInfo(
                maker_bps=float(tier_info.maker_bps),
                taker_bps=float(tier_info.taker_bps),
                vip_rebate_bps=float(tier_info.rebate_bps),
            )

    maker_bps = float(fee_info.maker_bps)
    taker_bps = float(fee_info.taker_bps)
    vip_rebate_bps = float(fee_info.vip_rebate_bps)

    execution_mode = "taker"
    execution_bps = taker_bps
    maker_candidate = maker_bps - vip_rebate_bps
    if bool(is_maker_possible):
        if maker_candidate <= execution_bps:
            execution_mode = "maker"
            execution_bps = maker_candidate

    execution_usdt = notional * execution_bps / 10_000.0

    funding_rate_per_hour = float((venue_meta or {}).get("funding_bps_per_hour", 0.0))
    funding_bps = funding_rate_per_hour * horizon_hours
    if side_normalised in {"sell", "short"}:
        funding_bps = -funding_bps
    funding_usdt = notional * funding_bps / 10_000.0

    impact_model_obj = impact_model if isinstance(impact_model, ImpactModel) else None
    impact_bps = 0.0
    if impact_model_obj is not None:
        impact_bps = impact_model_obj.impact_bps(notional, book_liquidity_value)
    impact_usdt = notional * impact_bps / 10_000.0

    total_bps = execution_bps + funding_bps + impact_bps
    total_usdt = execution_usdt + funding_usdt + impact_usdt

    breakdown = {
        "execution": {
            "mode": execution_mode,
            "bps": execution_bps,
            "usdt": execution_usdt,
            "maker_bps": maker_bps,
            "taker_bps": taker_bps,
            "vip_rebate_bps": vip_rebate_bps if execution_mode == "maker" else 0.0,
            "maker_candidate_bps": maker_candidate,
            "tier": tier_info.tier if tier_info else "",
        },
        "funding": {
            "bps": funding_bps,
            "usdt": funding_usdt,
            "per_hour_bps": funding_rate_per_hour,
            "hours": horizon_hours,
        },
        "impact": {
            "bps": impact_bps,
            "usdt": impact_usdt,
            "book_liquidity_usdt": book_liquidity_value,
            "k": impact_model_obj.k if impact_model_obj else 0.0,
        },
        "inputs": {
            "side": side_normalised,
            "qty": float(qty),
            "px": float(px),
            "notional": notional,
            "horizon_min": horizon_minutes,
            "is_maker_possible": bool(is_maker_possible),
        },
        "tier": tier_info.tier if tier_info else "",
        "impact_bps": impact_bps,
        "impact_usdt": impact_usdt,
    }

    return {"bps": total_bps, "usdt": total_usdt, "breakdown": breakdown}


__all__ = [
    "FeeInfo",
    "FeeTable",
    "TierInfo",
    "TierTable",
    "ImpactModel",
    "effective_cost",
    "funding_bps_per_hour",
]
