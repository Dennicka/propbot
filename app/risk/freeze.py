"""In-memory registry tracking active risk freeze rules."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Iterable, Literal

from ..golden.logger import get_golden_logger


ScopeLiteral = Literal["global", "venue", "symbol", "strategy"]


@dataclass(frozen=True, slots=True)
class FreezeRule:
    """Describe a freeze rule applied by the automated guards."""

    reason: str
    scope: ScopeLiteral
    ts: float


class FreezeRegistry:
    """Simple in-memory container for risk freeze rules."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: dict[str, FreezeRule] = {}

    # ------------------------------------------------------------------
    def apply(self, rule: FreezeRule) -> bool:
        """Register ``rule`` and return ``True`` when it was newly added."""

        normalised = FreezeRule(
            reason=str(rule.reason or "").strip() or "UNKNOWN_FREEZE",
            scope=rule.scope,
            ts=float(rule.ts or 0.0),
        )
        applied = False
        with self._lock:
            existing = self._rules.get(normalised.reason)
            if existing is not None and existing.scope == normalised.scope:
                if existing.ts < normalised.ts:
                    self._rules[normalised.reason] = normalised
                return False
            self._rules[normalised.reason] = normalised
            applied = True
        if applied:
            logger = get_golden_logger()
            if logger.enabled:
                logger.log(
                    "freeze_applied",
                    {"reason": normalised.reason, "scope": normalised.scope, "ts": normalised.ts},
                )
        return True

    # ------------------------------------------------------------------
    def clear(self, reason_prefix: str | None = None) -> int:
        """Remove freeze rules matching ``reason_prefix``.

        Returns the number of rules cleared.
        """

        with self._lock:
            if reason_prefix is None:
                cleared = len(self._rules)
                self._rules.clear()
                return cleared
            prefix = str(reason_prefix or "")
            if not prefix:
                cleared = len(self._rules)
                self._rules.clear()
                return cleared
            keys = [reason for reason in self._rules if reason.startswith(prefix)]
            for reason in keys:
                self._rules.pop(reason, None)
            return len(keys)

    # ------------------------------------------------------------------
    def is_frozen(
        self,
        *,
        strategy: str | None = None,
        venue: str | None = None,
        symbol: str | None = None,
    ) -> bool:
        """Return ``True`` when any freeze rule matches the provided scope."""

        with self._lock:
            rules: Iterable[FreezeRule] = tuple(self._rules.values())

        for rule in rules:
            if self._matches_rule(rule, strategy=strategy, venue=venue, symbol=symbol):
                return True
        return False

    # ------------------------------------------------------------------
    def list_rules(self) -> list[FreezeRule]:
        """Expose a copy of the active rules."""

        with self._lock:
            return sorted(self._rules.values(), key=lambda entry: entry.ts, reverse=True)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        """Return a serialisable snapshot suitable for APIs."""

        rules = self.list_rules()
        return {
            "active": bool(rules),
            "rules": [asdict(rule) for rule in rules],
        }

    # ------------------------------------------------------------------
    def _matches_rule(
        self,
        rule: FreezeRule,
        *,
        strategy: str | None,
        venue: str | None,
        symbol: str | None,
    ) -> bool:
        scope = rule.scope
        if scope == "global":
            return True
        if scope == "strategy":
            return self._match_strategy(rule.reason, strategy)
        if scope == "venue":
            return self._match_venue(rule.reason, venue)
        if scope == "symbol":
            return self._match_symbol(rule.reason, venue=venue, symbol=symbol)
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _match_strategy(reason: str, candidate: str | None) -> bool:
        if not candidate:
            return False
        expected = _extract_tag(reason, "strategy")
        if expected:
            return candidate.strip().lower() == expected.lower()
        suffix = reason.split("::")[-1]
        return suffix.strip().lower() == candidate.strip().lower()

    # ------------------------------------------------------------------
    @staticmethod
    def _match_venue(reason: str, candidate: str | None) -> bool:
        if candidate is None:
            return False
        value = candidate.strip().lower()
        if not value:
            return False
        expected = _extract_tag(reason, "venue")
        if expected:
            expected_value = expected.lower()
            return value == expected_value or value.startswith(f"{expected_value}-")
        suffix = reason.split("::")[-1]
        suffix_value = suffix.strip().lower()
        if not suffix_value:
            return False
        return value == suffix_value or value.startswith(f"{suffix_value}-")

    # ------------------------------------------------------------------
    @staticmethod
    def _match_symbol(
        reason: str,
        *,
        venue: str | None,
        symbol: str | None,
    ) -> bool:
        candidate_symbol = (symbol or "").strip().upper()
        if not candidate_symbol:
            return False
        expected_symbol = _extract_tag(reason, "symbol")
        expected_venue = _extract_tag(reason, "venue")
        if expected_symbol:
            if expected_venue and venue:
                venue_value = venue.strip().lower()
                expected_value = expected_venue.lower()
                if not (
                    venue_value == expected_value
                    or venue_value.startswith(f"{expected_value}-")
                ):
                    return False
            return candidate_symbol == expected_symbol.upper()
        suffix = reason.split("::")[-1]
        if ":" in suffix:
            venue_part, symbol_part = suffix.split(":", 1)
            if venue_part.strip() and venue:
                expected_venue = venue_part.strip().lower()
                venue_value = venue.strip().lower()
                if not (
                    venue_value == expected_venue
                    or venue_value.startswith(f"{expected_venue}-")
                ):
                    return False
            suffix_symbol = symbol_part
        else:
            suffix_symbol = suffix
        return candidate_symbol == suffix_symbol.strip().upper()


def _extract_tag(reason: str, key: str) -> str | None:
    prefix = f"{key}="
    parts = reason.split("::")
    for part in parts[1:]:
        text = part.strip()
        if not text or "=" not in text:
            continue
        if text.startswith(prefix):
            return text[len(prefix) :]
    return None


_REGISTRY = FreezeRegistry()


def get_freeze_registry() -> FreezeRegistry:
    return _REGISTRY


def reset_freeze_registry() -> None:
    _REGISTRY.clear()


__all__ = ["FreezeRule", "FreezeRegistry", "get_freeze_registry", "reset_freeze_registry"]

