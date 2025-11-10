from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import httpx
from fastapi import FastAPI

from .. import ledger
from ..services import portfolio, risk, approvals_store
from services.daily_reporter import load_latest_report
from ..services.loop import cancel_all_orders, hold_loop
from ..services.runtime import (
    CRITICAL_ACTION_EXIT_DRY_RUN,
    CRITICAL_ACTION_RAISE_LIMIT,
    CRITICAL_ACTION_RESUME,
    approve_exit_dry_run,
    approve_resume,
    approve_risk_limit_change,
    engage_safety_hold,
    get_safety_status,
    get_state,
    get_reconciliation_status,
    record_resume_request,
    request_exit_dry_run,
    request_risk_limit_change,
    set_open_orders,
)
from services import balances_monitor, reconciler

LOGGER = logging.getLogger(__name__)


_ACTIVE_BOT: "TelegramBot | None" = None


async def alert_slo_breach(message: str) -> None:
    """Send an async Telegram notification for SLO breaches."""

    bot = _ACTIVE_BOT
    if bot is None:
        return
    text = str(message or "SLO breach detected")
    await bot.send_message(f"⚠️ {text}")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(1, value)


@dataclass(frozen=True)
class TelegramBotConfig:
    token: str | None
    chat_id: str | None
    enabled: bool
    push_minutes: int

    @classmethod
    def from_env(cls) -> "TelegramBotConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        enabled = _env_flag("TELEGRAM_ENABLE", False)
        push_minutes = _env_int("TELEGRAM_PUSH_MINUTES", 5)
        return cls(token=token, chat_id=chat_id, enabled=enabled, push_minutes=push_minutes)

    @property
    def can_run(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)


def _normalise_symbol(value: Any) -> str:
    if isinstance(value, str):
        return value.upper()
    return str(value or "").upper()


def _extract_attr(item: Any, key: str) -> Any:
    if hasattr(item, key):
        return getattr(item, key)
    if isinstance(item, dict):
        return item.get(key)
    return None


def _summarise_positions(positions: Iterable[Any]) -> str:
    entries = []
    for position in positions:
        symbol = _normalise_symbol(_extract_attr(position, "symbol"))
        qty = _extract_attr(position, "qty")
        if qty is None:
            qty = _extract_attr(position, "notional")
        try:
            qty_value = float(qty)
        except (TypeError, ValueError):
            qty_value = 0.0
        if abs(qty_value) <= 1e-12:
            continue
        entries.append(f"{symbol}:{qty_value:.4g}")
    if not entries:
        return "none"
    if len(entries) > 5:
        head = ", ".join(entries[:5])
        return f"{head}, +{len(entries) - 5} more"
    return ", ".join(entries)


def format_status_message(
    snapshot: Any,
    state: Any,
    risk_state: Any | None,
    safety: Mapping[str, Any] | None,
    auto_state: Any | None,
    pending_requests: Iterable[Mapping[str, Any]],
) -> str:
    pnl_totals = getattr(snapshot, "pnl_totals", {}) or {}
    realized = float(pnl_totals.get("realized", 0.0))
    unrealized = float(pnl_totals.get("unrealized", 0.0))
    total = float(pnl_totals.get("total", realized + unrealized))
    positions = getattr(snapshot, "positions", []) or []
    positions_summary = _summarise_positions(positions)
    control = getattr(state, "control", state)
    safe_mode = bool(getattr(control, "safe_mode", False))
    profile = str(getattr(control, "environment", "paper") or "paper").lower()
    mode = str(getattr(control, "mode", "HOLD")).upper()
    dry_run_mode = bool(getattr(control, "dry_run_mode", False))
    dry_run_only = bool(getattr(control, "dry_run", False))
    breaches = []
    if risk_state is not None:
        breaches = list(getattr(risk_state, "breaches", []) or [])
    breaches_summary = f"RISK_BREACHES={len(breaches)}"
    hold_active = False
    hold_reason = "n/a"
    if safety:
        hold_active = bool(safety.get("hold_active", False))
        hold_reason = str(safety.get("hold_reason") or "n/a")
    auto_enabled = bool(getattr(auto_state, "enabled", False))
    last_hedge_ts = getattr(auto_state, "last_execution_ts", None) if auto_state else None
    if not last_hedge_ts and auto_state:
        last_hedge_ts = getattr(auto_state, "last_success_ts", None)
    last_hedge = str(last_hedge_ts or "never")
    consecutive_failures = 0
    if auto_state and hasattr(auto_state, "consecutive_failures"):
        try:
            consecutive_failures = int(getattr(auto_state, "consecutive_failures"))
        except (TypeError, ValueError):
            consecutive_failures = 0
    pending_items = list(pending_requests)
    pending_list: list[str] = []
    for entry in pending_items:
        action = str(entry.get("action", "unknown"))
        req_id = str(entry.get("id", ""))
        short_id = req_id[:8] if req_id else "-"
        pending_list.append(f"{action}:{short_id}")
        if len(pending_list) >= 3:
            break
    pending_count = len(pending_items)
    if pending_count > len(pending_list):
        pending_list.append(f"+{pending_count - len(pending_list)} more")
    pending_summary = ", ".join(pending_list) if pending_list else "none"
    lines = [
        "Status:",
        f"PnL=realized:{realized:.2f}, unrealized:{unrealized:.2f}, total:{total:.2f}",
        f"Positions={positions_summary}",
        f"SAFE_MODE={safe_mode}",
        f"MODE={mode}",
        f"PROFILE={profile}",
        breaches_summary,
        f"HOLD_ACTIVE={hold_active} (reason={hold_reason})",
        f"DRY_RUN_MODE={dry_run_mode} DRY_RUN_ONLY={dry_run_only}",
        f"AUTO_HEDGE={'on' if auto_enabled else 'off'} (last={last_hedge}, fails={consecutive_failures})",
        f"Pending approvals={pending_summary}",
    ]
    return "\n".join(lines)


async def build_status_message() -> str:
    snapshot = await portfolio.snapshot()
    open_orders = await asyncio.to_thread(ledger.fetch_open_orders)
    set_open_orders(open_orders)
    state = get_state()
    safety = get_safety_status()
    risk_state = risk.refresh_runtime_state(snapshot=snapshot, open_orders=open_orders)
    pending = approvals_store.list_requests(status="pending")
    auto_state = getattr(state, "auto_hedge", None)
    return format_status_message(snapshot, state, risk_state, safety, auto_state, pending)


class TelegramBot:
    def __init__(self, config: TelegramBotConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop_event = asyncio.Event()
        self._update_offset = 0
        self.logger = LOGGER

    def is_authorized_chat(self, chat_id: Any) -> bool:
        if chat_id is None:
            return False
        return str(chat_id) == str(self.config.chat_id)

    async def start(self) -> None:
        if not self.config.can_run:
            self.logger.info("Telegram bot disabled or missing credentials; skipping startup")
            return
        if self._client is not None:
            return
        self.logger.info("Starting Telegram bot background tasks")
        base_url = f"https://api.telegram.org/bot{self.config.token}/"
        self._client = httpx.AsyncClient(base_url=base_url, timeout=20.0)
        await self._discard_pending_updates()
        self._stop_event.clear()
        status_task = asyncio.create_task(self._status_loop(), name="telegram-status-loop")
        command_task = asyncio.create_task(self._command_loop(), name="telegram-command-loop")
        self._tasks = [status_task, command_task]

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception:  # pragma: no cover - defensive logging
                self.logger.exception("Telegram task exited with error")
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.logger.info("Telegram bot stopped")

    async def send_message(self, text: str) -> None:
        if not self.config.can_run:
            return
        if self._client is None:
            self.logger.warning("Telegram client not initialized; message skipped")
            return
        try:
            response = await self._client.post(
                "sendMessage", json={"chat_id": self.config.chat_id, "text": text}
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network failures are logged
            self.logger.warning("Failed to send Telegram message", extra={"error": str(exc)})

    async def send_status_update(self) -> None:
        if not self.config.can_run:
            return
        message = await build_status_message()
        await self.send_message(message)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _status_loop(self) -> None:
        interval = max(1, int(self.config.push_minutes)) * 60
        while not self._stop_event.is_set():
            try:
                await self.send_status_update()
            except Exception:  # pragma: no cover - logged for observability
                self.logger.exception("Failed to push Telegram status update")
            await self._wait(interval)

    async def _command_loop(self) -> None:
        if self._client is None:
            return
        while not self._stop_event.is_set():
            try:
                payload = await self._client.get(
                    "getUpdates",
                    params={
                        "offset": self._update_offset,
                        "timeout": 30,
                        "allowed_updates": ["message"],
                    },
                )
                payload.raise_for_status()
                data = payload.json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - log and retry
                self.logger.warning("Telegram update polling failed", extra={"error": str(exc)})
                await self._wait(5)
                continue
            updates = data.get("result", []) if isinstance(data, dict) else []
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._update_offset = update_id + 1
                try:
                    await self._handle_update(update)
                except Exception:  # pragma: no cover - ensure loop keeps running
                    self.logger.exception("Failed to handle Telegram update")

    async def _discard_pending_updates(self) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.get("getUpdates", params={"timeout": 0})
            response.raise_for_status()
            data = response.json()
            updates = data.get("result", []) if isinstance(data, dict) else []
            if updates:
                last_id = max(
                    update.get("update_id", 0) for update in updates if isinstance(update, dict)
                )
                if isinstance(last_id, int) and last_id:
                    self._update_offset = last_id + 1
        except Exception:  # pragma: no cover - best effort priming
            self.logger.debug("Failed to discard pending Telegram updates", exc_info=True)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        chat_id = None
        if isinstance(chat, dict):
            chat_id = chat.get("id")
        if not self.is_authorized_chat(chat_id):
            self.logger.warning(
                "Ignoring Telegram message from unauthorized chat", extra={"chat_id": chat_id}
            )
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return
        command = text.split()[0].lower()
        if "@" in command:
            command = command.split("@", 1)[0]
        if not command:
            return
        args = text.split()[1:]
        actor = self._extract_actor(message)
        actor_label = actor or "telegram"
        try:
            if command in {"/pause", "/hold"}:
                response = await self._handle_hold(actor_label, args)
            elif command == "/resume":
                response = await self._handle_resume_request(actor_label, args)
            elif command == "/status":
                response = await self._handle_status()
            elif command == "/daily":
                response = await self._handle_daily()
            elif command == "/liquidity":
                response = await self._handle_liquidity()
            elif command == "/reconcile":
                response = await self._handle_reconcile()
            elif command == "/raise_limit":
                response = await self._handle_raise_limit(actor_label, args)
            elif command == "/exit_dry_run":
                response = await self._handle_exit_dry_run(actor_label, args)
            elif command == "/approve":
                response = await self._handle_approve(actor_label, args)
            elif command in {"/close_all", "/closeall", "/close"}:
                response = await self._handle_close_all()
            else:
                response = (
                    "Unknown command. Available: /status, /daily, /liquidity, /reconcile, /hold, /resume, "
                    "/raise_limit, /exit_dry_run, /approve, /close"
                )
        except Exception as exc:  # pragma: no cover - ensure failure is reported
            self.logger.exception("Telegram command failed", extra={"command": command})
            response = f"Command failed: {exc}"[:400]
        await self.send_message(response)

    def _extract_actor(self, message: Mapping[str, Any]) -> str | None:
        user = message.get("from") if isinstance(message, Mapping) else None
        if isinstance(user, Mapping):
            username = user.get("username")
            if username:
                return f"telegram:{username}"
            first = user.get("first_name")
            last = user.get("last_name")
            parts = [part for part in [first, last] if part]
            if parts:
                return f"telegram:{' '.join(parts)}"
        return "telegram"

    async def _handle_hold(self, actor: str, args: list[str]) -> str:
        reason = " ".join(args).strip() or "manual_hold"
        await hold_loop()
        engage_safety_hold(reason, source="telegram")
        ledger.record_event(
            level="INFO",
            code="mode_change",
            payload={"mode": "HOLD", "reason": reason, "requested_by": actor},
        )
        return f"HOLD engaged. SAFE_MODE remains active. Reason: {reason}"

    async def _handle_resume_request(self, actor: str, args: list[str]) -> str:
        safety = get_safety_status()
        if not safety.get("hold_active", False):
            return "HOLD is not active; nothing to resume."
        if not args:
            return "Usage: /resume <reason>"
        reason = " ".join(args).strip()
        if not reason:
            return "Reason required to request resume."
        snapshot = record_resume_request(reason, requested_by=actor)
        request_id = snapshot.get("id") or "-"
        pending = snapshot.get("pending", True)
        ledger.record_event(
            level="INFO",
            code="resume_requested",
            payload={"reason": reason, "requested_by": actor, "request_id": request_id},
        )
        if pending:
            return f"Resume request recorded (id={request_id}). Awaiting approval."
        return f"Resume already approved (id={request_id})."

    async def _handle_status(self) -> str:
        message = await build_status_message()
        return message

    async def _handle_daily(self) -> str:
        report = load_latest_report()
        if not report:
            return "No daily report captured in the last 24h."
        timestamp = str(report.get("timestamp") or "n/a")
        window = int(float(report.get("window_hours") or 24))
        realized = float(report.get("pnl_realized_total") or 0.0)
        unrealized = float(
            report.get("pnl_unrealized_avg") or report.get("pnl_unrealized_latest") or 0.0
        )
        exposure = float(report.get("exposure_avg") or 0.0)
        slippage = report.get("slippage_avg_bps")
        breakdown = (
            report.get("hold_breakdown")
            if isinstance(report.get("hold_breakdown"), Mapping)
            else {}
        )
        auto_holds = int(float(breakdown.get("safety_hold") or 0))
        throttles = int(float(breakdown.get("risk_throttle") or 0))
        total_holds = int(float(report.get("hold_events") or auto_holds + throttles))
        pnl_samples = int(float(report.get("pnl_unrealized_samples") or 0))
        exposure_samples = int(float(report.get("exposure_samples") or 0))
        slippage_samples = int(float(report.get("slippage_samples") or 0))
        if slippage is None:
            slippage_text = "n/a"
        else:
            try:
                slippage_text = f"{float(slippage):.3f}"
            except (TypeError, ValueError):
                slippage_text = str(slippage)
        lines = [
            "Daily report:",
            f"Timestamp={timestamp}",
            f"Window={window}h",
            f"PnL_realized={realized:.2f}",
            f"PnL_unrealized_avg={unrealized:.2f}",
            f"Avg_exposure_usd={exposure:.2f}",
            f"Avg_slippage_bps={slippage_text}",
            f"HOLD_events={total_holds} (auto={auto_holds}, throttle={throttles})",
            f"Samples: pnl={pnl_samples}, exposure={exposure_samples}, slippage={slippage_samples}",
        ]
        return "\n".join(lines)

    async def _handle_liquidity(self) -> str:
        result = await asyncio.to_thread(balances_monitor.evaluate_balances)
        per_venue = result.get("per_venue") if isinstance(result, Mapping) else None
        if not isinstance(per_venue, Mapping):
            per_venue = {}
        lines = ["Liquidity snapshot:"]
        for venue, payload in sorted(per_venue.items()):
            if isinstance(payload, Mapping):
                free_value = payload.get("free_usdt")
                used_value = payload.get("used_usdt")
                risk_ok = bool(payload.get("risk_ok"))
                reason = payload.get("reason")
            else:
                free_value = None
                used_value = None
                risk_ok = False
                reason = payload
            try:
                free_text = f"{float(free_value):.2f}" if free_value is not None else "n/a"
            except (TypeError, ValueError):
                free_text = str(free_value)
            try:
                used_text = f"{float(used_value):.2f}" if used_value is not None else "n/a"
            except (TypeError, ValueError):
                used_text = str(used_value)
            status = "OK" if risk_ok else "BLOCKED"
            reason_text = str(reason or ("ok" if risk_ok else "unknown"))
            lines.append(
                f"{venue}: free={free_text} used={used_text} status={status} (reason={reason_text})"
            )
        blocked = bool(result.get("liquidity_blocked")) if isinstance(result, Mapping) else False
        reason_summary = result.get("reason") if isinstance(result, Mapping) else "unknown"
        lines.append(f"liquidity_blocked={blocked} reason={reason_summary}")
        if blocked:
            lines.append("trading halted for safety")
        return "\n".join(lines)

    async def _handle_reconcile(self) -> str:
        try:
            await asyncio.to_thread(reconciler.reconcile)
        except Exception as exc:  # pragma: no cover - defensive best effort
            return f"Reconciliation failed: {exc}"[:400]
        recon = get_reconciliation_status()
        issue_count = recon.get("issue_count")
        try:
            issue_total = int(issue_count)
        except (TypeError, ValueError):
            issues_payload = recon.get("issues", [])
            issue_total = len(issues_payload) if isinstance(issues_payload, list) else 0
        if issue_total <= 0:
            return "Reconciliation: in sync. No mismatches detected."
        lines = [
            "STATE DESYNC detected.",
            f"Outstanding mismatches: {issue_total}.",
            "Resolve manually before resume.",
        ]
        issues = recon.get("issues", [])
        if isinstance(issues, list):
            for issue in issues[:5]:
                if not isinstance(issue, Mapping):
                    continue
                kind = str(issue.get("kind") or "issue")
                venue = str(issue.get("venue") or "?")
                symbol = str(issue.get("symbol") or "?")
                side = str(issue.get("side") or "")
                detail = str(issue.get("description") or "")
                lines.append(f"- {kind}: {venue}/{symbol} {side} — {detail}")
        safety = get_safety_status()
        if isinstance(safety, Mapping) and safety.get("hold_active"):
            hold_reason = str(safety.get("hold_reason") or "manual_hold")
            lines.append(f"Current HOLD reason: {hold_reason}")
        return "\n".join(lines)

    async def _handle_raise_limit(self, actor: str, args: list[str]) -> str:
        if len(args) < 4:
            return "Usage: /raise_limit <limit> <scope or -> <value> <reason>"
        limit = args[0]
        scope_arg = args[1]
        scope = None if scope_arg in {"-", "none", "default"} else scope_arg
        value_arg = args[2]
        reason = " ".join(args[3:]).strip()
        if not reason:
            return "Reason required to raise limit."
        try:
            value = float(value_arg)
        except ValueError:
            return "Value must be numeric."
        try:
            record = request_risk_limit_change(
                limit, scope, value, reason=reason, requested_by=actor
            )
        except ValueError as exc:
            return f"Risk limit request failed: {exc}"
        parameters = record.get("parameters", {}) if isinstance(record, Mapping) else {}
        ledger.record_event(
            level="INFO",
            code="risk_limit_raise_requested",
            payload={
                "limit": parameters.get("limit", limit),
                "scope": parameters.get("scope", scope),
                "value": parameters.get("value", value),
                "reason": reason,
                "requested_by": actor,
                "request_id": record.get("id"),
            },
        )
        return f"Risk limit request recorded (id={record.get('id')}). Awaiting approval."

    async def _handle_exit_dry_run(self, actor: str, args: list[str]) -> str:
        if not args:
            return "Usage: /exit_dry_run <reason>"
        reason = " ".join(args).strip()
        if not reason:
            return "Reason required to exit DRY_RUN_MODE."
        try:
            record = request_exit_dry_run(reason, requested_by=actor)
        except ValueError as exc:
            return f"Exit DRY_RUN request failed: {exc}"
        ledger.record_event(
            level="INFO",
            code="exit_dry_run_requested",
            payload={"reason": reason, "requested_by": actor, "request_id": record.get("id")},
        )
        return f"Exit DRY_RUN request recorded (id={record.get('id')}). Awaiting approval."

    async def _handle_approve(self, actor: str, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: /approve <request_id> <token>"
        request_id = args[0]
        token = args[1]
        expected = os.environ.get("APPROVE_TOKEN")
        if not expected:
            return "Approve token not configured."
        if not secrets.compare_digest(token, expected):
            return "Invalid approval token."
        request_snapshot = approvals_store.get_request(request_id)
        if not request_snapshot:
            return "Request not found."
        if str(request_snapshot.get("status")) != "pending":
            return "Request already processed."
        action = str(request_snapshot.get("action") or "")
        try:
            if action == CRITICAL_ACTION_RESUME:
                result = approve_resume(request_id=request_id, actor=actor)
                hold_cleared = result.get("hold_cleared", False)
                parameters = (
                    request_snapshot.get("parameters", {})
                    if isinstance(request_snapshot, Mapping)
                    else {}
                )
                ledger.record_event(
                    level="INFO",
                    code="resume_confirmed",
                    payload={
                        "actor": actor,
                        "hold_cleared": hold_cleared,
                        "reason": parameters.get("reason"),
                        "request_id": request_id,
                    },
                )
                return (
                    "Resume approved. HOLD cleared."
                    if hold_cleared
                    else "Resume approval recorded. HOLD remains active."
                )
            if action == CRITICAL_ACTION_RAISE_LIMIT:
                result = approve_risk_limit_change(request_id, actor=actor)
                record = result.get("request", request_snapshot)
                params = record.get("parameters", {}) if isinstance(record, Mapping) else {}
                ledger.record_event(
                    level="INFO",
                    code="risk_limit_raise_approved",
                    payload={
                        "actor": actor,
                        "limit": params.get("limit"),
                        "scope": params.get("scope"),
                        "value": params.get("value"),
                        "reason": params.get("reason"),
                        "request_id": request_id,
                    },
                )
                return "Risk limit updated after approval."
            if action == CRITICAL_ACTION_EXIT_DRY_RUN:
                result = approve_exit_dry_run(request_id, actor=actor)
                record = result.get("request", request_snapshot)
                params = record.get("parameters", {}) if isinstance(record, Mapping) else {}
                ledger.record_event(
                    level="INFO",
                    code="exit_dry_run_approved",
                    payload={
                        "actor": actor,
                        "reason": params.get("reason"),
                        "request_id": request_id,
                    },
                )
                return "DRY_RUN_MODE disabled after approval."
        except ValueError as exc:
            return f"Approval failed: {exc}"
        return f"Unsupported action '{action}'."

    async def _handle_close_all(self) -> str:
        state = get_state()
        environment = str(getattr(state.control, "environment", "")).lower()
        if environment != "testnet":
            return "Cancel-all only available on testnet profile."
        result = await cancel_all_orders()
        ledger.record_event(
            level="INFO", code="cancel_all", payload={"source": "telegram", **result}
        )
        return "Cancel-all requested."


def setup_telegram_bot(app: FastAPI) -> None:
    global _ACTIVE_BOT
    config = TelegramBotConfig.from_env()
    bot = TelegramBot(config)
    _ACTIVE_BOT = bot
    app.state.telegram_bot = bot

    @app.on_event("startup")
    async def _start_bot() -> None:  # pragma: no cover - exercised in integration environments
        await bot.start()

    @app.on_event("shutdown")
    async def _stop_bot() -> None:  # pragma: no cover - exercised in integration environments
        global _ACTIVE_BOT
        await bot.stop()
        _ACTIVE_BOT = None
