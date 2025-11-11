"""Helpers for working with derivative venue symbols."""

from __future__ import annotations

from typing import Iterable, Mapping


def normalise_symbol(symbol: str) -> str:
    """Return an uppercase alphanumeric key for a trading symbol."""

    cleaned = [ch for ch in str(symbol) if ch.isalnum()]
    return "".join(cleaned).upper()


def iter_symbol_aliases(symbol: str) -> Iterable[str]:
    """Yield common normalised aliases for the given symbol."""

    norm = normalise_symbol(symbol)
    if not norm:
        return []
    aliases = {norm}
    stripped = symbol.replace("-", "").replace("_", "").replace("/", "")
    aliases.add(stripped.upper())
    aliases.add(symbol.upper())
    return aliases


def _as_mapping(config: Mapping[str, object] | object | None) -> Mapping[str, object] | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config
    if hasattr(config, "model_dump"):
        try:
            dumped = config.model_dump()
        except Exception:  # pragma: no cover - defensive
            return None
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(config, "dict"):
        try:
            dumped = config.dict()
        except Exception:  # pragma: no cover - defensive
            return None
        if isinstance(dumped, Mapping):
            return dumped
    return None


def resolve_venue_symbol(
    config: Mapping[str, object] | object | None,
    *,
    venue_id: str,
    symbol: str,
) -> str | None:
    """Return the configured derivative symbol for the venue if available."""

    mapping = _as_mapping(config)
    if mapping is None:
        return None
    derivatives = mapping.get("derivatives") if isinstance(mapping, Mapping) else None
    if not isinstance(derivatives, Mapping):
        return None
    arbitrage = derivatives.get("arbitrage")
    if not isinstance(arbitrage, Mapping):
        return None
    pairs = arbitrage.get("pairs")
    if not isinstance(pairs, Iterable):
        return None
    target = normalise_symbol(symbol)
    venue_norm = normalise_symbol(venue_id)
    for entry in pairs:
        if not isinstance(entry, Mapping):
            continue
        for leg_key in ("long", "short"):
            leg = entry.get(leg_key)
            if not isinstance(leg, Mapping):
                continue
            leg_venue = leg.get("venue")
            leg_symbol = leg.get("symbol")
            if not leg_symbol or not leg_venue:
                continue
            if normalise_symbol(str(leg_venue)) != venue_norm:
                continue
            if normalise_symbol(str(leg_symbol)) == target:
                return str(leg_symbol)
    return None


def resolve_runtime_venue_id(
    config: Mapping[str, object] | object | None,
    *,
    alias: str,
) -> str | None:
    """Match a human-friendly venue alias to the configured runtime ID."""

    mapping = _as_mapping(config)
    if mapping is None:
        return None
    derivatives = mapping.get("derivatives") if isinstance(mapping, Mapping) else None
    if not isinstance(derivatives, Mapping):
        return None
    venues = derivatives.get("venues")
    if not isinstance(venues, Iterable):
        return None
    alias_norm = normalise_symbol(alias)
    for venue in venues:
        if not isinstance(venue, Mapping):
            continue
        venue_id = str(venue.get("id") or "")
        if not venue_id:
            continue
        venue_norm = normalise_symbol(venue_id)
        if alias_norm == venue_norm:
            return venue_id
        simple = venue_id.split("_")[0]
        if normalise_symbol(simple) == alias_norm:
            return venue_id
    return None


__all__ = [
    "iter_symbol_aliases",
    "normalise_symbol",
    "resolve_runtime_venue_id",
    "resolve_venue_symbol",
]
