import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.golden import replay
from app.services.arbitrage import ExecutionReport
from app.services.runtime import HoldActiveError


@pytest.mark.asyncio
async def test_golden_replay_detects_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLDEN_REPLAY_ENABLED", "1")
    monkeypatch.setenv("GOLDEN_RECORD_ENABLED", "0")

    timestamp = datetime.now(timezone.utc).isoformat()

    plan_one = {
        "symbol": "BTCUSDT",
        "notional": 1000.0,
        "viable": True,
        "legs": [
            {"ex": "binance-um", "side": "buy", "px": 100.0, "qty": 0.01, "fee_usdt": 0.0},
            {"ex": "okx-perp", "side": "sell", "px": 101.0, "qty": 0.01, "fee_usdt": 0.0},
        ],
        "est_pnl_usdt": 1.0,
        "est_pnl_bps": 10.0,
        "used_fees_bps": {"binance": 10, "okx": 10},
        "used_slippage_bps": 5,
        "spread_bps": 12.0,
        "venues": ["binance-um", "okx-perp"],
    }
    runtime_one = json.dumps({"plan": plan_one, "report_state": "DONE"}, sort_keys=True)

    plan_two = {
        "symbol": "ETHUSDT",
        "notional": 500.0,
        "viable": False,
        "legs": [
            {"ex": "binance-um", "side": "buy", "px": 50.0, "qty": 0.02, "fee_usdt": 0.0},
            {"ex": "okx-perp", "side": "sell", "px": 50.5, "qty": 0.02, "fee_usdt": 0.0},
        ],
        "est_pnl_usdt": 0.0,
        "est_pnl_bps": 0.0,
        "used_fees_bps": {"binance": 10, "okx": 10},
        "used_slippage_bps": 5,
        "spread_bps": 0.0,
        "venues": ["binance-um", "okx-perp"],
        "reason": "hold_active",
    }
    runtime_two = json.dumps({"plan": plan_two, "report_state": "HOLD"}, sort_keys=True)

    plan_three = {
        "symbol": "LTCUSDT",
        "notional": 200.0,
        "viable": True,
        "legs": [
            {"ex": "binance-um", "side": "buy", "px": 70.0, "qty": 0.5, "fee_usdt": 0.0},
            {"ex": "okx-perp", "side": "sell", "px": 70.5, "qty": 0.5, "fee_usdt": 0.0},
        ],
        "est_pnl_usdt": 0.25,
        "est_pnl_bps": 12.5,
        "used_fees_bps": {"binance": 10, "okx": 10},
        "used_slippage_bps": 5,
        "spread_bps": 8.0,
        "venues": ["binance-um", "okx-perp"],
    }
    runtime_three = json.dumps({"plan": plan_three, "report_state": "DONE"}, sort_keys=True)

    events = [
        {
            "ts": timestamp,
            "venue": "binance-um",
            "symbol": "BTCUSDT",
            "side": "buy",
            "size": 0.01,
            "reason": "DONE",
            "runtime_state": runtime_one,
            "hold": False,
            "dry_run": True,
        },
        {
            "ts": timestamp,
            "venue": "okx-perp",
            "symbol": "BTCUSDT",
            "side": "sell",
            "size": 0.01,
            "reason": "DONE",
            "runtime_state": runtime_one,
            "hold": False,
            "dry_run": True,
        },
        {
            "ts": timestamp,
            "venue": "binance-um",
            "symbol": "ETHUSDT",
            "side": "none",
            "size": 0.0,
            "reason": "hold_active",
            "runtime_state": runtime_two,
            "hold": True,
            "dry_run": True,
        },
        {
            "ts": timestamp,
            "venue": "binance-um",
            "symbol": "LTCUSDT",
            "side": "buy",
            "size": 0.5,
            "reason": "DONE",
            "runtime_state": runtime_three,
            "hold": False,
            "dry_run": True,
        },
        {
            "ts": timestamp,
            "venue": "okx-perp",
            "symbol": "LTCUSDT",
            "side": "sell",
            "size": 0.5,
            "reason": "DONE",
            "runtime_state": runtime_three,
            "hold": False,
            "dry_run": True,
        },
    ]

    trace_path = tmp_path / "golden_trace.log"
    with trace_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True))
            handle.write("\n")

    async def fake_executor(plan):
        if plan.symbol == "BTCUSDT":
            return ExecutionReport(
                symbol=plan.symbol,
                simulated=True,
                pnl_usdt=0.0,
                pnl_bps=0.0,
                legs=plan.legs,
                plan_viable=True,
                safe_mode=False,
                dry_run=True,
                orders=[
                    {"venue": "binance-um", "symbol": plan.symbol, "side": "buy", "qty": 0.02},
                    {"venue": "okx-perp", "symbol": plan.symbol, "side": "sell", "qty": 0.02},
                ],
                exposures=[],
                pnl_summary={},
                state="DONE",
                risk_gate={},
                risk_snapshot={},
            )
        if plan.symbol == "ETHUSDT":
            raise HoldActiveError("manual_hold")
        return ExecutionReport(
            symbol=plan.symbol,
            simulated=True,
            pnl_usdt=0.0,
            pnl_bps=0.0,
            legs=plan.legs,
            plan_viable=True,
            safe_mode=False,
            dry_run=True,
            orders=[
                {"venue": "binance-um", "symbol": plan.symbol, "side": "buy", "qty": 0.5},
                {"venue": "okx-perp", "symbol": plan.symbol, "side": "sell", "qty": 0.5},
            ],
            exposures=[],
            pnl_summary={},
            state="DONE",
            risk_gate={},
            risk_snapshot={},
        )

    summary = await replay.replay_trace(path=trace_path, executor=fake_executor)

    assert summary.total_events == len(events)
    assert summary.total_groups == 3
    assert not summary.ok
    assert summary.mismatches
    mismatch_symbols = {entry.details["symbol"] for entry in summary.mismatches}
    assert "BTCUSDT" in mismatch_symbols
