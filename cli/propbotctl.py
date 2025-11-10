#!/usr/bin/env python3
"""Command-line helper for PropBot operator workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class CLIError(RuntimeError):
    """Error raised for CLI failures."""


def _build_url(base_url: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{base_url.rstrip('/')}{path}"


def request_json(
    method: str,
    base_url: str,
    path: str,
    token: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    url = _build_url(base_url, path)
    headers: Dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(method, url, headers=headers, json=payload, timeout=30)
    if not response.ok:
        try:
            details = json.dumps(response.json(), indent=2, ensure_ascii=False)
        except ValueError:
            details = response.text.strip()
        message = f"Request failed: {response.status_code} {response.reason}"
        if details:
            message = f"{message}\n{details}"
        raise CLIError(message)
    return response


def cmd_status(args: argparse.Namespace) -> None:
    response = request_json("GET", args.base_url, "/api/ui/status/overview")
    try:
        data = response.json()
    except ValueError as exc:
        raise CLIError(f"Malformed JSON in response: {exc}") from exc

    overall_status = data.get("overall", {}).get("status", "UNKNOWN")
    alerts = data.get("alerts", []) or []

    print(f"Overall status: {overall_status}")
    if alerts:
        print("Active alerts:")
        for alert in alerts:
            title = alert.get("title") or "(no title)"
            print(f"- {title}")
    else:
        print("Active alerts: none")


def _format_table(rows: Iterable[List[str]], headers: List[str]) -> str:
    columns = list(zip(*([headers] + list(rows)))) if rows else [headers]
    widths = [max(len(cell) for cell in column) for column in columns]

    def format_row(row: List[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(row, widths))

    output_lines = [format_row(headers)]
    output_lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        output_lines.append(format_row(row))
    return "\n".join(output_lines)


def cmd_components(args: argparse.Namespace) -> None:
    response = request_json("GET", args.base_url, "/api/ui/status/components")
    try:
        data = response.json()
    except ValueError as exc:
        raise CLIError(f"Malformed JSON in response: {exc}") from exc

    if isinstance(data, dict):
        components = data.get("components", [])
    else:
        components = data

    rows: List[List[str]] = []
    for component in components or []:
        rows.append(
            [
                str(component.get("component_id", "")),
                str(component.get("since", "")),
                str(component.get("status", "")),
            ]
        )

    headers = ["component_id", "since", "status"]
    table = _format_table(rows, headers) if rows else "No components reported."
    print(table)


def _require_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("API_TOKEN")
    if not token:
        raise CLIError("Bearer token required: pass --token or set API_TOKEN environment variable.")
    return token


def cmd_pause(args: argparse.Namespace) -> None:
    token = _require_token(args)
    response = request_json(
        "PATCH",
        args.base_url,
        "/api/ui/control",
        token=token,
        payload={"mode": "HOLD"},
    )
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


def cmd_resume(args: argparse.Namespace) -> None:
    token = _require_token(args)
    response = request_json(
        "PATCH",
        args.base_url,
        "/api/ui/control",
        token=token,
        payload={"mode": "RUN"},
    )
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


def cmd_rotate_key(args: argparse.Namespace) -> None:
    token = _require_token(args)
    if not args.value:
        raise CLIError("--value is required for rotate-key")
    response = request_json(
        "POST",
        args.base_url,
        "/api/ui/secret",
        token=token,
        payload={
            "name": "BINANCE_LV_API_SECRET",
            "value": args.value,
        },
    )
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


def cmd_export_log(args: argparse.Namespace) -> None:
    token = _require_token(args)
    response = request_json("GET", args.base_url, "/api/ui/events/export", token=token)
    try:
        data = response.json()
    except ValueError as exc:
        raise CLIError(f"Malformed JSON in response: {exc}") from exc

    output_path = args.out
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"Events exported to {output_path}")


COMMAND_HANDLERS = {
    "status": cmd_status,
    "components": cmd_components,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "rotate-key": cmd_rotate_key,
    "export-log": cmd_export_log,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PropBot operator CLI helper")
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"API base URL (default: {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--token", default=None, help="Bearer token for mutating commands (env: API_TOKEN)"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show overall bot status")
    subparsers.add_parser("components", help="Show component status table")

    subparsers.add_parser("pause", help="Put the bot into HOLD mode")
    subparsers.add_parser("resume", help="Return the bot to RUN mode")

    rotate_parser = subparsers.add_parser("rotate-key", help="Rotate the Binance live API secret")
    rotate_parser.add_argument("--value", required=True, help="New secret value")

    export_parser = subparsers.add_parser("export-log", help="Export recent events to a JSON file")
    export_parser.add_argument(
        "--out", default="./events_export.json", help="Path to save the exported events"
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"Unknown command: {args.command}")
    try:
        handler(args)
    except CLIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Request error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
