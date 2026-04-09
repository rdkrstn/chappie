"""Shared Pydantic models used across BudgetCtl sub-systems."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------


class LoopCheckResult(BaseModel):
    """Outcome of a single ``LoopDetector.check()`` call."""

    is_loop: bool
    strategy: str
    details: str
    agent_id: str


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitBreakerState(str, enum.Enum):
    """Three-state circuit breaker following the standard pattern."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerInfo(BaseModel):
    """Snapshot of an agent's circuit breaker state."""

    state: CircuitBreakerState
    reason: str
    tripped_at: datetime | None = None
    open_until: datetime | None = None
    error_count: int = 0


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class BudgetStatus(BaseModel):
    """Current spend vs. limit for a given scope."""

    scope: str
    scope_id: str
    spent: float
    limit: float
    remaining: float
    percentage: float


class Reservation(BaseModel):
    """A cost reservation that will be committed or released after the call."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    scope: str
    scope_id: str
    estimated_cost: float
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentInfo(BaseModel):
    """Aggregate stats for a single agent."""

    agent_id: str
    total_calls: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    cb_state: CircuitBreakerState = CircuitBreakerState.CLOSED
    last_seen: datetime | None = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class BudgetCtlEvent(BaseModel):
    """Generic event emitted by BudgetCtl for alerting / logging."""

    event_type: str
    agent_id: str
    data: dict = Field(default_factory=dict)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
