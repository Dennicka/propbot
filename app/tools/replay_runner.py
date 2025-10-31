"""Offline replay/backtest runner for execution logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _is_fill(status: Any) -> bool:
    label = _coerce_str(status, "").strip().lower()
    if not label:
        return True
    return label in {"filled", "fill", "success", "done", "partial", "partial_fill"}


def _resolve_side(payload: Mapping[str, Any]) -> str:
    for key in ("side", "direction", "action"):
        if key in payload:
            return _coerce_str(payload.get(key)).strip().lower()
    return "buy"


def _extract_fee(payload: Mapping[str, Any]) -> float:
    for key in ("fees_usd", "fee_usd", "fee", "fees", "commission_usd", "commission"):
        if key in payload and payload[key] not in (None, ""):
            return _coerce_float(payload[key], 0.0)
    return 0.0


def _extract_reference_price(payload: Mapping[str, Any]) -> float:
    for key in (
        "quote_price",
        "reference_price",
        "mark_price",
        "expected_price",
        "target_price",
        "mid_price",
    ):
        if key in payload:
            price = _coerce_float(payload.get(key), 0.0)
            if price > 0:
                return price
    return 0.0


def _extract_fill_price(payload: Mapping[str, Any]) -> float:
    for key in (
        "fill_price",
        "execution_price",
        "price",
        "executed_price",
        "trade_price",
    ):
        if key in payload:
            price = _coerce_float(payload.get(key), 0.0)
            if price > 0:
                return price
    return 0.0


def _extract_quantity(payload: Mapping[str, Any], reference_price: float) -> float:
    for key in ("qty", "quantity", "size", "filled_qty", "base_quantity"):
        if key in payload:
            qty = _coerce_float(payload.get(key), 0.0)
            if qty != 0.0:
                return qty
    # Derive quantity from notional if available.
    notional_keys = (
        "notional",
        "notional_usd",
        "notional_usdt",
        "quote_notional",
        "order_notional_usd",
    )
    for key in notional_keys:
        if key in payload:
            notional = _coerce_float(payload.get(key), 0.0)
            if notional and reference_price > 0:
                return notional / reference_price
    return 0.0


@dataclass
class ReplaySummary:
    input_file: str
    generated_at: str
    attempts: int
    fills: int
    hit_ratio: float
    gross_pnl: float
    fees_total: float
    net_pnl: float
    total_notional: float
    avg_slippage_bps: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Normalise floats for readability.
        for key in (
            "hit_ratio",
            "gross_pnl",
            "fees_total",
            "net_pnl",
            "total_notional",
            "avg_slippage_bps",
        ):
            value = payload.get(key)
            if isinstance(value, float):
                payload[key] = float(f"{value:.10f}") if math.isfinite(value) else value
        return payload


def load_records(path: Path) -> Iterator[Mapping[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".json"}:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                yield json.loads(line)
        return
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Parquet support requires the optional 'pyarrow' dependency"
            ) from exc
        table = pq.read_table(path)
        for record in table.to_pylist():
            if isinstance(record, Mapping):
                yield record
        return
    raise ValueError(f"Unsupported replay format: {path.suffix}")


def compute_summary(records: Iterable[Mapping[str, Any]], *, input_file: str) -> ReplaySummary:
    attempts = 0
    fills = 0
    gross_pnl = 0.0
    fees_total = 0.0
    total_notional = 0.0
    slippage_bps_numerator = 0.0

    for payload in records:
        if not isinstance(payload, Mapping):
            continue
        attempts += 1
        filled = _is_fill(payload.get("status"))
        if filled:
            fills += 1

        reference_price = _extract_reference_price(payload)
        fill_price = _extract_fill_price(payload)
        qty = _extract_quantity(payload, reference_price)
        if reference_price <= 0 and fill_price > 0:
            reference_price = fill_price
        if fill_price <= 0 and reference_price > 0:
            fill_price = reference_price

        if filled and qty and reference_price > 0 and fill_price > 0:
            qty_abs = abs(qty)
            notional = qty_abs * reference_price
            side = _resolve_side(payload)
            is_buy = side in {"buy", "long", "bid"}
            is_sell = side in {"sell", "short", "ask"}
            if not (is_buy or is_sell):
                is_buy = True
            if is_buy:
                pnl = (reference_price - fill_price) * qty_abs
            else:
                pnl = (fill_price - reference_price) * qty_abs
            gross_pnl += pnl
            total_notional += notional
            slippage_bps_numerator += pnl * 10_000.0

        fees_total += _extract_fee(payload)

    hit_ratio = fills / attempts if attempts else 0.0
    net_pnl = gross_pnl - fees_total
    avg_slippage_bps = (
        slippage_bps_numerator / total_notional if total_notional else 0.0
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    return ReplaySummary(
        input_file=str(input_file),
        generated_at=generated_at,
        attempts=attempts,
        fills=fills,
        hit_ratio=hit_ratio,
        gross_pnl=gross_pnl,
        fees_total=fees_total,
        net_pnl=net_pnl,
        total_notional=total_notional,
        avg_slippage_bps=avg_slippage_bps,
    )


def _resolve_output_stem(directory: Path, timestamp: datetime) -> str:
    base = timestamp.strftime("backtest_%Y%m%d_%H%M")
    stem = base
    counter = 1
    while (directory / f"{stem}.json").exists() or (directory / f"{stem}.csv").exists():
        stem = f"{base}_{counter:02d}"
        counter += 1
    return stem


def save_summary(
    summary: ReplaySummary,
    *,
    output_dir: Path,
    timestamp: datetime | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc)
    stem = _resolve_output_stem(output_dir, ts)
    payload = summary.to_dict()
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(payload.keys()))
        writer.writeheader()
        writer.writerow(payload)
    return json_path, csv_path


def run_backtest(file_path: Path, *, output_dir: Path) -> ReplaySummary:
    records = list(load_records(file_path))
    summary = compute_summary(records, input_file=str(file_path))
    save_summary(summary, output_dir=output_dir)
    return summary


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline replay/backtest runner")
    parser.add_argument("--file", required=True, help="Path to replay file (.jsonl or .parquet)")
    parser.add_argument(
        "--outdir",
        default="data/reports",
        help="Directory for generated reports (default: data/reports)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Replay file not found: {file_path}", file=sys.stderr)
        return 1
    try:
        records = list(load_records(file_path))
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"Failed to load replay: {exc}", file=sys.stderr)
        return 1
    summary = compute_summary(records, input_file=str(file_path))
    try:
        json_path, csv_path = save_summary(
            summary, output_dir=Path(args.outdir)
        )
    except Exception as exc:  # pragma: no cover - filesystem guard
        print(f"Failed to write report: {exc}", file=sys.stderr)
        return 1
    payload = summary.to_dict()
    print("Replay summary")
    for key, value in payload.items():
        print(f"  {key}: {value}")
    print(f"Reports written to {json_path} and {csv_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

