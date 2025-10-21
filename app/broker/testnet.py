from __future__ import annotations

import os
from typing import Dict

from .paper import PaperBroker


class TestnetBroker(PaperBroker):
    """Broker wrapper that validates credentials when SAFE_MODE is disabled."""

    def __init__(self, venue: str, *, safe_mode: bool, required_env: tuple[str, ...]) -> None:
        super().__init__(venue)
        self.safe_mode = safe_mode
        self.required_env = required_env
        if not safe_mode:
            missing = [name for name in required_env if not os.getenv(name)]
            if missing:
                missing_vars = ", ".join(missing)
                raise RuntimeError(f"missing credentials for {venue}: {missing_vars}")

    async def create_order(self, *args, **kwargs) -> Dict[str, object]:  # type: ignore[override]
        if self.safe_mode:
            return await super().create_order(*args, **kwargs)
        raise RuntimeError("live testnet execution is disabled in this build")
