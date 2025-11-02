from __future__ import annotations

"""Pre-trade gate wiring the risk governor into order submission."""

import logging
from typing import Mapping, MutableMapping, Optional

from ..risk.risk_governor import (
    RiskDecision,
    evaluate_pre_trade,
    get_pretrade_risk_governor,
)
from ..rules.pretrade import PretradeValidationError, get_pretrade_validator
from ..services.runtime import HoldActiveError

LOGGER = logging.getLogger(__name__)


class PreTradeThrottled(HoldActiveError):
    """Raised when the risk governor requests throttling."""

    def __init__(self, reason: str, *, decision: Optional[RiskDecision] = None) -> None:
        super().__init__(reason or "RISK_THROTTLED")
        self.decision = decision


class PreTradeRejected(HoldActiveError):
    """Raised when the pre-trade validator blocks an order."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason or "PRETRADE_INVALID")


def enforce_pre_trade(
    venue: str | None,
    order: Mapping[str, object] | None = None,
) -> RiskDecision:
    """Evaluate the risk governor before submitting an order."""

    validator = get_pretrade_validator()
    try:
        ok, reason, fixed = validator.validate(order or {})
    except PretradeValidationError as exc:  # pragma: no cover - defensive path
        LOGGER.warning(
            "pre-trade validator raised error",
            extra={"reason": exc.reason, "venue": venue},
        )
        raise PreTradeRejected(exc.reason or "PRETRADE_INVALID") from exc
    if not ok:
        LOGGER.warning(
            "pre-trade validator blocked order",
            extra={"reason": reason or "PRETRADE_INVALID", "venue": venue},
        )
        raise PreTradeRejected(reason or "PRETRADE_INVALID")
    if fixed and isinstance(order, MutableMapping):
        order.update(fixed)

    governor = get_pretrade_risk_governor()
    ok, reason = governor.check_and_account(None, order)
    if not ok:
        LOGGER.warning(
            "pre-trade governor blocked order", extra={"reason": reason or "RISK_THROTTLED", "venue": venue}
        )
        raise PreTradeThrottled(reason or "RISK_THROTTLED")

    decision = evaluate_pre_trade(venue=venue)
    if decision.auto_hold_reason:
        LOGGER.warning(
            "risk governor triggered auto-hold", extra={"reason": decision.auto_hold_reason, "venue": venue}
        )
    if decision.throttled:
        LOGGER.warning(
            "pre-trade gate blocked order due to risk governor",
            extra={
                "reason": decision.reason or "RISK_THROTTLED",
                "venue": venue,
                "success_rate": f"{decision.success_rate:.5f}",
                "error_rate": f"{decision.error_rate:.5f}",
            },
        )
        raise PreTradeThrottled(decision.reason or "RISK_THROTTLED", decision=decision)
    return decision


__all__ = ["PreTradeRejected", "PreTradeThrottled", "enforce_pre_trade"]
