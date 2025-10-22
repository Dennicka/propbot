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
    loop_parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="optional number of cycles to run before exiting (0 runs indefinitely)",
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

    ledger.init_db()
    logging.info("starting auto-loop (env=%s, cycles=%s)", args.env, "infinite" if args.cycles <= 0 else args.cycles)
    cycles = args.cycles if args.cycles > 0 else None
    try:
        asyncio.run(loop_forever(cycles=cycles))
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
