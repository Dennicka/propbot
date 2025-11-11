"""Operational self-check to validate readiness before enabling trading."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import os
import socket
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

from ..config import trading_profiles
from ..config.loader import load_app_config, validate_payload
from ..profile_config import (
    MissingSecretsError,
    ProfileConfig,
    ProfileConfigError,
    ensure_required_secrets,
    load_profile_config,
)
from ..secrets_store import SecretsStore
from ..startup_validation import collect_startup_errors
from ..util.venues import VENUE_ALIASES


class CheckStatus(str, Enum):
    """Outcome for a self-check step."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    """Single validation result."""

    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class SelfCheckReport:
    """Aggregated report for all self-check steps."""

    profile: str
    results: Sequence[CheckResult]

    def has_failures(self) -> bool:
        return any(result.status is CheckStatus.FAIL for result in self.results)

    def overall_status(self) -> CheckStatus:
        if self.has_failures():
            return CheckStatus.FAIL
        if any(result.status is CheckStatus.WARN for result in self.results):
            return CheckStatus.WARN
        return CheckStatus.OK


def _normalise_profile_name(name: str | None) -> str:
    if not name:
        return trading_profiles.ExecutionProfile.PAPER.value
    try:
        resolved = trading_profiles.ExecutionProfile(str(name).strip().lower())
    except ValueError as exc:
        raise ProfileConfigError(f"Неизвестный профиль self-check: {name!r}") from exc
    return resolved.value


def _load_trading_profile(name: str) -> trading_profiles.TradingProfile:
    try:
        return trading_profiles.get_profile(name)
    except trading_profiles.TradingProfileError as exc:  # pragma: no cover - defensive
        raise ProfileConfigError(str(exc)) from exc


def _check_env_alignment(profile_name: str) -> list[CheckResult]:
    configured = os.getenv("TRADING_PROFILE")
    config_profile = os.getenv("PROFILE")
    results: list[CheckResult] = []

    if configured and configured.strip().lower() != profile_name:
        results.append(
            CheckResult(
                name="env.trading_profile",
                status=CheckStatus.WARN,
                message=(
                    "TRADING_PROFILE=%s (env) не совпадает с выбранным профилем %s. "
                    "Установи TRADING_PROFILE перед запуском runtime." % (configured, profile_name)
                ),
            )
        )

    if config_profile and config_profile.strip().lower() != profile_name:
        results.append(
            CheckResult(
                name="env.profile_config",
                status=CheckStatus.WARN,
                message=(
                    "PROFILE=%s (env) не совпадает с профилем self-check %s. "
                    "Синхронизируй переменные окружения перед запуском."
                    % (config_profile, profile_name)
                ),
            )
        )

    return results


def _check_profile_config(profile_name: str) -> tuple[ProfileConfig | None, CheckResult]:
    try:
        profile_cfg = load_profile_config(profile=profile_name)
    except ProfileConfigError as exc:
        return None, CheckResult(
            name="profile.config",
            status=CheckStatus.FAIL,
            message=str(exc),
        )

    if profile_cfg.requires_secrets:
        secrets_path = os.getenv("SECRETS_STORE_PATH")
        if not secrets_path:
            return profile_cfg, CheckResult(
                name="profile.secrets",
                status=CheckStatus.FAIL,
                message=(
                    "PROFILE=%s требует SECRETS_STORE_PATH с JSON-хранилищем ключей."
                    % profile_cfg.name
                ),
            )
        try:
            store = SecretsStore(secrets_path)
        except Exception as exc:  # pragma: no cover - defensive
            return profile_cfg, CheckResult(
                name="profile.secrets",
                status=CheckStatus.FAIL,
                message=f"Не удалось загрузить secrets store: {exc}",
            )
        try:
            ensure_required_secrets(profile_cfg, lambda name: store.get_exchange_credentials(name))
        except MissingSecretsError as exc:
            return profile_cfg, CheckResult(
                name="profile.secrets",
                status=CheckStatus.FAIL,
                message=str(exc),
            )

    message = "Профиль %s загружен (dry_run=%s, requires_secrets=%s)." % (
        profile_cfg.name,
        profile_cfg.dry_run,
        profile_cfg.requires_secrets,
    )
    return profile_cfg, CheckResult(
        name="profile.config",
        status=CheckStatus.OK,
        message=message,
    )


def _check_runtime_config(profile_name: str) -> CheckResult:
    config_path = Path("configs") / f"config.{profile_name}.yaml"
    if not config_path.exists():
        return CheckResult(
            name="config.runtime",
            status=CheckStatus.FAIL,
            message=f"Отсутствует {config_path}. Создай конфиг перед запуском.",
        )
    try:
        loaded = load_app_config(config_path)
    except Exception as exc:
        return CheckResult(
            name="config.runtime",
            status=CheckStatus.FAIL,
            message=f"Ошибка загрузки {config_path.name}: {exc}",
        )

    errors = validate_payload(loaded.data.model_dump())
    if errors:
        return CheckResult(
            name="config.runtime",
            status=CheckStatus.FAIL,
            message="Конфиг %s содержит ошибки: %s" % (config_path.name, "; ".join(errors)),
        )
    return CheckResult(
        name="config.runtime",
        status=CheckStatus.OK,
        message=f"Конфиг {config_path.name} валиден и загружен без ошибок.",
    )


def _check_risk_limits(
    trading_profile: trading_profiles.TradingProfile,
    profile_cfg: ProfileConfig | None,
) -> CheckResult:
    issues: list[str] = []

    if trading_profile.max_notional_per_order <= 0:
        issues.append("max_notional_per_order должен быть > 0")
    if trading_profile.max_notional_per_symbol <= 0:
        issues.append("max_notional_per_symbol должен быть > 0")
    if trading_profile.max_notional_global <= 0:
        issues.append("max_notional_global должен быть > 0")
    if trading_profile.daily_loss_limit <= 0:
        issues.append("daily_loss_limit должен быть > 0")
    if trading_profile.max_notional_per_order > trading_profile.max_notional_per_symbol:
        issues.append("max_notional_per_order превышает max_notional_per_symbol")
    if trading_profile.max_notional_per_symbol > trading_profile.max_notional_global:
        issues.append("max_notional_per_symbol превышает max_notional_global")

    if profile_cfg is not None:
        limits = profile_cfg.risk_limits
        if limits.max_single_position_usd <= 0:
            issues.append("risk_limits.max_single_position_usd должен быть > 0")
        if limits.max_total_notional_usd <= 0:
            issues.append("risk_limits.max_total_notional_usd должен быть > 0")
        if limits.daily_loss_cap_usd <= 0:
            issues.append("risk_limits.daily_loss_cap_usd должен быть > 0")
        if limits.max_drawdown_bps <= 0:
            issues.append("risk_limits.max_drawdown_bps должен быть > 0")
        if limits.max_single_position_usd > limits.max_total_notional_usd:
            issues.append("max_single_position_usd не может превышать max_total_notional_usd")
        trading_cap = float(trading_profile.max_notional_global)
        if limits.max_total_notional_usd > trading_cap:
            issues.append("risk_limits.max_total_notional_usd превышает лимит trading_profile")
        daily_cap = float(trading_profile.daily_loss_limit)
        if limits.daily_loss_cap_usd > daily_cap:
            issues.append(
                "risk_limits.daily_loss_cap_usd превышает daily_loss_limit trading_profile"
            )

    if issues:
        return CheckResult(
            name="risk.limits",
            status=CheckStatus.FAIL,
            message="; ".join(issues),
        )

    return CheckResult(
        name="risk.limits",
        status=CheckStatus.OK,
        message="Лимиты риска профиля %s валидны." % trading_profile.name,
    )


def _check_venues(profile_cfg: ProfileConfig | None) -> CheckResult:
    if profile_cfg is None:
        return CheckResult(
            name="venues",
            status=CheckStatus.WARN,
            message="Профиль не загружен, пропускаем проверку площадок.",
        )

    missing: list[str] = []
    venues: Iterable[str] = [profile_cfg.broker.primary.venue]
    if profile_cfg.broker.hedge is not None:
        venues = [*venues, profile_cfg.broker.hedge.venue]
    for venue in venues:
        if VENUE_ALIASES.get(venue) is None:
            missing.append(venue)

    if missing:
        return CheckResult(
            name="venues",
            status=CheckStatus.FAIL,
            message="Неизвестные venues: %s" % ", ".join(sorted(set(missing))),
        )

    return CheckResult(
        name="venues",
        status=CheckStatus.OK,
        message="Все venues профиля присутствуют в поддерживаемом списке.",
    )


def _probe_endpoint(label: str, url: str) -> CheckResult:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return CheckResult(
            name=f"network.{label}",
            status=CheckStatus.FAIL,
            message=f"URL {url!r} некорректен",
        )

    host = parsed.hostname
    if not host:
        return CheckResult(
            name=f"network.{label}",
            status=CheckStatus.FAIL,
            message=f"Не удалось извлечь хост из {url}",
        )

    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return CheckResult(
            name=f"network.{label}",
            status=CheckStatus.WARN,
            message=f"DNS недоступен для {host}: {exc}",
        )
    except OSError as exc:  # pragma: no cover - defensive
        return CheckResult(
            name=f"network.{label}",
            status=CheckStatus.WARN,
            message=f"Проверка {host} завершилась с ошибкой: {exc}",
        )
    return CheckResult(
        name=f"network.{label}",
        status=CheckStatus.OK,
        message=f"{host} разрешается через DNS",
    )


def _check_network(profile_cfg: ProfileConfig | None, *, skip: bool) -> list[CheckResult]:
    if skip:
        return [
            CheckResult(
                name="network",
                status=CheckStatus.WARN,
                message="Сетевые проверки пропущены (--skip-network)",
            )
        ]

    if profile_cfg is None:
        return [
            CheckResult(
                name="network",
                status=CheckStatus.WARN,
                message="Профиль не загружен, сетевые проверки пропущены.",
            )
        ]

    endpoints = {
        "primary.rest": profile_cfg.broker.primary.endpoints.rest,
        "primary.ws": profile_cfg.broker.primary.endpoints.websocket,
    }
    if profile_cfg.broker.hedge is not None:
        endpoints.update(
            {
                "hedge.rest": profile_cfg.broker.hedge.endpoints.rest,
                "hedge.ws": profile_cfg.broker.hedge.endpoints.websocket,
            }
        )

    return [_probe_endpoint(label, url) for label, url in endpoints.items()]


def _check_startup_environment() -> CheckResult:
    errors = collect_startup_errors()
    if errors:
        return CheckResult(
            name="environment",
            status=CheckStatus.FAIL,
            message="; ".join(errors),
        )
    return CheckResult(
        name="environment",
        status=CheckStatus.OK,
        message="ENV переменные и файловые пути готовы к запуску.",
    )


def run_self_check(profile: str | None = None, *, skip_network: bool = False) -> SelfCheckReport:
    profile_name = _normalise_profile_name(profile)
    trading_profile = _load_trading_profile(profile_name)

    results: list[CheckResult] = []
    results.append(
        CheckResult(
            name="trading_profile",
            status=CheckStatus.OK,
            message=(
                "Активный trading profile: %s (order=%s, symbol=%s, global=%s, daily_loss=%s)."
                % (
                    trading_profile.name,
                    trading_profile.max_notional_per_order,
                    trading_profile.max_notional_per_symbol,
                    trading_profile.max_notional_global,
                    trading_profile.daily_loss_limit,
                )
            ),
        )
    )
    results.extend(_check_env_alignment(profile_name))

    profile_cfg, cfg_result = _check_profile_config(profile_name)
    results.append(cfg_result)
    results.append(_check_runtime_config(profile_name))
    results.append(_check_risk_limits(trading_profile, profile_cfg))
    results.append(_check_venues(profile_cfg))
    results.append(_check_startup_environment())
    results.extend(_check_network(profile_cfg, skip=skip_network))

    return SelfCheckReport(profile=profile_name, results=tuple(results))


def _format_report(report: SelfCheckReport) -> str:
    lines = [f"Self-check profile: {report.profile}"]
    for result in report.results:
        lines.append(f"[{result.status.value}] {result.name}: {result.message}")
    lines.append(f"Overall: {report.overall_status().value}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PropBot operational self-check")
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in trading_profiles.ExecutionProfile],
        help="Профиль для проверки (по умолчанию TRADING_PROFILE или paper)",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Пропустить DNS-проверки (например, для оффлайн CI).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_self_check(profile=args.profile, skip_network=args.skip_network)
    print(_format_report(report))
    return 1 if report.has_failures() else 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
