"""Shared fixtures for the Chappie test suite."""

from __future__ import annotations

import pytest

from chappie.config import BudgetConfig, CircuitBreakerConfig, LoopDetectorConfig
from chappie.store.memory import MemoryStore


# ---------------------------------------------------------------------------
# Store fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_store() -> MemoryStore:
    """Fresh in-memory store for each test."""
    return MemoryStore()


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_config() -> LoopDetectorConfig:
    """Loop detector config with tight thresholds for fast test feedback."""
    return LoopDetectorConfig(
        window_size=10,
        repeat_threshold=3,
        cycle_max_period=4,
        velocity_window_sec=60,
        velocity_multiplier=5.0,
    )


@pytest.fixture
def cb_config() -> CircuitBreakerConfig:
    return CircuitBreakerConfig()


@pytest.fixture
def budget_config() -> BudgetConfig:
    return BudgetConfig()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_litellm_kwargs(
    model: str = "gpt-4",
    messages: list | None = None,
    agent_id: str = "test-agent",
    response_cost: float = 0.01,
) -> dict:
    """Build a dict shaped like the kwargs LiteLLM passes to callbacks.

    This helper keeps individual tests concise and makes it easy to
    override only the fields that matter for each scenario.
    """
    if messages is None:
        messages = [{"role": "user", "content": "Hello"}]
    return {
        "model": model,
        "messages": messages,
        "litellm_params": {"metadata": {"agent_id": agent_id}},
        "standard_logging_object": {
            "response_cost": response_cost,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "model": model,
        },
    }
