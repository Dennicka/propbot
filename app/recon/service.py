"""Collect reconciliation snapshots comparing local and remote state."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import math
from typing import Literal

from .core import reconcile_once
from .reconciler import Reconciler


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

    try:
        snapshots = reconcile_once(ctx)
    except Exception:
        LOGGER.exception("collect_recon_snapshot.failed")
        raise

    diffs: list[ReconDiff] = []
    for snapshot in snapshots:
        if snapshot.status == "OK":
            continue
        if snapshot.symbol is not None:
            kind = "position"
            local_value = float(snapshot.local_position or Decimal("0"))
            remote_value = float(snapshot.exch_position or Decimal("0"))
            delta = remote_value - local_value
        else:
            kind = "balance"
            local_value = float(snapshot.local_balance or Decimal("0"))
            remote_value = float(snapshot.exch_balance or Decimal("0"))
            delta = remote_value - local_value

        diff_abs = float(snapshot.diff_abs)
        diff_rel = _relative(delta, local_value, remote_value)
        diffs.append(
            ReconDiff(
                kind=kind,
                venue=snapshot.venue,
                symbol=snapshot.symbol if kind == "position" else snapshot.asset,
                local=local_value,
                remote=remote_value,
                diff_abs=diff_abs,
                diff_rel=diff_rel,
            )
        )
    return diffs


def _relative(delta: float, local: float, remote: float) -> float | None:
    base = max(abs(remote), abs(local), 1e-9)
    if math.isclose(base, 0.0, abs_tol=1e-12):
        return None
    return abs(delta) / base


__all__ = ["ReconDiff", "collect_recon_snapshot", "Reconciler"]

