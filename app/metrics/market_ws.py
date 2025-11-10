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


WS_CONNECT_TOTAL: Counter
WS_DISCONNECT_TOTAL: Counter
WS_GAP_DETECTED_TOTAL: Counter
WS_RESYNC_TOTAL: Counter


def _ensure_metrics() -> None:
    global _WS_METRICS_INITIALISED
    if _WS_METRICS_INITIALISED:
        return
    with _LOCK:
        if _WS_METRICS_INITIALISED:
            return
        globals()["WS_CONNECT_TOTAL"] = Counter(
            "ws_connect_total",
            "Total websocket connections established by venue.",
            ("venue",),
        )
        globals()["WS_DISCONNECT_TOTAL"] = Counter(
            "ws_disconnect_total",
            "Total websocket disconnects by venue and reason.",
            ("venue", "reason"),
        )
        globals()["WS_GAP_DETECTED_TOTAL"] = Counter(
            "ws_gap_detected_total",
            "Total detected websocket diff gaps by venue and symbol.",
            ("venue", "symbol"),
        )
        globals()["WS_RESYNC_TOTAL"] = Counter(
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
        try:
            metric._metrics.clear()  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.debug("failed to reset ws metric collector=%s error=%s", metric, exc)
