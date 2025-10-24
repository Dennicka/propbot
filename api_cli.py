from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import requests

DEFAULT_BASE_URL = "http://localhost:8000"
REQUEST_TIMEOUT = 30


class CLIError(RuntimeError):
    """Raised when CLI execution fails."""


def _build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}" if path.startswith('/') else f"{base_url.rstrip('/')}/{path}"


def _perform_get(
    url: str, params: dict[str, Any], headers: dict[str, str] | None = None
) -> requests.Response:
    try:
        request_kwargs: dict[str, Any] = {"params": params, "timeout": REQUEST_TIMEOUT}
        if headers:
            request_kwargs["headers"] = headers
        response = requests.get(url, **request_kwargs)
    except requests.RequestException as exc:  # pragma: no cover - defensive
        raise CLIError(f"Request failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")  # type: ignore[assignment]
        except Exception:  # pragma: no cover - fallback when payload not json
            detail = response.text
        raise CLIError(f"HTTP {response.status_code}: {detail}")
    return response


def _to_text(response: requests.Response, fmt: str) -> str:
    if fmt == "json":
        data = response.json()
        return json.dumps(data, indent=2, sort_keys=True) + "\n"
    return response.text if response.text.endswith("\n") else response.text + "\n"


def _write_output(text: str, out_path: Path | None) -> None:
    if out_path is None:
        sys.stdout.write(text)
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")


def _auth_headers(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _events_command(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {
        "format": args.format,
        "limit": args.limit,
        "offset": args.offset,
        "order": args.order,
    }
    for key in ("venue", "symbol", "level", "since", "until", "search"):
        value = getattr(args, key)
        if value:
            params[key] = value
    url = _build_url(args.base_url, "/api/ui/events/export")
    response = _perform_get(url, params, headers=_auth_headers(args.api_token))
    text = _to_text(response, args.format)
    _write_output(text, args.out)
    if args.out:
        print(f"Events export written to {args.out}")
    return 0


def _portfolio_command(args: argparse.Namespace) -> int:
    params = {"format": args.format}
    url = _build_url(args.base_url, "/api/ui/portfolio/export")
    response = _perform_get(url, params, headers=_auth_headers(args.api_token))
    text = _to_text(response, args.format)
    _write_output(text, args.out)
    if args.out:
        print(f"Portfolio export written to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PropBot API export CLI")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Base URL of the PropBot API")
    parser.add_argument(
        "--api-token",
        default=None,
        help="Bearer token for API calls (falls back to PROPBOT_API_TOKEN or API_TOKEN env)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="Export UI events")
    events_parser.add_argument("--format", choices=["csv", "json"], default="csv")
    events_parser.add_argument("--out", type=Path, default=None, help="Output file path")
    events_parser.add_argument("--limit", type=int, default=100, help="Maximum number of events to download (<=1000)")
    events_parser.add_argument("--offset", type=int, default=0, help="Pagination offset")
    events_parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="Sorting order")
    events_parser.add_argument("--venue")
    events_parser.add_argument("--symbol")
    events_parser.add_argument("--level")
    events_parser.add_argument("--since", help="ISO timestamp lower bound")
    events_parser.add_argument("--until", help="ISO timestamp upper bound")
    events_parser.add_argument("--search", help="Search substring in event message")
    events_parser.set_defaults(func=_events_command)

    portfolio_parser = subparsers.add_parser("portfolio", help="Export current portfolio snapshot")
    portfolio_parser.add_argument("--format", choices=["csv", "json"], default="csv")
    portfolio_parser.add_argument("--out", type=Path, default=None, help="Output file path")
    portfolio_parser.set_defaults(func=_portfolio_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "api_token", None):
        env_token = os.getenv("PROPBOT_API_TOKEN") or os.getenv("API_TOKEN")
        if env_token:
            args.api_token = env_token
    try:
        func: Callable[[argparse.Namespace], int] = getattr(args, "func")
    except AttributeError as exc:  # pragma: no cover - defensive
        raise CLIError("No command specified") from exc
    try:
        return func(args)
    except CLIError as exc:
        parser.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
