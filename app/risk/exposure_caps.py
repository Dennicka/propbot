"""Exposure cap helpers for pre-trade validation and telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Tuple

from prometheus_client import Gauge

from .. import ledger


__all__ = [
    "ExposureCapsSnapshot",
    "collect_snapshot",
    "resolve_caps",
    "check_open_allowed",
    "build_status_payload",
    "snapshot_entry",
    "project_exposure",
    "EXPOSURE_CURRENT_ABS",
    "EXPOSURE_CAP_GLOBAL",
    "EXPOSURE_CAP_SIDE",
    "EXPOSURE_CAP_VENUE",
]


EXPOSURE_CURRENT_ABS = Gauge(
    "propbot_exposure_current_abs",
    "Current absolute exposure per symbol/side/venue.",
    ("symbol", "side", "venue"),
)
EXPOSURE_CAP_GLOBAL = Gauge(
    "propbot_exposure_cap_global",
    "Configured global absolute exposure cap per symbol.",
    ("symbol",),
)
EXPOSURE_CAP_SIDE = Gauge(
    "propbot_exposure_cap_side",
    "Configured per-side absolute exposure cap per symbol.",
    ("symbol", "side"),
)
EXPOSURE_CAP_VENUE = Gauge(
    "propbot_exposure_cap_venue",
    "Configured per-venue absolute exposure cap per symbol.",
    ("symbol", "venue"),
)

# Initialise metric series to known labels to avoid missing-time-series alerts.
for _symbol in ("UNKNOWN",):
    EXPOSURE_CAP_GLOBAL.labels(symbol=_symbol).set(0.0)
    for _side in ("LONG", "SHORT"):
        EXPOSURE_CAP_SIDE.labels(symbol=_symbol, side=_side).set(0.0)
        EXPOSURE_CURRENT_ABS.labels(symbol=_symbol, side=_side, venue="unknown").set(0.0)
    EXPOSURE_CAP_VENUE.labels(symbol=_symbol, venue="unknown").set(0.0)


@dataclass
class ExposureCapsSnapshot:
    """In-memory snapshot of current exposures grouped by scopes."""

    by_symbol: Dict[str, float] = field(default_factory=dict)
    by_symbol_side: Dict[Tuple[str, str], float] = field(default_factory=dict)
    by_venue_symbol: Dict[Tuple[str, str], Dict[str, float]] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_PREVIOUS_EXPOSURE_LABELS: set[Tuple[str, str, str]] = set()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_to_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            payload = dump()
        except Exception:  # pragma: no cover - defensive
            payload = {}
        if isinstance(payload, Mapping):
            return {str(k): v for k, v in payload.items()}
    return {}


def _normalise_symbol(symbol: Any) -> str:
    text = str(symbol or "").upper()
    if not text:
        return ""
    return text.replace("-", "").replace("_", "").replace("/", "")


def _normalise_venue(venue: Any) -> str:
    text = str(venue or "").lower()
    if not text:
        return ""
    return "".join(ch for ch in text if ch.isalnum())


def _normalise_side(side: Any) -> str | None:
    text = str(side or "").upper()
    if not text:
        return None
    if text in {"LONG", "BUY"}:
        return "LONG"
    if text in {"SHORT", "SELL"}:
        return "SHORT"
    return None


def _caps_entry(value: Any) -> Dict[str, Any]:
    mapping = _model_to_mapping(value)
    result: Dict[str, Any] = {
        "max_abs_usdt": _coerce_optional_float(mapping.get("max_abs_usdt")),
        "per_side_max_abs_usdt": {},
    }
    per_side = _model_to_mapping(mapping.get("per_side_max_abs_usdt"))
    for key, raw in per_side.items():
        side = _normalise_side(key)
        if not side:
            continue
        limit = _coerce_optional_float(raw)
        if limit is not None:
            result["per_side_max_abs_usdt"][side] = limit
    return result


def _extract_caps_root(cfg: Any) -> Any:
    if cfg is None:
        return None
    exposure_cfg = getattr(cfg, "exposure_caps", None)
    if exposure_cfg is not None:
        return exposure_cfg
    return cfg


def _iter_mapping_items(mapping: Any) -> Iterable[Tuple[str, Any]]:
    data = _model_to_mapping(mapping)
    for key, value in data.items():
        yield key, value


def collect_snapshot(
    positions: Iterable[Mapping[str, Any]] | None = None,
) -> ExposureCapsSnapshot:
    """Collect the current exposure snapshot from the ledger."""

    if positions is None:
        try:
            positions = ledger.fetch_positions()
        except Exception:  # pragma: no cover - defensive fetch
            positions = []
    snapshot = ExposureCapsSnapshot()
    for row in positions:
        if not isinstance(row, Mapping):
            continue
        raw_symbol = str(row.get("symbol") or "").upper()
        if not raw_symbol:
            continue
        raw_venue = str(row.get("venue") or "")
        symbol_key = _normalise_symbol(raw_symbol)
        venue_key = _normalise_venue(raw_venue)
        base_qty = _coerce_float(row.get("base_qty"))
        avg_price = abs(_coerce_float(row.get("avg_price")))
        notional = abs(base_qty) * avg_price
        entry = snapshot.by_venue_symbol.setdefault(
            (venue_key, symbol_key),
            {
                "symbol": raw_symbol or symbol_key,
                "venue": raw_venue or venue_key,
                "base_qty": 0.0,
                "avg_price": 0.0,
                "LONG": 0.0,
                "SHORT": 0.0,
                "total_abs": 0.0,
                "side": "FLAT",
            },
        )
        entry["base_qty"] = base_qty
        entry["avg_price"] = avg_price
        entry["total_abs"] = notional
        if base_qty > 0:
            entry["LONG"] = notional
            entry["SHORT"] = 0.0
            entry["side"] = "LONG"
        elif base_qty < 0:
            entry["LONG"] = 0.0
            entry["SHORT"] = notional
            entry["side"] = "SHORT"
        else:
            entry["LONG"] = 0.0
            entry["SHORT"] = 0.0
            entry["side"] = "FLAT"
        snapshot.by_symbol[symbol_key] = snapshot.by_symbol.get(symbol_key, 0.0) + notional
        side_key = entry["side"]
        if side_key in {"LONG", "SHORT"}:
            key = (symbol_key, side_key)
            snapshot.by_symbol_side[key] = snapshot.by_symbol_side.get(key, 0.0) + notional
    _record_snapshot_metrics(snapshot)
    return snapshot


def _record_snapshot_metrics(snapshot: ExposureCapsSnapshot) -> None:
    global _PREVIOUS_EXPOSURE_LABELS
    current: set[Tuple[str, str, str]] = set()
    for (venue_key, symbol_key), entry in snapshot.by_venue_symbol.items():
        venue_label = str(entry.get("venue") or venue_key or "unknown")
        for side in ("LONG", "SHORT"):
            value = _coerce_float(entry.get(side))
            label = (symbol_key or "UNKNOWN", side, venue_label)
            current.add(label)
            EXPOSURE_CURRENT_ABS.labels(symbol=label[0], side=side, venue=venue_label).set(value)
    for label in _PREVIOUS_EXPOSURE_LABELS - current:
        symbol, side, venue = label
        EXPOSURE_CURRENT_ABS.labels(symbol=symbol, side=side, venue=venue).set(0.0)
    _PREVIOUS_EXPOSURE_LABELS = current


def resolve_caps(cfg: Any, symbol: Any, side: Any, venue: Any) -> Dict[str, float | None]:
    """Resolve the applicable caps for the supplied scope."""

    root = _extract_caps_root(cfg)
    symbol_key = _normalise_symbol(symbol)
    side_key = _normalise_side(side)
    venue_key = _normalise_venue(venue)
    if not root or not symbol_key:
        return {
            "global_max_abs": None,
            "side_max_abs": None,
            "venue_max_abs": None,
        }
    default_entry = _caps_entry(_model_to_mapping(root).get("default"))
    global_cap = default_entry.get("max_abs_usdt")
    side_cap = default_entry.get("per_side_max_abs_usdt", {}).get(side_key) if side_key else None
    venue_cap: float | None = None

    for candidate, payload in _iter_mapping_items(_model_to_mapping(root).get("per_symbol")):
        if _normalise_symbol(candidate) != symbol_key:
            continue
        entry = _caps_entry(payload)
        if entry.get("max_abs_usdt") is not None:
            global_cap = entry.get("max_abs_usdt")
        if side_key:
            per_side = entry.get("per_side_max_abs_usdt", {})
            if per_side.get(side_key) is not None:
                side_cap = per_side.get(side_key)
        break

    if venue_key:
        per_venue = _model_to_mapping(_model_to_mapping(root).get("per_venue"))
        for venue_candidate, symbols in per_venue.items():
            if _normalise_venue(venue_candidate) != venue_key:
                continue
            for symbol_candidate, entry_raw in _iter_mapping_items(symbols):
                if _normalise_symbol(symbol_candidate) != symbol_key:
                    continue
                entry = _caps_entry(entry_raw)
                if entry.get("max_abs_usdt") is not None:
                    venue_cap = entry.get("max_abs_usdt")
                if side_key:
                    per_side = entry.get("per_side_max_abs_usdt", {})
                    if per_side.get(side_key) is not None:
                        side_cap = per_side.get(side_key)
                break
            break

    return {
        "global_max_abs": global_cap,
        "side_max_abs": side_cap,
        "venue_max_abs": venue_cap,
    }


def snapshot_entry(
    snapshot: ExposureCapsSnapshot,
    *,
    symbol: Any,
    venue: Any,
) -> Tuple[Dict[str, float], str, str]:
    symbol_key = _normalise_symbol(symbol)
    venue_key = _normalise_venue(venue)
    entry = snapshot.by_venue_symbol.get((venue_key, symbol_key))
    if entry is None:
        entry = {
            "symbol": str(symbol or symbol_key or "").upper() or symbol_key,
            "venue": str(venue or venue_key or ""),
            "base_qty": 0.0,
            "avg_price": 0.0,
            "LONG": 0.0,
            "SHORT": 0.0,
            "total_abs": 0.0,
            "side": "FLAT",
        }
    return entry, symbol_key, venue_key


def project_exposure(
    snapshot: ExposureCapsSnapshot,
    *,
    symbol: Any,
    side: Any,
    venue: Any,
    new_abs_position: Any,
) -> Dict[str, Any]:
    side_key = _normalise_side(side)
    entry, symbol_key, venue_key = snapshot_entry(snapshot, symbol=symbol, venue=venue)
    current_total = _coerce_float(entry.get("total_abs"))
    current_global = _coerce_float(snapshot.by_symbol.get(symbol_key))
    current_side_total = (
        _coerce_float(snapshot.by_symbol_side.get((symbol_key, side_key))) if side_key else 0.0
    )
    current_side_contribution = _coerce_float(entry.get(side_key)) if side_key else 0.0
    projected_side_abs = max(_coerce_float(new_abs_position), 0.0) if side_key else 0.0
    projected_total = projected_side_abs if side_key else 0.0
    projected_global = current_global - current_total + projected_total
    projected_side_total = (
        current_side_total - current_side_contribution + projected_side_abs
        if side_key
        else current_side_total
    )
    return {
        "symbol": symbol_key,
        "symbol_label": entry.get("symbol") or str(symbol or symbol_key or "").upper(),
        "venue": venue_key,
        "venue_label": entry.get("venue") or str(venue or venue_key or ""),
        "side_key": side_key,
        "global": {
            "current": current_global,
            "projected": projected_global,
        },
        "side_totals": {
            "current": current_side_total,
            "projected": projected_side_total,
        },
        "venue_total": {
            "current": current_total,
            "projected": projected_total,
        },
    }


def _store_snapshot(ctx: Any, snapshot: ExposureCapsSnapshot) -> None:
    if isinstance(ctx, MutableMapping):
        ctx["snapshot"] = snapshot
        return
    if hasattr(ctx, "__dict__"):
        setattr(ctx, "snapshot", snapshot)


def _store_projection(ctx: Any, projection: Mapping[str, Any]) -> None:
    if isinstance(ctx, MutableMapping):
        ctx["projection"] = dict(projection)
        return
    if hasattr(ctx, "__dict__"):
        setattr(ctx, "projection", dict(projection))


def _extract_snapshot(ctx: Any) -> ExposureCapsSnapshot | None:
    if isinstance(ctx, Mapping):
        candidate = ctx.get("snapshot")
    else:
        candidate = getattr(ctx, "snapshot", None)
    if isinstance(candidate, ExposureCapsSnapshot):
        return candidate
    return None


def _extract_config(ctx: Any) -> Any:
    if isinstance(ctx, Mapping):
        return ctx.get("config")
    return getattr(ctx, "config", ctx)


def _record_cap_metrics(projection: Mapping[str, Any], caps: Mapping[str, Any]) -> None:
    symbol = str(projection.get("symbol") or projection.get("symbol_label") or "UNKNOWN")
    venue_label = str(projection.get("venue_label") or projection.get("venue") or "unknown")
    side = str(projection.get("side_key") or "LONG")
    global_cap = caps.get("global_max_abs")
    side_cap = caps.get("side_max_abs")
    venue_cap = caps.get("venue_max_abs")
    EXPOSURE_CAP_GLOBAL.labels(symbol=symbol).set(
        float(global_cap) if global_cap is not None else 0.0
    )
    EXPOSURE_CAP_SIDE.labels(symbol=symbol, side=side).set(
        float(side_cap) if side_cap is not None else 0.0
    )
    EXPOSURE_CAP_VENUE.labels(symbol=symbol, venue=venue_label).set(
        float(venue_cap) if venue_cap is not None else 0.0
    )


def check_open_allowed(
    ctx: Any,
    symbol: Any,
    side: Any,
    venue: Any,
    new_abs_position: Any,
) -> Tuple[bool, str | None]:
    """Return whether opening additional exposure is allowed for the scope."""

    snapshot = _extract_snapshot(ctx)
    if snapshot is None:
        snapshot = collect_snapshot()
        _store_snapshot(ctx, snapshot)
    projection = project_exposure(
        snapshot,
        symbol=symbol,
        side=side,
        venue=venue,
        new_abs_position=new_abs_position,
    )
    _store_projection(ctx, projection)
    side_key = projection.get("side_key")
    if side_key is None:
        # No increase in exposure if the resulting side is flat/unknown.
        return True, None
    if projection["venue_total"]["projected"] <= projection["venue_total"]["current"] + 1e-9:
        caps = resolve_caps(_extract_config(ctx), symbol, side_key, venue)
        _record_cap_metrics(projection, caps)
        return True, None
    caps = resolve_caps(_extract_config(ctx), symbol, side_key, venue)
    _record_cap_metrics(projection, caps)
    checks = {
        "GLOBAL": projection["global"]["projected"],
        "SIDE": projection["side_totals"]["projected"],
        "VENUE": projection["venue_total"]["projected"],
    }
    limits = {
        "GLOBAL": caps.get("global_max_abs"),
        "SIDE": caps.get("side_max_abs"),
        "VENUE": caps.get("venue_max_abs"),
    }
    for kind, limit in limits.items():
        if limit is None:
            continue
        if checks[kind] > float(limit) + 1e-9:
            return False, f"EXPOSURE_CAPS::{kind}"
    return True, None


def _collect_symbol_labels(snapshot: ExposureCapsSnapshot, cfg: Any) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for (venue_key, symbol_key), entry in snapshot.by_venue_symbol.items():
        labels.setdefault(symbol_key, str(entry.get("symbol") or symbol_key))
    for key, _ in _iter_mapping_items(_model_to_mapping(_extract_caps_root(cfg)).get("per_symbol")):
        norm = _normalise_symbol(key)
        if norm:
            labels.setdefault(norm, str(key).upper())
    return labels


def _collect_venue_labels(cfg: Any) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    root = _extract_caps_root(cfg)
    if not root:
        return labels
    for venue_key, symbols in _iter_mapping_items(_model_to_mapping(root).get("per_venue")):
        norm = _normalise_venue(venue_key)
        if norm:
            labels.setdefault(norm, str(venue_key))
            if isinstance(symbols, Mapping):
                # ensure nested mapping iteration works for pydantic models
                continue
    return labels


def build_status_payload(
    cfg: Any,
    snapshot: ExposureCapsSnapshot | None = None,
) -> Dict[str, Any]:
    """Build a serialisable payload describing exposure caps and usage."""

    if snapshot is None:
        snapshot = collect_snapshot()
    root = _extract_caps_root(cfg)
    labels = _collect_symbol_labels(snapshot, cfg)
    venues_cfg = _collect_venue_labels(cfg)
    symbols = set(snapshot.by_symbol.keys())
    for key, _ in _iter_mapping_items(_model_to_mapping(root).get("per_symbol")):
        norm = _normalise_symbol(key)
        if norm:
            symbols.add(norm)
    payload: Dict[str, Any] = {
        "enabled": bool(root),
        "ts": snapshot.ts,
        "by_symbol": {},
        "by_venue": {},
    }
    for symbol_key in sorted(symbols):
        label = labels.get(symbol_key, symbol_key)
        current_total = _coerce_float(snapshot.by_symbol.get(symbol_key))
        entry: Dict[str, Any] = {
            "current_abs": current_total,
            "global_cap": resolve_caps(cfg, label, "LONG", None).get("global_max_abs"),
            "per_side": {},
        }
        for side in ("LONG", "SHORT"):
            entry["per_side"][side] = {
                "current_abs": _coerce_float(snapshot.by_symbol_side.get((symbol_key, side))),
                "cap": resolve_caps(cfg, label, side, None).get("side_max_abs"),
            }
        payload["by_symbol"][label] = entry
    per_venue: Dict[str, Dict[str, Any]] = {}
    for (venue_key, symbol_key), entry in snapshot.by_venue_symbol.items():
        venue_label = str(entry.get("venue") or venues_cfg.get(venue_key) or venue_key or "")
        symbol_label = labels.get(symbol_key, entry.get("symbol") or symbol_key)
        venue_section = per_venue.setdefault(venue_label, {})
        venue_section[symbol_label] = {
            "current_abs": _coerce_float(entry.get("total_abs")),
            "side": entry.get("side") or "FLAT",
            "cap": resolve_caps(cfg, symbol_label, entry.get("side"), venue_label).get(
                "venue_max_abs"
            ),
        }
    if root:
        per_venue_cfg = _model_to_mapping(root).get("per_venue")
        for venue_key, symbols in _iter_mapping_items(per_venue_cfg):
            venue_label = venues_cfg.get(_normalise_venue(venue_key), str(venue_key))
            venue_section = per_venue.setdefault(venue_label, {})
            for symbol_key, _ in _iter_mapping_items(symbols):
                symbol_label = labels.get(_normalise_symbol(symbol_key), str(symbol_key).upper())
                venue_section.setdefault(
                    symbol_label,
                    {
                        "current_abs": 0.0,
                        "side": "FLAT",
                        "cap": resolve_caps(cfg, symbol_label, "LONG", venue_label).get(
                            "venue_max_abs"
                        ),
                    },
                )
    payload["by_venue"] = per_venue
    return payload
