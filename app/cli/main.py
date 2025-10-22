from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PropBot execution CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    exec_parser = sub.add_parser("exec", help="run arbitrage plans")
    exec_parser.add_argument("--profile", default="paper", choices=["paper", "testnet", "live"], help="runtime profile")
    exec_parser.add_argument("--loop", action="store_true", help="run continuously until interrupted")
    exec_parser.add_argument("--artifact", help="path for storing last plan JSON artifact")
    loop_parser = sub.add_parser("loop", help="run automated preview/execute cycles")
    loop_parser.add_argument("--env", default="paper", choices=["paper", "testnet"], help="runtime profile to use")
    loop_parser.add_argument("--pair", required=True, help="symbol to trade (e.g. BTCUSDT)")
    loop_parser.add_argument(
        "--venues",
        nargs="+",
        required=True,
        help="venues participating in the loop (e.g. binance-um okx-perp)",
    )
    loop_parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="optional number of cycles to run before exiting (0 runs indefinitely)",
    )
    loop_parser.add_argument(
        "--notional",
        type=float,
        required=True,
        help="order notional in USDT",
    )
    return parser


def _configure_environment(profile: str) -> None:
    os.environ.setdefault("PROFILE", profile)
    os.environ.setdefault("SAFE_MODE", "true")
    if profile != "live":
        os.environ.setdefault("DRY_RUN_ONLY", "true")


def _run_exec(args: argparse.Namespace) -> int:
    _configure_environment(args.profile)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    from ..services.dryrun import DryRunScheduler

    artifact = Path(args.artifact) if args.artifact else None
    scheduler = DryRunScheduler(artifact_path=artifact)
    if args.loop:
        logging.info("starting continuous execution loop (profile=%s)", args.profile)
        try:
            scheduler.loop()
        except KeyboardInterrupt:
            logging.info("execution loop interrupted")
        return 0
    result = scheduler.run_once()
    logging.info("execution result: ok=%s", result.get("ok"))
    return 0


def _run_loop(args: argparse.Namespace) -> int:
    _configure_environment(args.env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    from .. import ledger
    from ..services.loop import loop_forever
    from ..services.runtime import get_state, set_loop_config, set_mode

    ledger.init_db()
    pair = str(args.pair).upper()
    venues = [str(venue) for venue in args.venues]
    notional = float(args.notional)
    set_loop_config(pair=pair, venues=venues, notional_usdt=notional)
    state = get_state()
    logging.info(
        "starting auto-loop (env=%s, pair=%s, venues=%s, notional=%s, cycles=%s)",
        args.env,
        state.control.loop_pair,
        ",".join(state.control.loop_venues),
        state.control.order_notional_usdt,
        "infinite" if args.cycles <= 0 else args.cycles,
    )
    set_mode("RUN")
    cycles = args.cycles if args.cycles > 0 else None

    async def _log_cycle(result) -> None:
        summary = result.summary.as_dict() if result.summary else {}
        status = summary.get("status", "unknown")
        symbol = summary.get("symbol") or result.symbol or pair
        spread_bps = summary.get("spread_bps")
        pnl_usdt = summary.get("est_pnl_usdt")
        reason = summary.get("reason") or result.error
        details = [f"status={status}", f"symbol={symbol}"]
        if isinstance(spread_bps, (int, float)):
            details.append(f"spread_bps={spread_bps:.2f}")
        if isinstance(pnl_usdt, (int, float)):
            details.append(f"pnl_usdt={pnl_usdt:.4f}")
        if reason:
            details.append(f"reason={reason}")
        logging.info("cycle summary: %s", ", ".join(details))

    try:
        asyncio.run(loop_forever(cycles=cycles, on_cycle=_log_cycle))
    except KeyboardInterrupt:
        logging.info("loop interrupted by user")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "exec":
        return _run_exec(args)
    if args.command == "loop":
        return _run_loop(args)
    parser.error("unknown command")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
