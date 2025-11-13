from pathlib import Path

from app.metrics.recon import export_recon_metrics


def test_export_recon_metrics(tmp_path: Path) -> None:
    target = tmp_path / "metrics.prom"

    export_recon_metrics(
        orders_open={"okx": 2, "binance": 3},
        orders_final=7,
        anomalies={"invalid_transition": 5, "duplicate_event": 1},
        md_staleness_p95_ms={"okx": 800, "binance": 1200, "bybit": 0},
        path=target,
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == [
        'propbot_orders_open{venue="binance"} 3',
        'propbot_orders_open{venue="okx"} 2',
        "propbot_orders_final_total 7",
        'propbot_anomaly_total{type="invalid_transition"} 5',
        'propbot_md_staleness_p95_ms{venue="binance"} 1200',
        'propbot_md_staleness_p95_ms{venue="bybit"} 0',
        'propbot_md_staleness_p95_ms{venue="okx"} 800',
    ]
