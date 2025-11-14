from __future__ import annotations

import json
from typing import Dict, List

from .notifier import Event


def _format_extra(data: Dict[str, object] | None) -> str:
    if not data:
        return ""
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    except TypeError:
        safe = {key: str(value) for key, value in data.items()}
        return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def evt_router_block(
    *, reason: str, strategy: str, symbol: str, extra: Dict[str, object] | None
) -> Event:
    tags = {"reason": reason, "strategy": strategy, "symbol": symbol}
    detail = f"strategy={strategy} symbol={symbol} reason={reason}"
    extra_payload = _format_extra(extra)
    if extra_payload:
        detail = f"{detail} extra={extra_payload}"
    return Event(
        kind="router-block",
        severity="warn",
        title=f"Router block: {reason}",
        detail=detail,
        tags=tags,
        ctx={"symbol": symbol, "strategy": strategy},
    )


def evt_recon_issues(count: int, sample_kinds: List[str]) -> Event:
    detail = ", ".join(sample_kinds)
    if detail:
        detail = f"sample={detail}"
    return Event(
        kind="recon-issues",
        severity="warn",
        title=f"Recon issues: {count}",
        detail=detail,
        tags={"count": str(count)},
    )


def evt_readiness(state: str, detail: str) -> Event:
    severity = "info" if state == "ok" else "critical"
    return Event(
        kind="readiness",
        severity=severity,
        title=f"Readiness state: {state}",
        detail=detail,
        tags={"state": state},
    )


def evt_pnl_cap(scope: str, reason: str) -> Event:
    return Event(
        kind="pnl-cap",
        severity="critical",
        title=f"PnL cap triggered: {scope}",
        detail=f"reason={reason}",
        tags={"scope": scope, "reason": reason},
    )


def evt_error(title: str, detail: str) -> Event:
    return Event(
        kind="error",
        severity="error",
        title=title,
        detail=detail,
    )
