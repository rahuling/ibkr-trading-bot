"""
Configuration loading and validation.

Loads config.yaml using pydantic v2. Validation runs at startup — bad config
raises ValidationError immediately, never silently mid-session.

Usage:
    from bot.config import load_config
    config = load_config()          # loads config.yaml from cwd
    config = load_config("path/to/config.yaml")
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------

class RiskConfig(BaseModel):
    core_bucket_pct: float = 0.55
    tactical_bucket_pct: float = 0.20
    momentum_bucket_pct: float = 0.10
    reserve_pct: float = 0.15
    max_position_pct_of_bucket: float = 0.33
    max_underlying_pct: float = 0.40
    daily_loss_limit_pct: float = 0.015
    weekly_loss_limit_pct: float = 0.03
    monthly_loss_limit_pct: float = 0.08
    max_spread_loss: float = 600.0
    min_ivr_core: int = 30
    min_ivr_tactical: int = 35
    earnings_blackout_pre_days: int = 7
    earnings_blackout_post_days: int = 2
    pdt_warning_threshold: float = 30_000.0
    pdt_stop_threshold: float = 25_000.0

    @model_validator(mode="after")
    def buckets_sum_to_one(self) -> RiskConfig:
        total = (
            self.core_bucket_pct
            + self.tactical_bucket_pct
            + self.momentum_bucket_pct
            + self.reserve_pct
        )
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"Bucket allocations must sum to 1.0, got {total:.4f}. "
                f"(core={self.core_bucket_pct}, tactical={self.tactical_bucket_pct}, "
                f"momentum={self.momentum_bucket_pct}, reserve={self.reserve_pct})"
            )
        return self


class WatchlistConfig(BaseModel):
    core: List[str] = Field(default_factory=list)
    tactical: List[str] = Field(default_factory=list)


class ScannerConfig(BaseModel):
    top_n_candidates: int = 5
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)


class CSPConfig(BaseModel):
    dte_min: int = 30
    dte_max: int = 45
    target_delta: float = -0.27
    delta_tolerance: float = 0.03
    profit_close_pct: float = 0.50


class SpreadConfig(BaseModel):
    dte_min: int = 7
    dte_max: int = 21
    target_delta: float = -0.30
    delta_tolerance: float = 0.02
    spread_width: int = 5
    min_credit_to_width_ratio: float = 0.25
    profit_close_pct: float = 0.75


class CoveredCallConfig(BaseModel):
    dte_min: int = 7
    dte_max: int = 21
    target_delta: float = 0.28
    profit_close_pct: float = 0.75


class TradingConfig(BaseModel):
    csp: CSPConfig = Field(default_factory=CSPConfig)
    spread: SpreadConfig = Field(default_factory=SpreadConfig)
    covered_call: CoveredCallConfig = Field(default_factory=CoveredCallConfig)


class ExecutionConfig(BaseModel):
    order_blackout_open_mins: int = 15
    order_blackout_close_mins: int = 5
    reprice_wait_minutes: int = 5
    reprice_retry_wait_minutes: int = 3
    proposal_ttl_minutes: int = 120


class LeapSignalConfig(BaseModel):
    min_volume_ratio: float = 1.2
    max_pct_from_day_high: float = 1.0
    require_above_sma20: bool = True
    earnings_blackout_days: int = 5


class LeapConfig(BaseModel):
    min_dte: int = 300
    max_dte: int = 420
    target_delta: float = 0.78
    delta_tolerance: float = 0.05
    stop_loss_pct: float = 0.02
    profit_target_pct: float = 0.08
    max_extrinsic_pct: float = 0.25
    eod_entry_window_start: str = "15:30"
    eod_entry_window_end: str = "15:55"
    momentum_watchlist: List[str] = Field(default_factory=list)
    signal: LeapSignalConfig = Field(default_factory=LeapSignalConfig)


class AutomationConfig(BaseModel):
    level: int = 2
    l3_large_position_pct: float = 0.20

    @model_validator(mode="after")
    def level_is_valid(self) -> AutomationConfig:
        if self.level not in (1, 2, 3):
            raise ValueError(f"automation.level must be 1, 2, or 3 — got {self.level}")
        return self


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    risk: RiskConfig = Field(default_factory=RiskConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    leap: LeapConfig = Field(default_factory=LeapConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> AppConfig:
    """
    Load and validate config.yaml.

    Raises:
        FileNotFoundError: if config file does not exist
        pydantic.ValidationError: if any value fails validation (e.g. buckets != 1.0)

    This is intentionally strict — bad config should fail at startup,
    never silently mid-session.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path.absolute()}\n"
            f"Copy config.yaml.example to config.yaml and edit it."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError("config.yaml is empty")

    return AppConfig.model_validate(raw)
