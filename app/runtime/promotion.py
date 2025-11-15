from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence

from app.config.profile import normalise_profile_category
from app.services.runtime import get_profile

PromotionStage = Literal[
    "paper_only",
    "testnet_sandbox",
    "live_dry_run",
    "live_canary",
    "live_full",
]


@dataclass(slots=True)
class PromotionStatus:
    stage: PromotionStage
    runtime_profile: str
    is_live_profile: bool
    allowed_next_stages: Sequence[PromotionStage]
    reason: str | None = None


_STAGE_BY_NAME: dict[str, PromotionStage] = {
    "paper_only": "paper_only",
    "testnet_sandbox": "testnet_sandbox",
    "live_dry_run": "live_dry_run",
    "live_canary": "live_canary",
    "live_full": "live_full",
}

_DEFAULT_STAGE: PromotionStage = "paper_only"

_DEFAULT_TRANSITIONS: dict[PromotionStage, tuple[PromotionStage, ...]] = {
    "paper_only": ("testnet_sandbox",),
    "testnet_sandbox": ("live_dry_run",),
    "live_dry_run": ("live_canary",),
    "live_canary": ("live_full",),
    "live_full": (),
}

_EXPECTED_PROFILE_CATEGORY: dict[PromotionStage, str] = {
    "paper_only": "paper",
    "testnet_sandbox": "testnet",
    "live_dry_run": "live",
    "live_canary": "live",
    "live_full": "live",
}


def _read_setting(settings: Any | None, key: str) -> str | None:
    if settings is None:
        return os.getenv(key)

    attr_candidates = (key, key.lower())
    for attr in attr_candidates:
        if hasattr(settings, attr):
            value = getattr(settings, attr)
            if value is not None:
                return str(value)

    if isinstance(settings, Mapping):
        mapping: Mapping[str, Any] = settings
        for attr in attr_candidates:
            if attr in mapping:
                value = mapping[attr]
                if value is not None:
                    return str(value)

    return os.getenv(key)


def _resolve_stage(raw: str | None) -> tuple[PromotionStage, str | None]:
    if raw is None:
        return _DEFAULT_STAGE, None

    lowered = raw.strip().lower()
    stage = _STAGE_BY_NAME.get(lowered)
    if stage is None:
        message = f"invalid PROMOTION_STAGE value: {raw!r}; using {_DEFAULT_STAGE!r}"
        return _DEFAULT_STAGE, message
    return stage, None


def _deduplicate(values: Iterable[PromotionStage]) -> list[PromotionStage]:
    seen: set[PromotionStage] = set()
    ordered: list[PromotionStage] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _resolve_allowed_next(
    stage: PromotionStage,
    raw: str | None,
) -> tuple[list[PromotionStage], str | None]:
    if not raw:
        return list(_DEFAULT_TRANSITIONS[stage]), None

    candidates = [item.strip().lower() for item in raw.split(",") if item.strip()]
    allowed: list[PromotionStage] = []
    invalid: list[str] = []
    for candidate in candidates:
        resolved = _STAGE_BY_NAME.get(candidate)
        if resolved is None:
            invalid.append(candidate)
            continue
        allowed.append(resolved)

    if not allowed:
        allowed = list(_DEFAULT_TRANSITIONS[stage])

    reason: str | None = None
    if invalid:
        invalid_values = ", ".join(sorted(set(invalid)))
        reason = f"invalid entries in PROMOTION_ALLOWED_NEXT: {invalid_values}"
        if allowed == list(_DEFAULT_TRANSITIONS[stage]):
            reason = f"{reason}; using default transitions"

    return _deduplicate(allowed), reason


def get_promotion_status(settings: Any | None = None) -> PromotionStatus:
    stage_raw = _read_setting(settings, "PROMOTION_STAGE")
    stage, stage_reason = _resolve_stage(stage_raw)

    allowed_next_raw = _read_setting(settings, "PROMOTION_ALLOWED_NEXT")
    allowed_next, allowed_reason = _resolve_allowed_next(stage, allowed_next_raw)

    profile = get_profile()
    runtime_profile = profile.name
    is_live_profile = runtime_profile == "live"
    runtime_category = normalise_profile_category(runtime_profile)
    expected_category = _EXPECTED_PROFILE_CATEGORY[stage]

    reasons: list[str] = []
    if stage_reason:
        reasons.append(stage_reason)
    if allowed_reason:
        reasons.append(allowed_reason)
    if runtime_category != expected_category:
        reasons.append(
            "runtime profile '%s' does not match promotion stage '%s' (expected %s)"
            % (runtime_profile, stage, expected_category)
        )

    return PromotionStatus(
        stage=stage,
        runtime_profile=runtime_profile,
        is_live_profile=is_live_profile,
        allowed_next_stages=tuple(allowed_next),
        reason="; ".join(reasons) if reasons else None,
    )


__all__ = [
    "PromotionStage",
    "PromotionStatus",
    "get_promotion_status",
]
