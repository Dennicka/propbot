from __future__ import annotations

from dataclasses import dataclass
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import yaml
from pydantic import BaseModel, Field, validator


class RateLimitConfig(BaseModel):
    place_per_min: int = Field(..., ge=0)
    cancel_per_min: int = Field(..., ge=0)


class KillCapsConfig(BaseModel):
    enabled: bool = True
    flatten_on_breach: bool = True


class RunawayBreakerConfig(BaseModel):
    place_per_min: int = Field(..., ge=0)
    cancel_per_min: int = Field(..., ge=0)


class MaintenanceWindow(BaseModel):
    start: str
    end: str
    title: str | None = None


class GuardsConfig(BaseModel):
    cancel_on_disconnect: bool = True
    rate_limit: RateLimitConfig
    clock_skew_guard_ms: int = Field(200, ge=0)
    snapshot_diff_check: bool = True
    kill_caps: KillCapsConfig
    runaway_breaker: RunawayBreakerConfig
    maintenance_calendar: List[MaintenanceWindow] = Field(default_factory=list)


class NotionalCapsConfig(BaseModel):
    per_symbol_usd: float = Field(..., ge=0)
    per_venue_usd: float | None = Field(None, ge=0)
    total_usd: float = Field(..., ge=0)


class RiskConfig(BaseModel):
    notional_caps: NotionalCapsConfig
    max_day_drawdown_bps: int | None = None
    cross_venue_delta_abs_max_usd: float | None = None


class DerivRoutingConfig(BaseModel):
    rest: str
    ws: str


class DerivVenueConfig(BaseModel):
    id: str
    symbols: List[str]
    leverage: int = Field(ge=1, default=1)
    margin_type: str = "isolated"
    position_mode: str = "hedge"
    routing: DerivRoutingConfig

    @validator("position_mode")
    def validate_mode(cls, v: str) -> str:
        if v not in {"hedge", "one_way"}:
            raise ValueError("position_mode must be hedge or one_way")
        return v

    @validator("margin_type")
    def validate_margin(cls, v: str) -> str:
        if v not in {"isolated", "cross"}:
            raise ValueError("margin_type must be isolated or cross")
        return v


class ArbitrageLeg(BaseModel):
    venue: str
    symbol: str


class ArbitragePairConfig(BaseModel):
    long: ArbitrageLeg
    short: ArbitrageLeg


class ArbitragePolicies(BaseModel):
    min_edge_bps: float = 0.0
    max_latency_ms: int = Field(250, ge=0)
    max_leg_slippage_bps: float = 0.0
    prefer_maker: bool = False
    post_only_maker: bool = False
    partial_fill_policy: str = "reject_if_unhedged"

    @validator("partial_fill_policy")
    def validate_policy(cls, v: str) -> str:
        allowed = {"reject_if_unhedged", "hedge_remaining"}
        if v not in allowed:
            raise ValueError("partial_fill_policy invalid")
        return v


class ArbitrageConfig(BaseModel):
    pairs: List[ArbitragePairConfig]
    min_edge_bps: float = 0.0
    max_latency_ms: int = Field(250, ge=0)
    max_leg_slippage_bps: float = 0.0
    prefer_maker: bool = False
    post_only_maker: bool = False
    partial_fill_policy: str = "reject_if_unhedged"

    @validator("partial_fill_policy")
    def validate_policy(cls, v: str) -> str:
        allowed = {"reject_if_unhedged", "hedge_remaining"}
        if v not in allowed:
            raise ValueError("partial_fill_policy invalid")
        return v


class FeesManualConfig(BaseModel):
    maker_bps: float
    taker_bps: float


class FeesConfig(BaseModel):
    source: str = "auto"
    manual: Dict[str, FeesManualConfig] = Field(default_factory=dict)

    @validator("source")
    def validate_source(cls, v: str) -> str:
        allowed = {"auto", "manual"}
        if v not in allowed:
            raise ValueError("fees.source must be auto or manual")
        return v


class FundingConfig(BaseModel):
    include_next_window: bool = True
    avoid_window_minutes: int = Field(5, ge=0)


class ControlConfig(BaseModel):
    safe_mode: bool = True
    dry_run: bool = True
    two_man_rule: bool = True
    post_only: bool = True
    reduce_only: bool = False


class DerivativesConfig(BaseModel):
    venues: List[DerivVenueConfig]
    arbitrage: ArbitrageConfig
    fees: FeesConfig = Field(default_factory=FeesConfig)
    funding: FundingConfig = Field(default_factory=FundingConfig)

    @property
    def venue_by_id(self) -> Dict[str, DerivVenueConfig]:
        return {v.id: v for v in self.venues}


class StatusThresholds(BaseModel):
    slo: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    status: Dict[str, float] = Field(default_factory=dict)


class AppConfig(BaseModel):
    profile: str
    lang: str | None = None
    server: Dict[str, object] | None = None
    risk: RiskConfig | None = None
    guards: GuardsConfig | None = None
    control: ControlConfig | None = None
    derivatives: DerivativesConfig | None = None
    obs: Dict[str, object] | None = None
    status_thresholds_file: str | None = None


@dataclass
class LoadedConfig:
    path: Path
    data: AppConfig
    thresholds: StatusThresholds | None


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_app_config(path: str | Path) -> LoadedConfig:
    cfg_path = Path(path)
    payload = load_yaml(cfg_path)
    app_cfg = AppConfig.parse_obj(payload)

    thresholds: StatusThresholds | None = None
    if app_cfg.status_thresholds_file:
        thresh_path = (cfg_path.parent / app_cfg.status_thresholds_file).resolve()
        thresholds = StatusThresholds.parse_obj(load_yaml(thresh_path))

    return LoadedConfig(path=cfg_path, data=app_cfg, thresholds=thresholds)
