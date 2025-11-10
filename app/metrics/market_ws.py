"""Prometheus counters specific to websocket market data resilience."""

from __future__ import annotations

import logging
import threading

from prometheus_client import Counter

__all__ = [
    "WS_CONNECT_TOTAL",
    "WS_DISCONNECT_TOTAL",
    "WS_GAP_DETECTED_TOTAL",
    "WS_RESYNC_TOTAL",
    "reset_for_tests",
]


LOGGER = logging.getLogger(__name__)


_LOCK = threading.Lock()
_WS_METRICS_INITIALISED = False


WS_CONNECT_TOTAL: Counter | None = None
WS_DISCONNECT_TOTAL: Counter | None = None
WS_GAP_DETECTED_TOTAL: Counter | None = None
WS_RESYNC_TOTAL: Counter | None = None


def _ensure_metrics() -> None:
    global _WS_METRICS_INITIALISED, WS_CONNECT_TOTAL, WS_DISCONNECT_TOTAL, WS_GAP_DETECTED_TOTAL, WS_RESYNC_TOTAL
    if _WS_METRICS_INITIALISED:
        return
    with _LOCK:
        if _WS_METRICS_INITIALISED:
            return
        WS_CONNECT_TOTAL = Counter(
            "ws_connect_total",
            "Total websocket connections established by venue.",
            ("venue",),
        )
        WS_DISCONNECT_TOTAL = Counter(
            "ws_disconnect_total",
            "Total websocket disconnects by venue and reason.",
            ("venue", "reason"),
        )
        WS_GAP_DETECTED_TOTAL = Counter(
            "ws_gap_detected_total",
            "Total detected websocket diff gaps by venue and symbol.",
            ("venue", "symbol"),
        )
        WS_RESYNC_TOTAL = Counter(
            "ws_resync_total",
            "Total websocket resync operations triggered by venue and symbol.",
            ("venue", "symbol"),
        )
        _WS_METRICS_INITIALISED = True


_ensure_metrics()


def reset_for_tests() -> None:  # pragma: no cover - best effort cleanup
    _ensure_metrics()
    for metric in (
        WS_CONNECT_TOTAL,
        WS_DISCONNECT_TOTAL,
        WS_GAP_DETECTED_TOTAL,
        WS_RESYNC_TOTAL,
    ):
        if metric is None:
            continue
        try:
            metric._metrics.clear()  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.debug("failed to reset ws metric collector=%s error=%s", metric, exc)
