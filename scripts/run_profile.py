#!/usr/bin/env python3
"""Unified entrypoint for launching PropBot runtime profiles."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Sequence

from app.config.profiles import (
    ProfileSafetyError,
    RuntimeProfile,
    apply_profile_environment,
    ensure_profile_safe,
    load_profile,
)
from app.profile_config import ProfileConfigError


LOGGER = logging.getLogger("propbot.run_profile")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PropBot profile launcher. Defaults to paper mode with all risk guards enabled."
        )
    )
    parser.add_argument(
        "--profile",
        default="paper",
        choices=[member.value for member in RuntimeProfile],
        help="runtime profile to activate",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("PROP_APP_HOST", os.getenv("API_HOST", "127.0.0.1")),
        help="bind host for uvicorn",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PROP_APP_PORT", os.getenv("API_PORT", "8000"))),
        help="bind port for uvicorn",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="enable uvicorn autoreload (development only)",
    )
    return parser


def _prepare_environment(profile: RuntimeProfile) -> int:
    try:
        profile_cfg = load_profile(profile)
    except ProfileSafetyError as exc:
        LOGGER.error("%s", exc)
        return 1
    except ProfileConfigError as exc:
        LOGGER.error("Не удалось загрузить профиль %s: %s", profile.value, exc)
        return 1

    applied = apply_profile_environment(profile, profile_cfg)
    LOGGER.info(
        "Активируем профиль=%s", profile.value,
    )
    LOGGER.debug(
        "Параметры окружения: %s",
        ", ".join(f"{key}={value}" for key, value in sorted(applied.items())),
    )

    try:
        ensure_profile_safe(profile, profile_cfg)
    except ProfileSafetyError as exc:
        details = "\n".join(f"  {reason}" for reason in exc.reasons) if exc.reasons else str(exc)
        LOGGER.error("Запуск отклонён:%s%s", "\n" if exc.reasons else " ", details)
        return 1
    return 0


def _run_uvicorn(host: str, port: int, reload: bool) -> int:
    try:
        import uvicorn
    except Exception as exc:  # pragma: no cover - defensive import guard
        LOGGER.error("Не удалось импортировать uvicorn: %s", exc)
        return 1

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=bool(reload),
        log_config=None,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    profile = RuntimeProfile.parse(args.profile)
    exit_code = _prepare_environment(profile)
    if exit_code != 0:
        return exit_code
    LOGGER.info(
        "Запуск сервиса host=%s port=%s профиль=%s", args.host, args.port, profile.value
    )
    return _run_uvicorn(args.host, args.port, args.reload)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
