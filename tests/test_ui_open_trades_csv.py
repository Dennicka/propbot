from __future__ import annotations

import csv
import io

from positions import create_position, reset_positions


def test_open_trades_csv_export(client) -> None:
    reset_positions()
    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=12.5,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
    )

    response = client.get("/api/ui/open-trades.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    content_disposition = response.headers.get("content-disposition")
    assert content_disposition is not None
    assert "open-trades.csv" in content_disposition

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows
    header = rows[0]
    assert header == [
        "trade_id",
        "pair",
        "side",
        "size",
        "entry_price",
        "unrealized_pnl",
        "opened_ts",
    ]
    assert len(rows) > 1
    data_rows = rows[1:]
    assert any(entry[2] == "long" for entry in data_rows)
    assert any(entry[2] == "short" for entry in data_rows)
    for entry in data_rows:
        assert entry[0]
        assert entry[1] == "ETHUSDT"
        float(entry[3])
        float(entry[4])
        float(entry[5])
        assert entry[6]
