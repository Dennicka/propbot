from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ..core.config import DerivVenueConfig, LoadedConfig
from ..exchanges import DerivClient, InMemoryDerivClient
from ..exchanges import binance_um, okx_perp


@dataclass
class VenueRuntime:
    config: DerivVenueConfig
    client: DerivClient


@dataclass
class DerivativesRuntime:
    config: LoadedConfig
    venues: Dict[str, VenueRuntime] = field(default_factory=dict)

    def initialise(self, safe_mode: bool = True) -> None:
        cfg = self.config.data
        if not cfg.derivatives:
            self.venues = {}
            return
        self.venues = {}
        for venue_cfg in cfg.derivatives.venues:
            if venue_cfg.id == "binance_um":
                client = binance_um.create_client(venue_cfg, safe_mode=safe_mode)
            elif venue_cfg.id == "okx_perp":
                client = okx_perp.create_client(venue_cfg, safe_mode=safe_mode)
            else:
                client = InMemoryDerivClient(venue=venue_cfg.id, symbols={})
            runtime = VenueRuntime(config=venue_cfg, client=client)
            client.set_position_mode(venue_cfg.position_mode)
            for symbol in venue_cfg.symbols:
                client.set_margin_type(symbol, venue_cfg.margin_type)
                client.set_leverage(symbol, venue_cfg.leverage)
            self.venues[venue_cfg.id] = runtime

    def status_payload(self) -> Dict[str, object]:
        items: List[Dict[str, object]] = []
        for venue_id, runtime in self.venues.items():
            client = runtime.client
            venue_cfg = runtime.config
            for symbol in venue_cfg.symbols:
                items.append(
                    {
                        "venue": venue_id,
                        "symbol": symbol,
                        "position_mode": client.position_mode,
                        "margin_type": client.margin_type.get(symbol, venue_cfg.margin_type),
                        "leverage": client.leverage.get(symbol, venue_cfg.leverage),
                        "connected": client.ping(),
                    }
                )
        return {"venues": items}

    def set_modes(self, payload: Dict[str, Dict[str, object]]) -> Dict[str, object]:
        report: Dict[str, object] = {"results": []}
        for venue_id, overrides in payload.items():
            runtime = self.venues.get(venue_id)
            if not runtime:
                report["results"].append({"venue": venue_id, "error": "unknown venue"})
                continue
            client = runtime.client
            if "position_mode" in overrides:
                client.set_position_mode(str(overrides["position_mode"]))
            for symbol in runtime.config.symbols:
                if "margin_type" in overrides:
                    client.set_margin_type(symbol, str(overrides["margin_type"]))
                if "leverage" in overrides:
                    client.set_leverage(symbol, int(overrides["leverage"]))
            report["results"].append({"venue": venue_id, "ok": True})
        return report

    def positions_payload(self) -> Dict[str, object]:
        return {"positions": {vid: rt.client.positions() for vid, rt in self.venues.items()}}

    def flatten_all(self) -> Dict[str, object]:
        reports = []
        for venue_id, runtime in self.venues.items():
            runtime.client.positions_data.clear()
            reports.append({"venue": venue_id, "flattened": True})
        return {"results": reports}


def bootstrap_derivatives(config: LoadedConfig, safe_mode: bool = True) -> DerivativesRuntime:
    runtime = DerivativesRuntime(config=config)
    runtime.initialise(safe_mode=safe_mode)
    return runtime
