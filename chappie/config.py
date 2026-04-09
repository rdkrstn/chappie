"""Chappie configuration via Pydantic BaseSettings.

All settings load from environment variables with the CHAPPIE_ prefix.
Nested models use double-underscore separators:
    CHAPPIE_LOOP_DETECTION__WINDOW_SIZE=30
    CHAPPIE_CIRCUIT_BREAKER__ERROR_THRESHOLD=10
    CHAPPIE_BUDGETS__DEFAULT_BUDGET=500
    CHAPPIE_ALERTS__SLACK_WEBHOOK_URL=https://hooks.slack.com/...
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class LoopDetectorConfig(BaseModel):
    """Tuning knobs for the three loop-detection strategies."""

    # Strategy A: Hash Dedup
    window_size: int = 20
    repeat_threshold: int = 3

    # Strategy B: Cycle Detection
    cycle_max_period: int = 4

    # Strategy C: Token Velocity Anomaly
    velocity_window_sec: int = 60
    velocity_multiplier: float = 5.0


class CircuitBreakerConfig(BaseModel):
    """Per-agent circuit breaker settings."""

    error_threshold: int = 5
    error_window_sec: int = 60
    cooldown_sec: int = 300
    half_open_max_calls: int = 1


class BudgetConfig(BaseModel):
    """Spend-limit defaults applied when no per-agent override exists."""

    default_budget: float = 100.0
    reset_period: Literal["daily", "weekly", "monthly"] = "monthly"
    reservation_ttl_sec: int = 120
    alert_thresholds: list[float] = Field(
        default_factory=lambda: [0.5, 0.8, 0.9, 1.0],
    )


class AlertConfig(BaseModel):
    """Where Chappie sends budget/loop/circuit alerts."""

    slack_webhook_url: str | None = None
    webhook_url: str | None = None
    enabled: bool = True


class ChappieConfig(BaseSettings):
    """Root configuration object.

    Reads from env vars prefixed with ``CHAPPIE_``.
    Nested values use ``__`` as separator, e.g.
    ``CHAPPIE_LOOP_DETECTION__WINDOW_SIZE=30``.
    """

    mode: Literal["observe", "enforce"] = "observe"
    redis_url: str | None = None
    on_redis_failure: Literal["open", "closed"] = "open"
    api_port: int = 8787

    loop_detection: LoopDetectorConfig = Field(
        default_factory=LoopDetectorConfig,
    )
    circuit_breaker: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
    )
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    model_config = {
        "env_prefix": "CHAPPIE_",
        "env_nested_delimiter": "__",
        "case_sensitive": False,
    }

    @classmethod
    def from_env(cls) -> ChappieConfig:
        """Convenience factory -- identical to ``ChappieConfig()`` but
        makes the intent explicit at call sites."""
        return cls()
