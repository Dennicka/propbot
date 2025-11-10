"""Recovery helpers for idempotent order intents."""

from __future__ import annotations

from .runtime import get_state
from ..router.order_router import OrderRouter


async def recover_inflight_intents(router: OrderRouter) -> None:
    state = get_state()
    if getattr(state.control, "safe_mode", False):  # pragma: no cover - defensive gate
        return
    await router.recover_inflight()


__all__ = ["recover_inflight_intents"]
