from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator, ConfigDict, model_validator


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


class ThresholdBand(BaseModel):
    degraded: float = Field(..., ge=0.0)
    down: float = Field(..., ge=0.0)

    @model_validator(mode="after")
    def _validate_order(self) -> "ThresholdBand":
        if self.down < self.degraded:
            raise ValueError("down must be >= degraded")
        return self


class BrokerWatchdogThresholds(BaseModel):
    ws_lag_ms_p95: ThresholdBand
    ws_disconnects_per_min: ThresholdBand
    rest_5xx_rate: ThresholdBand
    rest_timeouts_rate: ThresholdBand = Field(
        default_factory=lambda: ThresholdBand(degraded=0.02, down=0.10)
    )
    order_reject_rate: ThresholdBand


class BrokerWatchdogConfig(BaseModel):
    auto_hold_on_down: bool = True
    block_on_down: bool = True
    error_budget_window_s: int = Field(600, ge=60)
    thresholds: BrokerWatchdogThresholds


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


class RunawayRiskConfig(BaseModel):
    max_cancels_per_min: int = Field(0, ge=0)
    cooldown_sec: int = Field(0, ge=0)


class RiskConfig(BaseModel):
    notional_caps: NotionalCapsConfig
    max_day_drawdown_bps: int | None = None
    cross_venue_delta_abs_max_usd: float | None = None
    runaway: RunawayRiskConfig | None = None


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

    @field_validator("position_mode")
    def validate_mode(cls, v: str) -> str:
        if v not in {"hedge", "one_way"}:
            raise ValueError("position_mode must be hedge or one_way")
        return v

    @field_validator("margin_type")
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

    @field_validator("partial_fill_policy")
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

    @field_validator("partial_fill_policy")
    def validate_policy(cls, v: str) -> str:
        allowed = {"reject_if_unhedged", "hedge_remaining"}
        if v not in allowed:
            raise ValueError("partial_fill_policy invalid")
        return v


class FeesManualConfig(BaseModel):
    maker_bps: float
    taker_bps: float
    vip_rebate_bps: float = 0.0


class FeesConfig(BaseModel):
    source: str = "auto"
    manual: Dict[str, FeesManualConfig] = Field(default_factory=dict)

    @field_validator("source")
    def validate_source(cls, v: str) -> str:
        allowed = {"auto", "manual"}
        if v not in allowed:
            raise ValueError("fees.source must be auto or manual")
        return v


class FundingConfig(BaseModel):
    include_next_window: bool = True
    avoid_window_minutes: int = Field(5, ge=0)


class TcaTierEntry(BaseModel):
    tier: str
    maker_bps: float
    taker_bps: float
    rebate_bps: float = 0.0
    notional_from: float = Field(0.0, ge=0.0)


class TcaImpactConfig(BaseModel):
    k: float = Field(0.0, ge=0.0)


class TcaConfig(BaseModel):
    tiers: Dict[str, List[TcaTierEntry]] = Field(default_factory=dict)
    impact: TcaImpactConfig = Field(default_factory=TcaImpactConfig)
    horizon_min: float = Field(60.0, ge=0.0)


class IncidentConfig(BaseModel):
    restore_on_start: bool = True

    model_config = ConfigDict(extra="ignore")


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


class ChaosConfig(BaseModel):
    ws_drop_probability: float = Field(0.0, ge=0.0, le=1.0)
    rest_timeout_probability: float = Field(0.0, ge=0.0, le=1.0)
    order_delay_ms: int = Field(0, ge=0)


class AppConfig(BaseModel):
    profile: str
    lang: str | None = None
    server: Dict[str, object] | None = None
    risk: RiskConfig | None = None
    guards: GuardsConfig | None = None
    control: ControlConfig | None = None
    derivatives: DerivativesConfig | None = None
    tca: TcaConfig | None = None
    obs: Dict[str, object] | None = None
    status_thresholds_file: str | None = None
    chaos: ChaosConfig | None = None
    incident: IncidentConfig | None = None
    watchdog: BrokerWatchdogConfig | None = None


@dataclass
class LoadedConfig:
    path: Path
    data: AppConfig
    thresholds: StatusThresholds | None


__all__ = [
    "RateLimitConfig",
    "KillCapsConfig",
    "RunawayBreakerConfig",
    "MaintenanceWindow",
    "ThresholdBand",
    "BrokerWatchdogThresholds",
    "BrokerWatchdogConfig",
    "GuardsConfig",
    "NotionalCapsConfig",
    "RiskConfig",
    "DerivRoutingConfig",
    "DerivVenueConfig",
    "ArbitrageLeg",
    "ArbitragePairConfig",
    "ArbitragePolicies",
    "ArbitrageConfig",
    "FeesManualConfig",
    "FeesConfig",
    "FundingConfig",
    "TcaConfig",
    "TcaImpactConfig",
    "TcaTierEntry",
    "ControlConfig",
    "DerivativesConfig",
    "StatusThresholds",
    "ChaosConfig",
    "IncidentConfig",
    "AppConfig",
    "LoadedConfig",
]
