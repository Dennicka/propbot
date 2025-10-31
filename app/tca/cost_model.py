"""Comprehensive transaction cost modelling for perp routing decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, MutableMapping


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
) -> Dict[str, object]:
    """Return total expected cost in bps/usdt for the given leg."""

    side_normalised = str(side or "").strip().lower()
    if side_normalised not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell or long/short")

    notional = max(float(qty), 0.0) * max(float(px), 0.0)
    horizon_minutes = max(float(horizon_min), 0.0)
    horizon_hours = horizon_minutes / 60.0

    fee_info = _extract_fee_info(venue_meta or {})
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

    total_bps = execution_bps + funding_bps
    total_usdt = execution_usdt + funding_usdt

    breakdown = {
        "execution": {
            "mode": execution_mode,
            "bps": execution_bps,
            "usdt": execution_usdt,
            "maker_bps": maker_bps,
            "taker_bps": taker_bps,
            "vip_rebate_bps": vip_rebate_bps if execution_mode == "maker" else 0.0,
            "maker_candidate_bps": maker_candidate,
        },
        "funding": {
            "bps": funding_bps,
            "usdt": funding_usdt,
            "per_hour_bps": funding_rate_per_hour,
            "hours": horizon_hours,
        },
        "inputs": {
            "side": side_normalised,
            "qty": float(qty),
            "px": float(px),
            "notional": notional,
            "horizon_min": horizon_minutes,
            "is_maker_possible": bool(is_maker_possible),
        },
    }

    return {"bps": total_bps, "usdt": total_usdt, "breakdown": breakdown}


__all__ = ["FeeInfo", "FeeTable", "effective_cost", "funding_bps_per_hour"]
