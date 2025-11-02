"""Prometheus counters for pre-trade validation."""

from prometheus_client import Counter

PRETRADE_CHECKS_TOTAL = Counter(
    "pretrade_checks_total",
    "Count of pre-trade validation decisions",
    ("result", "reason"),
)

PRETRADE_AUTOFIX_TOTAL = Counter(
    "pretrade_autofix_total",
    "Count of automatic pre-trade adjustments",
    ("field",),
)

PRETRADE_BLOCKS_TOTAL = Counter(
    "pretrade_blocks_total",
    "Number of orders blocked by the pre-trade gate",
    ("reason",),
)
__all__ = ["PRETRADE_CHECKS_TOTAL", "PRETRADE_AUTOFIX_TOTAL", "PRETRADE_BLOCKS_TOTAL"]
