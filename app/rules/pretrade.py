from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping, Sequence

from zoneinfo import ZoneInfo

from ..metrics.pretrade import PRETRADE_AUTOFIX_TOTAL, PRETRADE_CHECKS_TOTAL
from ..services.runtime import get_state, record_pretrade_block

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TimeWindow:
    """Represents a repeating time window in a specific timezone."""

    start: dt_time
    end: dt_time
    tz: ZoneInfo
    label: str | None = None

    def contains(self, instant: datetime) -> bool:
        local_now = instant.astimezone(self.tz)
        start_dt = datetime.combine(local_now.date(), self.start, tzinfo=self.tz)
        end_dt = datetime.combine(local_now.date(), self.end, tzinfo=self.tz)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
            if local_now < start_dt:
                start_dt -= timedelta(days=1)
        return start_dt <= local_now < end_dt

    def next_occurrences(self, now: datetime, *, limit: int = 2) -> list[tuple[datetime, datetime]]:
        occurrences: list[tuple[datetime, datetime]] = []
        local_now = now.astimezone(self.tz)
        # consider windows from yesterday through three days ahead to handle wraparound
        for offset in range(-1, 4):
            day = local_now.date() + timedelta(days=offset)
            start_dt = datetime.combine(day, self.start, tzinfo=self.tz)
            end_dt = datetime.combine(day, self.end, tzinfo=self.tz)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            if end_dt <= local_now:
                continue
            if start_dt <= local_now < end_dt or start_dt > local_now:
                occurrences.append((start_dt, end_dt))
            if len(occurrences) >= limit:
                break
        return occurrences

    def as_dict(self) -> dict[str, str]:
        payload = {
            "from": self.start.strftime("%H:%M"),
            "to": self.end.strftime("%H:%M"),
            "tz": self.tz.key,
        }
        if self.label:
            payload["reason"] = self.label
        return payload


@dataclass(slots=True)
class SymbolSpecs:
    symbol: str
    tick: float
    lot: float
    min_notional: float
    trade_hours: list[TimeWindow] = field(default_factory=list)
    blocked: bool = False
    reason: str | None = None

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "tick": self.tick,
            "lot": self.lot,
            "min_notional": self.min_notional,
            "trade_hours": [window.as_dict() for window in self.trade_hours],
            "blocked": self.blocked,
            "reason": self.reason,
        }
        if now is not None:
            payload["upcoming_trade_windows"] = [
                {
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "tz": window.tz.key,
                    "reason": window.label,
                }
                for window in self.trade_hours
                for start, end in window.next_occurrences(now)
            ]
        return payload


class PretradeValidationError(RuntimeError):
    """Raised when a pre-trade validation fails."""

    def __init__(self, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


def _decimal_floor(value: float, step: float) -> float:
    if step <= 0:
        return value
    try:
        dec_value = Decimal(str(value))
        dec_step = Decimal(str(step))
        scaled = (dec_value / dec_step).to_integral_value(rounding=ROUND_DOWN)
        return float(scaled * dec_step)
    except (InvalidOperation, ValueError):
        return value


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _coerce_positive_float(value: Any) -> float:
    number = _coerce_float(value)
    if math.isnan(number) or number <= 0:
        return float("nan")
    return number


def _parse_time(value: str) -> dt_time:
    hours, minutes = value.split(":", 1)
    return dt_time(hour=int(hours), minute=int(minutes))


def _to_zoneinfo(value: str | None, default: ZoneInfo) -> ZoneInfo:
    if value:
        try:
            return ZoneInfo(value)
        except Exception:  # pragma: no cover - invalid tz falls back
            pass
    return default


class PretradeValidator:
    """Performs venue-specific pre-trade validation before order submission."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        local_specs_path: Path | None = None,
        local_specs: Mapping[str, Mapping[str, Any]] | None = None,
        cache_ttl_s: float = 300.0,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if local_specs_path is None:
            local_specs_path = Path(__file__).resolve().parents[2] / "data" / "symbol_specs.json"
        self._local_specs_path = local_specs_path
        self._local_specs_override = local_specs
        self._local_specs_cache: Mapping[str, Mapping[str, Any]] | None = None
        self._cache_ttl_s = float(cache_ttl_s)
        self._cache: Dict[tuple[str, str], tuple[SymbolSpecs, float]] = {}
        self._cache_lock = RLock()

    # ------------------------------------------------------------------
    def _load_local_specs(self) -> Mapping[str, Mapping[str, Any]]:
        if self._local_specs_override is not None:
            return self._local_specs_override
        if self._local_specs_cache is not None:
            return self._local_specs_cache
        try:
            with self._local_specs_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle) or {}
        except FileNotFoundError:
            payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        result = {
            str(symbol).upper(): value
            for symbol, value in payload.items()
            if isinstance(value, Mapping)
        }
        self._local_specs_cache = result
        return result

    # ------------------------------------------------------------------
    def _resolve_client(self, venue: str) -> Any:
        state = get_state()
        derivatives = getattr(state, "derivatives", None)
        if derivatives is None:
            return None
        venue_key = venue.replace("-", "_").lower()
        runtime = derivatives.venues.get(venue_key)
        if runtime is None:
            # try canonical mapping
            for candidate, value in derivatives.venues.items():
                if candidate.replace("_", "-").lower() == venue.lower():
                    runtime = value
                    break
        if runtime is None:
            return None
        return getattr(runtime, "client", None)

    # ------------------------------------------------------------------
    def _build_trade_windows(
        self,
        entries: Iterable[Mapping[str, Any]] | None,
        default_tz: ZoneInfo,
    ) -> list[TimeWindow]:
        windows: list[TimeWindow] = []
        if not entries:
            return windows
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            start_raw = entry.get("from")
            end_raw = entry.get("to")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                continue
            try:
                start_time = _parse_time(start_raw)
                end_time = _parse_time(end_raw)
            except Exception:
                continue
            tz_value = _to_zoneinfo(entry.get("tz") if isinstance(entry.get("tz"), str) else None, default_tz)
            windows.append(
                TimeWindow(
                    start=start_time,
                    end=end_time,
                    tz=tz_value,
                    label=str(entry.get("reason")) if entry.get("reason") else None,
                )
            )
        return windows

    # ------------------------------------------------------------------
    def _maintenance_windows(self, default_tz: ZoneInfo) -> list[TimeWindow]:
        state = get_state()
        cfg = getattr(state.config.data, "maintenance", None)
        entries = getattr(cfg, "windows", None)
        if isinstance(entries, Sequence):
            raw_entries = [
                entry.model_dump(by_alias=True) if hasattr(entry, "model_dump") else dict(entry)
                for entry in entries
            ]
        else:
            raw_entries = []
        windows = self._build_trade_windows(raw_entries, default_tz)
        return windows

    # ------------------------------------------------------------------
    def load_specs(self, ctx: Mapping[str, Any]) -> SymbolSpecs | None:
        symbol = str(ctx.get("symbol") or "").upper().strip()
        if not symbol:
            return None
        venue = str(ctx.get("venue") or "").strip()
        cache_key = (venue.lower(), symbol)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and cached[1] > now:
                return cached[0]
        state = get_state()
        pretrade_cfg = getattr(state.config.data, "pretrade", None)
        default_tz_str = getattr(pretrade_cfg, "default_tz", "UTC") or "UTC"
        default_tz = _to_zoneinfo(default_tz_str, ZoneInfo("UTC"))
        local_specs = self._load_local_specs().get(symbol, {})
        trade_windows = self._build_trade_windows(local_specs.get("trade_hours"), default_tz)
        blocked = bool(local_specs.get("blocked", False))
        reason = str(local_specs.get("reason") or "") or None
        tick = _coerce_positive_float(local_specs.get("tick"))
        lot = _coerce_positive_float(local_specs.get("lot"))
        min_notional = _coerce_positive_float(local_specs.get("min_notional"))

        client = self._resolve_client(venue) if venue else None
        exchange_symbol = str(local_specs.get("exchange_symbol") or symbol)
        filters: Mapping[str, Any] | None = None
        if client is not None:
            getter = getattr(client, "get_symbol_specs", None)
            if callable(getter):
                try:
                    filters = getter(exchange_symbol)
                except Exception:
                    try:
                        filters = getter(symbol)
                    except Exception:
                        filters = None
            else:
                getter = getattr(client, "get_filters", None)
                if callable(getter):
                    try:
                        filters = getter(exchange_symbol)
                    except Exception:
                        try:
                            filters = getter(symbol)
                        except Exception:
                            filters = None
        if isinstance(filters, Mapping):
            tick = _coerce_positive_float(filters.get("tick_size")) if math.isnan(tick) else tick
            lot = _coerce_positive_float(filters.get("step_size")) if math.isnan(lot) else lot
            min_notional = (
                _coerce_positive_float(filters.get("min_notional")) if math.isnan(min_notional) else min_notional
            )
        if math.isnan(tick):
            tick = 0.0
        if math.isnan(lot):
            lot = 0.0
        if math.isnan(min_notional):
            min_notional = 0.0
        specs = SymbolSpecs(
            symbol=symbol,
            tick=tick,
            lot=lot,
            min_notional=min_notional,
            trade_hours=trade_windows,
            blocked=blocked,
            reason=reason,
        )
        with self._cache_lock:
            self._cache[cache_key] = (specs, now + self._cache_ttl_s)
        return specs

    # ------------------------------------------------------------------
    def validate(self, order: Mapping[str, Any]) -> tuple[bool, str | None, Mapping[str, Any] | None]:
        state = get_state()
        pretrade_cfg = getattr(state.config.data, "pretrade", None)
        allow_autofix = bool(getattr(pretrade_cfg, "allow_autofix", True))
        default_tz_str = getattr(pretrade_cfg, "default_tz", "UTC") or "UTC"
        default_tz = _to_zoneinfo(default_tz_str, ZoneInfo("UTC"))

        symbol = str(order.get("symbol") or "").upper().strip()
        venue = str(order.get("venue") or "").strip()
        qty_value = _coerce_float(order.get("qty"))
        price_value = order.get("price")
        price_float = _coerce_float(price_value) if price_value is not None else float("nan")
        notional_value = order.get("notional")
        notional_float = _coerce_float(notional_value) if notional_value is not None else float("nan")

        if not symbol:
            PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="missing_symbol").inc()
            LOGGER.warning("pretrade_blocked missing symbol", extra={"reason": "missing_symbol"})
            record_pretrade_block(symbol, "missing_symbol", qty=float("nan"), price=float("nan"))
            return False, "missing_symbol", None

        environment = str(state.control.environment or state.control.deployment_mode or "paper").lower()
        guardrails_cfg = getattr(state.config.data, "guardrails", None)
        resume_required = environment == "live" and bool(state.control.two_man_rule)
        if resume_required:
            resume_state = getattr(state.safety, "resume_request", None)
            resume_ok = bool(resume_state and getattr(resume_state, "approved_ts", None))
            if not resume_ok:
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="guard_live").inc()
                reason = "two_man_resume_required"
                record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None
        if environment == "testnet" and guardrails_cfg is not None:
            should_block = bool(getattr(guardrails_cfg, "testnet_block_highrisk", False))
            blocklist = {str(entry).upper() for entry in getattr(guardrails_cfg, "blocklist", [])}
            if should_block and symbol in blocklist:
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="guard_testnet").inc()
                reason = "symbol_blocked_testnet"
                record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None

        specs = self.load_specs(order)
        if specs is None:
            specs = SymbolSpecs(symbol=symbol, tick=0.0, lot=0.0, min_notional=0.0)

        if specs.blocked:
            PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="blocked").inc()
            reason = specs.reason or "symbol_blocked"
            record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
            LOGGER.warning(
                "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
            )
            return False, reason, None

        fixed: Dict[str, Any] = {}
        mutable_price = price_float
        mutable_qty = qty_value
        if math.isnan(mutable_qty) or mutable_qty <= 0:
            PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="qty").inc()
            reason = "qty_invalid"
            record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
            LOGGER.warning("pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason})
            return False, reason, None

        if not math.isnan(mutable_price) and specs.tick > 0:
            floored_price = _decimal_floor(mutable_price, specs.tick)
            if floored_price <= 0:
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="tick").inc()
                reason = "price_below_tick"
                record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None
            if not math.isclose(mutable_price, floored_price, rel_tol=0.0, abs_tol=1e-12):
                if allow_autofix:
                    mutable_price = floored_price
                    fixed["price"] = floored_price
                    PRETRADE_AUTOFIX_TOTAL.labels(field="price").inc()
                else:
                    PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="tick").inc()
                    reason = "price_tick"
                    record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                    LOGGER.warning(
                        "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                    )
                    return False, reason, None

        if specs.lot > 0:
            floored_qty = _decimal_floor(mutable_qty, specs.lot)
            if floored_qty <= 0:
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="lot").inc()
                reason = "qty_below_step"
                record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None
            if not math.isclose(mutable_qty, floored_qty, rel_tol=0.0, abs_tol=1e-12):
                if allow_autofix:
                    mutable_qty = floored_qty
                    fixed["qty"] = floored_qty
                    PRETRADE_AUTOFIX_TOTAL.labels(field="qty").inc()
                else:
                    PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="lot").inc()
                    reason = "qty_step"
                    record_pretrade_block(symbol, reason, qty=qty_value, price=price_float)
                    LOGGER.warning(
                        "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                    )
                    return False, reason, None

        price_for_notional = mutable_price
        if math.isnan(price_for_notional):
            price_for_notional = _coerce_float(order.get("price"))
        if math.isnan(price_for_notional):
            price_for_notional = float("nan")
        notional_calculated = mutable_qty * price_for_notional if not math.isnan(price_for_notional) else float("nan")
        if math.isnan(notional_float):
            notional_float = notional_calculated
        if specs.min_notional > 0 and not math.isnan(notional_float) and notional_float + 1e-9 < specs.min_notional:
            PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="notional").inc()
            reason = "min_notional"
            record_pretrade_block(symbol, reason, qty=mutable_qty, price=mutable_price)
            LOGGER.warning(
                "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
            )
            return False, reason, None

        now_ts = self._clock()
        if specs.trade_hours:
            if not any(window.contains(now_ts) for window in specs.trade_hours):
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="trade_hours").inc()
                reason = "outside_trade_hours"
                record_pretrade_block(symbol, reason, qty=mutable_qty, price=mutable_price)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None

        maintenance_windows = self._maintenance_windows(default_tz)
        for window in maintenance_windows:
            if window.contains(now_ts):
                PRETRADE_CHECKS_TOTAL.labels(result="blocked", reason="maintenance").inc()
                reason = window.label or "maintenance"
                record_pretrade_block(symbol, reason, qty=mutable_qty, price=mutable_price)
                LOGGER.warning(
                    "pretrade_blocked", extra={"symbol": symbol, "venue": venue, "reason": reason}
                )
                return False, reason, None

        if fixed:
            PRETRADE_CHECKS_TOTAL.labels(result="fixed", reason="ok").inc()
            return True, None, fixed
        PRETRADE_CHECKS_TOTAL.labels(result="ok", reason="ok").inc()
        return True, None, None

    # ------------------------------------------------------------------
    def describe_symbol(self, symbol: str, *, venue: str | None = None) -> dict[str, Any]:
        specs = self.load_specs({"symbol": symbol, "venue": venue or ""})
        if specs is None:
            raise KeyError(symbol)
        now_ts = self._clock()
        payload = specs.as_dict(now=now_ts)
        pretrade_cfg = getattr(get_state().config.data, "pretrade", None)
        default_tz_str = getattr(pretrade_cfg, "default_tz", "UTC") or "UTC"
        default_tz = _to_zoneinfo(default_tz_str, ZoneInfo("UTC"))
        maintenance = [
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "tz": window.tz.key,
                "reason": window.label,
            }
            for window in self._maintenance_windows(default_tz)
            for start, end in window.next_occurrences(now_ts)
        ]
        payload["upcoming_maintenance"] = maintenance
        return payload


_VALIDATOR: PretradeValidator | None = None
_VALIDATOR_LOCK = RLock()


def get_pretrade_validator() -> PretradeValidator:
    global _VALIDATOR
    with _VALIDATOR_LOCK:
        if _VALIDATOR is None:
            _VALIDATOR = PretradeValidator()
        return _VALIDATOR


def reset_pretrade_validator_for_tests() -> None:
    global _VALIDATOR
    with _VALIDATOR_LOCK:
        _VALIDATOR = None
