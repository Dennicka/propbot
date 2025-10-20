from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable

from app.services import arbitrage
from app.services.runtime import get_state, reset_for_tests
from app.util.env import ensure_defaults, load_env_file


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def _configure_logger(log_path: str | None) -> logging.Logger:
    logger = logging.getLogger("testnet_smoke")
    logger.handlers = []
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s :: %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def _serialize_edges(edges: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for edge in edges:
        yield {
            "pair_id": edge.get("pair_id"),
            "net_edge_bps": edge.get("net_edge_bps"),
            "tradable_size": edge.get("tradable_size"),
        }


def run_smoke(*, dry_run: bool = True, log_path: str | None = None) -> Dict[str, Any]:
    """Execute a lightweight arbitrage dry-run using testnet defaults."""

    load_env_file()
    ensure_defaults(
        [
            ("MODE", "testnet"),
            ("SAFE_MODE", "true"),
            ("POST_ONLY", "true"),
            ("REDUCE_ONLY", "true"),
        ]
    )

    logger = _configure_logger(log_path)
    logger.info("starting testnet smoke (dry_run=%s)", dry_run)

    reset_for_tests()
    arbitrage.reset_engine()
    state = get_state()

    engine = arbitrage.get_engine()
    report = arbitrage.run_preflight_report()
    preflight = report.as_dict()
    edges = list(engine.compute_edges())

    logger.info("preflight ok: %s", preflight["ok"])
    if edges:
        best = max(edge.get("net_edge_bps", 0.0) for edge in edges)
        logger.info("best edge: %.4fbps across %d pairs", best, len(edges))
    else:
        logger.warning("no arbitrage pairs available")

    enable_orders = _truthy(os.getenv("ENABLE_PLACE_TEST_ORDERS"))
    should_execute = enable_orders and not state.control.safe_mode and not dry_run

    execution: Dict[str, Any]
    if should_execute:
        logger.warning("test orders enabled and SAFE_MODE disabled — executing live test trade")
        execution = arbitrage.execute_trade(None, None)
    else:
        logger.info(
            "order placement disabled (safe_mode=%s, enable_orders=%s, dry_run=%s)",
            state.control.safe_mode,
            enable_orders,
            dry_run,
        )
        execution = {"ok": True, "executed": False, "reason": "dry_run"}

    summary: Dict[str, Any] = {
        "mode": state.control.environment,
        "safe_mode": state.control.safe_mode,
        "post_only": state.control.post_only,
        "reduce_only": state.control.reduce_only,
        "preflight": preflight,
        "edges": list(_serialize_edges(edges)),
        "execution": execution,
    }

    logger.info("smoke summary: %s", json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="PropBot testnet smoke harness")
    parser.add_argument("--log", default="logs/testnet_smoke.log", help="path to store smoke log output")
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="allow execution when SAFE_MODE=false and ENABLE_PLACE_TEST_ORDERS=true",
    )
    args = parser.parse_args()

    summary = run_smoke(dry_run=not args.no_dry_run, log_path=args.log)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
