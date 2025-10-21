from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from ..services.dryrun import DryRunScheduler

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run continuous dry-run arbitrage cycles without placing real orders.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--once",
        action="store_true",
        help="execute a single dry-run cycle and print the result as JSON",
    )
    group.add_argument(
        "--loop",
        action="store_true",
        help="run the dry-run cycle continuously using the configured poll interval",
    )
    parser.add_argument(
        "--artifact",
        help="override path for the JSON artifact with the last dry-run plan",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    scheduler = DryRunScheduler(artifact_path=Path(args.artifact) if args.artifact else None)

    if args.loop:
        LOGGER.info("starting dry-run scheduler loop")
        try:
            scheduler.loop()
        except KeyboardInterrupt:
            LOGGER.info("dry-run loop interrupted by user")
        return 0

    # default behaviour is a single cycle
    result = scheduler.run_once()
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
