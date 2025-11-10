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


class TradeWindowConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    start: str = Field(alias="from")
    end: str = Field(alias="to")
    tz: str | None = None
    reason: str | None = None


class PretradeConfig(BaseModel):
    allow_autofix: bool = True
    default_tz: str = "UTC"


class ExposureSideCapsConfig(BaseModel):
    LONG: float | None = None
    SHORT: float | None = None


class ExposureCapsEntry(BaseModel):
    max_abs_usdt: float | None = None
    per_side_max_abs_usdt: ExposureSideCapsConfig | None = None


class ExposureCapsConfig(BaseModel):
    default: ExposureCapsEntry = Field(default_factory=ExposureCapsEntry)
    per_symbol: Dict[str, ExposureCapsEntry] = Field(default_factory=dict)
    per_venue: Dict[str, Dict[str, ExposureCapsEntry]] = Field(default_factory=dict)


class MaintenanceScheduleWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    start: str = Field(alias="from")
    end: str = Field(alias="to")
    tz: str | None = None
    reason: str | None = None


class MaintenanceConfig(BaseModel):
    windows: List[MaintenanceScheduleWindow] = Field(default_factory=list)


class GuardrailsConfig(BaseModel):
    testnet_block_highrisk: bool = True
    blocklist: List[str] = Field(default_factory=list)


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


class RiskGovernorConfig(BaseModel):
    window_sec: int = Field(3600, ge=60)
    min_success_rate: float = Field(0.985, ge=0.0, le=1.0)
    max_order_error_rate: float = Field(0.01, ge=0.0, le=1.0)
    min_broker_state: str = Field("UP")
    hold_after_windows: int = Field(2, ge=1)

    @model_validator(mode="after")
    def _validate_state(self) -> "RiskGovernorConfig":
        allowed = {"UP", "DEGRADED", "DOWN"}
        state = (self.min_broker_state or "").upper()
        if state not in allowed:
            raise ValueError("min_broker_state must be one of UP, DEGRADED, DOWN")
        object.__setattr__(self, "min_broker_state", state)
        return self


class RiskConfig(BaseModel):
    notional_caps: NotionalCapsConfig
    max_day_drawdown_bps: int | None = None
    cross_venue_delta_abs_max_usd: float | None = None
    runaway: RunawayRiskConfig | None = None
    governor: RiskGovernorConfig | None = None


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


class StuckResolverConfig(BaseModel):
    enabled: bool = True
    pending_timeout_sec: float = Field(8.0, ge=0.0)
    cancel_grace_sec: float = Field(3.0, ge=0.0)
    max_retries: int = Field(3, ge=0)
    backoff_sec: List[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0])

    @field_validator("backoff_sec", mode="after")
    def _normalise_backoff(cls, values: List[float]) -> List[float]:
        cleaned: List[float] = []
        for entry in values:
            try:
                value = float(entry)
            except (TypeError, ValueError):
                continue
            if value < 0:
                continue
            cleaned.append(value)
        return cleaned or [1.0]


class ExecutionConfig(BaseModel):
    stuck_resolver: StuckResolverConfig = Field(default_factory=StuckResolverConfig)


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




class MarketRetryPolicy(BaseModel):
    max_attempts: int = Field(3, ge=1)
    backoff_s: float = Field(1.0, ge=0.0)


class MarketResyncConfig(BaseModel):
    max_snapshot_depth: int = Field(1000, ge=1)
    retry_policy: MarketRetryPolicy = Field(default_factory=MarketRetryPolicy)


class MarketWsConfig(BaseModel):
    hb_timeout_s: float = Field(5.0, ge=0.0)
    backoff_base_s: float = Field(0.25, gt=0.0)
    backoff_max_s: float = Field(30.0, gt=0.0)
    stable_window_s: float = Field(60.0, ge=0.0)


class MarketConfig(BaseModel):
    ws: MarketWsConfig = Field(default_factory=MarketWsConfig)
    resync: MarketResyncConfig = Field(default_factory=MarketResyncConfig)


class ChaosConfig(BaseModel):
    ws_drop_probability: float = Field(0.0, ge=0.0, le=1.0)
    rest_timeout_probability: float = Field(0.0, ge=0.0, le=1.0)
    order_delay_ms: int = Field(0, ge=0)


class ReconConfig(BaseModel):
    enabled: bool = Field(True)
    interval_sec: float = Field(15.0, ge=0.5)
    warn_notional_usd: float = Field(5.0, ge=0.0)
    critical_notional_usd: float = Field(25.0, ge=0.0)
    clear_after_ok_runs: int = Field(3, ge=1)
    max_divergence: float = Field(0.0, ge=0.0)
    diff_abs_usd_warn: float = Field(50.0, ge=0.0)
    diff_abs_usd_crit: float = Field(100.0, ge=0.0)
    diff_rel_warn: float = Field(0.05, ge=0.0)
    diff_rel_crit: float = Field(0.1, ge=0.0)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "ReconConfig":
        if self.diff_abs_usd_crit < self.diff_abs_usd_warn:
            raise ValueError("diff_abs_usd_crit must be >= diff_abs_usd_warn")
        if self.diff_rel_crit < self.diff_rel_warn:
            raise ValueError("diff_rel_crit must be >= diff_rel_warn")
        if self.critical_notional_usd < self.warn_notional_usd:
            raise ValueError("critical_notional_usd must be >= warn_notional_usd")
        return self


class ReadinessConfig(BaseModel):
    startup_timeout_sec: float = Field(120.0, ge=1.0)


class HealthConfig(BaseModel):
    guard_enabled: bool = Field(True)
    margin_ratio_warn: float = Field(0.75, ge=0.0, le=1.0)
    margin_ratio_critical: float = Field(0.85, ge=0.0, le=1.0)
    free_collateral_warn_usd: float = Field(100.0, ge=0.0)
    free_collateral_critical_usd: float = Field(10.0, ge=0.0)
    hysteresis_ok_windows: int = Field(2, ge=0)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "HealthConfig":
        if self.margin_ratio_critical < self.margin_ratio_warn:
            raise ValueError("margin_ratio_critical must be >= margin_ratio_warn")
        if self.free_collateral_critical_usd > self.free_collateral_warn_usd:
            raise ValueError(
                "free_collateral_critical_usd must be <= free_collateral_warn_usd"
            )
        return self


class AppConfig(BaseModel):
    profile: str
    market: MarketConfig | None = None
    lang: str | None = None
    server: Dict[str, object] | None = None
    risk: RiskConfig | None = None
    health: HealthConfig = Field(default_factory=HealthConfig)
    guards: GuardsConfig | None = None
    guardrails: GuardrailsConfig | None = None
    maintenance: MaintenanceConfig | None = None
    pretrade: PretradeConfig | None = None
    exposure_caps: ExposureCapsConfig | None = None
    control: ControlConfig | None = None
    execution: ExecutionConfig | None = None
    derivatives: DerivativesConfig | None = None
    tca: TcaConfig | None = None
    obs: Dict[str, object] | None = None
    status_thresholds_file: str | None = None
    chaos: ChaosConfig | None = None
    incident: IncidentConfig | None = None
    watchdog: BrokerWatchdogConfig | None = None
    recon: ReconConfig | None = None
    readiness: ReadinessConfig | None = None


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
    "TradeWindowConfig",
    "PretradeConfig",
    "ExposureSideCapsConfig",
    "ExposureCapsEntry",
    "ExposureCapsConfig",
    "MaintenanceScheduleWindow",
    "MaintenanceConfig",
    "GuardrailsConfig",
    "ThresholdBand",
    "BrokerWatchdogThresholds",
    "BrokerWatchdogConfig",
    "GuardsConfig",
    "NotionalCapsConfig",
    "RunawayRiskConfig",
    "RiskGovernorConfig",
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
    "StuckResolverConfig",
    "ExecutionConfig",
    "DerivativesConfig",
    "StatusThresholds",
    "ChaosConfig",
    "ReconConfig",
    "ReadinessConfig",
    "HealthConfig",
    "IncidentConfig",
    "MarketRetryPolicy",
    "MarketResyncConfig",
    "MarketWsConfig",
    "MarketConfig",
    "AppConfig",
    "LoadedConfig",
]
