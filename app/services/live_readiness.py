from __future__ import annotations

from typing import Mapping, Sequence

from ..exchange_watchdog import get_exchange_watchdog
from ..risk.core import FeatureFlags
from ..risk.daily_loss import get_daily_loss_cap
from ..services.runtime import is_hold_active
from ..universe_manager import UniverseManager


def _normalise_symbols(symbols: object) -> Sequence[str]:
    if isinstance(symbols, Sequence) and not isinstance(symbols, (str, bytes)):
        return [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    if isinstance(symbols, Mapping):
        return [
            str(symbol).strip()
            for symbol in symbols.values()
            if str(symbol).strip()
        ]
    if symbols:
        text = str(symbols).strip()
        return [text] if text else []
    return []


def _universe_has_tradeable_instruments(manager: UniverseManager | None = None) -> bool:
    manager = manager or UniverseManager()
    derivatives = getattr(manager, "_derivatives", None)
    venues: Mapping[str, object] | None = None
    if derivatives is not None:
        venues = getattr(derivatives, "venues", None)
    if not isinstance(venues, Mapping):
        return False
    for venue_id, runtime in venues.items():
        config = getattr(runtime, "config", None)
        symbols = _normalise_symbols(getattr(config, "symbols", []))
        if not symbols:
            continue
        for symbol in symbols:
            venue_symbol = manager._symbol_for_venue(venue_id, symbol)  # type: ignore[attr-defined]
            if manager._is_symbol_supported(venue_id, venue_symbol):  # type: ignore[attr-defined]
                return True
    return False


def compute_readiness() -> dict[str, object]:
    """Aggregate bot readiness for orchestrators and external monitors."""

    hold_active = is_hold_active()
    watchdog_ok = bool(get_exchange_watchdog().overall_ok())
    daily_loss_cap = get_daily_loss_cap()
    daily_loss_breached = bool(daily_loss_cap.is_breached())
    enforce_daily_loss = bool(FeatureFlags.enforce_daily_loss_cap())
    universe_loaded = _universe_has_tradeable_instruments()

    ready = (
        (not hold_active)
        and watchdog_ok
        and (not daily_loss_breached if enforce_daily_loss else True)
        and universe_loaded
    )

    reasons: list[str] = []
    if hold_active:
        reasons.append("Global HOLD is active")
    if not watchdog_ok:
        reasons.append("Exchange watchdog is reporting failures")
    if enforce_daily_loss and daily_loss_breached:
        reasons.append("Daily loss cap breached")
    if not universe_loaded:
        reasons.append("Trading universe is empty")

    return {
        "ready": ready,
        "hold": hold_active,
        "watchdog_ok": watchdog_ok,
        "daily_loss_breached": daily_loss_breached,
        "universe_loaded": universe_loaded,
        "reasons": reasons,
    }


__all__ = ["compute_readiness"]
