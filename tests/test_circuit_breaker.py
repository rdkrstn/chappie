"""Comprehensive tests for the circuit breaker engine.

Uses MemoryStore for all tests -- no Redis required.
pytest-asyncio with asyncio_mode="auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import time

import pytest

from budgetctl.config import CircuitBreakerConfig
from budgetctl.engine.circuit_breaker import CircuitBreaker, TripReason
from budgetctl.models import CircuitBreakerState
from budgetctl.store.memory import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fast_config(
    error_threshold: int = 3,
    error_window_sec: int = 60,
    cooldown_sec: int = 2,
    half_open_max_calls: int = 1,
) -> CircuitBreakerConfig:
    """Config with short cooldowns for fast test feedback."""
    return CircuitBreakerConfig(
        error_threshold=error_threshold,
        error_window_sec=error_window_sec,
        cooldown_sec=cooldown_sec,
        half_open_max_calls=half_open_max_calls,
    )


def _make_cb(
    store: MemoryStore | None = None,
    config: CircuitBreakerConfig | None = None,
) -> tuple[CircuitBreaker, MemoryStore]:
    """Create a CircuitBreaker with a fresh MemoryStore."""
    store = store or MemoryStore()
    config = config or _fast_config()
    return CircuitBreaker(store=store, config=config), store


# ---------------------------------------------------------------------------
# 1. Default state is CLOSED
# ---------------------------------------------------------------------------


async def test_default_state_is_closed():
    """A brand new agent with no history should be in CLOSED state."""
    cb, _ = _make_cb()

    info = await cb.check("agent-new")

    assert info.state == CircuitBreakerState.CLOSED
    assert info.reason == ""
    assert info.tripped_at is None
    assert info.open_until is None
    assert info.error_count == 0


# ---------------------------------------------------------------------------
# 2. Trip changes state to OPEN
# ---------------------------------------------------------------------------


async def test_trip_changes_state_to_open():
    """Calling trip() should move the agent to OPEN state."""
    cb, _ = _make_cb()

    await cb.trip("agent-1", TripReason.LOOP_DETECTED, "test loop")
    info = await cb.check("agent-1")

    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == TripReason.LOOP_DETECTED.value
    assert info.tripped_at is not None
    assert info.open_until is not None


# ---------------------------------------------------------------------------
# 3. OPEN rejects immediately
# ---------------------------------------------------------------------------


async def test_open_rejects_immediately():
    """check() on an OPEN agent should return OPEN without delay."""
    cb, _ = _make_cb()

    await cb.trip("agent-1", TripReason.MANUAL, "blocked by operator")
    info = await cb.check("agent-1")

    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == TripReason.MANUAL.value


# ---------------------------------------------------------------------------
# 4. Suspended flag set on trip
# ---------------------------------------------------------------------------


async def test_suspended_flag_set_on_trip():
    """trip() must set the chappie:suspended:{agent_id} key."""
    cb, store = _make_cb()

    await cb.trip("agent-1", TripReason.BUDGET_EXCEEDED)
    exists = await store.exists("budgetctl:suspended:agent-1")

    assert exists is True


# ---------------------------------------------------------------------------
# 5. Cooldown transitions to HALF_OPEN
# ---------------------------------------------------------------------------


async def test_cooldown_transitions_to_half_open():
    """After the cooldown expires, check() should return HALF_OPEN."""
    # Use a 1-second cooldown so the test finishes fast.
    config = _fast_config(cooldown_sec=1)
    cb, store = _make_cb(config=config)

    await cb.trip("agent-1", TripReason.LOOP_DETECTED, "looping")

    # Verify currently OPEN
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN

    # Simulate cooldown expiry by directly expiring the suspended key.
    # The MemoryStore uses time.monotonic() internally, so we manipulate
    # its _expiry dict to move the deadline into the past.
    suspended_key = "budgetctl:suspended:agent-1"
    store._expiry[suspended_key] = time.monotonic() - 1

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.HALF_OPEN


# ---------------------------------------------------------------------------
# 6. HALF_OPEN + success -> CLOSED
# ---------------------------------------------------------------------------


async def test_half_open_success_closes():
    """record_success() in HALF_OPEN should transition to CLOSED."""
    config = _fast_config(cooldown_sec=1)
    cb, store = _make_cb(config=config)

    # Trip, then expire the cooldown
    await cb.trip("agent-1", TripReason.ERROR_THRESHOLD)
    store._expiry["budgetctl:suspended:agent-1"] = time.monotonic() - 1

    # Confirm HALF_OPEN
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.HALF_OPEN

    # Record a success -- should close
    await cb.record_success("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED
    assert info.reason == ""


# ---------------------------------------------------------------------------
# 7. HALF_OPEN + failure -> OPEN again
# ---------------------------------------------------------------------------


async def test_half_open_failure_reopens():
    """record_failure() in HALF_OPEN should transition back to OPEN."""
    config = _fast_config(cooldown_sec=1)
    cb, store = _make_cb(config=config)

    # Trip, then expire the cooldown to reach HALF_OPEN
    await cb.trip("agent-1", TripReason.LOOP_DETECTED)
    store._expiry["budgetctl:suspended:agent-1"] = time.monotonic() - 1

    # Force transition to HALF_OPEN
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.HALF_OPEN

    # Record a failure -- should re-open
    await cb.record_failure("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN


# ---------------------------------------------------------------------------
# 8. Error threshold trips the CB
# ---------------------------------------------------------------------------


async def test_error_threshold_trips():
    """N errors within the window should trip the circuit breaker."""
    config = _fast_config(error_threshold=3, error_window_sec=60)
    cb, _ = _make_cb(config=config)

    # Agent starts CLOSED
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED

    # Record 3 failures (= threshold)
    await cb.record_failure("agent-1")
    await cb.record_failure("agent-1")
    await cb.record_failure("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == TripReason.ERROR_THRESHOLD.value


# ---------------------------------------------------------------------------
# 9. Errors outside the window don't count
# ---------------------------------------------------------------------------


async def test_errors_outside_window_dont_count():
    """Old errors that fall outside error_window_sec should be evicted."""
    config = _fast_config(error_threshold=3, error_window_sec=60)
    cb, _ = _make_cb(config=config)

    # Manually inject 2 "old" error timestamps into the window.
    old_time = time.time() - 120  # 2 minutes ago (outside 60s window)
    cb._error_windows["agent-1"].append(old_time)
    cb._error_windows["agent-1"].append(old_time)

    # Record 1 new failure -- total within window should be 1, not 3
    await cb.record_failure("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED


# ---------------------------------------------------------------------------
# 10. Reset clears all state
# ---------------------------------------------------------------------------


async def test_reset_clears_all_state():
    """reset() should clear the HASH, suspended key, and error window."""
    cb, store = _make_cb()

    # Trip the agent
    await cb.trip("agent-1", TripReason.MANUAL, "test reset")

    # Confirm OPEN
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN

    # Add some errors to the window
    cb._error_windows["agent-1"].append(time.time())

    # Reset
    await cb.reset("agent-1")

    # Verify everything is clean
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED
    assert info.reason == ""

    # Suspended key should be gone
    assert await store.exists("budgetctl:suspended:agent-1") is False

    # HASH should be gone
    data = await store.hgetall("budgetctl:cb:agent-1")
    assert data == {}

    # Error window should be empty
    assert "agent-1" not in cb._error_windows


# ---------------------------------------------------------------------------
# 11. Trip with different reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        TripReason.LOOP_DETECTED,
        TripReason.ERROR_THRESHOLD,
        TripReason.BUDGET_EXCEEDED,
        TripReason.MANUAL,
    ],
)
async def test_trip_with_different_reasons(reason: TripReason):
    """Each TripReason should be stored and returned correctly."""
    cb, _ = _make_cb()

    await cb.trip("agent-1", reason, f"details for {reason.value}")
    info = await cb.check("agent-1")

    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == reason.value


# ---------------------------------------------------------------------------
# 12. record_success on CLOSED is a no-op
# ---------------------------------------------------------------------------


async def test_closed_success_is_noop():
    """record_success() on a CLOSED agent should change nothing."""
    cb, store = _make_cb()

    # No state yet -- effectively CLOSED
    await cb.record_success("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED

    # Also test with explicit CLOSED state in the HASH
    await store.hset("budgetctl:cb:agent-1", {"state": "closed", "reason": ""})
    await cb.record_success("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED


# ---------------------------------------------------------------------------
# 13. get_all_states returns all known agents
# ---------------------------------------------------------------------------


async def test_get_all_states():
    """get_all_states() should return a dict of all agents with CB state."""
    cb, _ = _make_cb()

    # Trip two agents
    await cb.trip("agent-a", TripReason.LOOP_DETECTED)
    await cb.trip("agent-b", TripReason.BUDGET_EXCEEDED)

    states = await cb.get_all_states()

    assert len(states) == 2
    assert "agent-a" in states
    assert "agent-b" in states
    assert states["agent-a"].state == CircuitBreakerState.OPEN
    assert states["agent-b"].state == CircuitBreakerState.OPEN
    assert states["agent-a"].reason == TripReason.LOOP_DETECTED.value
    assert states["agent-b"].reason == TripReason.BUDGET_EXCEEDED.value


# ---------------------------------------------------------------------------
# 14. Multiple agents are independent
# ---------------------------------------------------------------------------


async def test_multiple_agents_independent():
    """Tripping agent A must not affect agent B."""
    cb, _ = _make_cb()

    await cb.trip("agent-a", TripReason.MANUAL, "blocked")

    # Agent A is OPEN
    info_a = await cb.check("agent-a")
    assert info_a.state == CircuitBreakerState.OPEN

    # Agent B should still be CLOSED
    info_b = await cb.check("agent-b")
    assert info_b.state == CircuitBreakerState.CLOSED


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_get_all_states_empty():
    """get_all_states() with no agents should return an empty dict."""
    cb, _ = _make_cb()
    states = await cb.get_all_states()
    assert states == {}


async def test_double_trip_updates_reason():
    """Tripping an already-OPEN agent should update the reason."""
    cb, _ = _make_cb()

    await cb.trip("agent-1", TripReason.LOOP_DETECTED, "first trip")
    await cb.trip("agent-1", TripReason.BUDGET_EXCEEDED, "second trip")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == TripReason.BUDGET_EXCEEDED.value


async def test_reset_then_trip_works():
    """After a reset, the agent can be tripped again cleanly."""
    cb, _ = _make_cb()

    await cb.trip("agent-1", TripReason.MANUAL)
    await cb.reset("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED

    await cb.trip("agent-1", TripReason.LOOP_DETECTED)

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN
    assert info.reason == TripReason.LOOP_DETECTED.value


async def test_errors_below_threshold_stay_closed():
    """Fewer errors than the threshold should keep the agent CLOSED."""
    config = _fast_config(error_threshold=5, error_window_sec=60)
    cb, _ = _make_cb(config=config)

    # Record 4 failures (below threshold of 5)
    for _ in range(4):
        await cb.record_failure("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED


async def test_record_failure_on_closed_agent_with_no_state():
    """record_failure on a completely new agent should track errors."""
    cb, _ = _make_cb()

    # Single failure -- should not trip (threshold=3)
    await cb.record_failure("agent-new")

    info = await cb.check("agent-new")
    assert info.state == CircuitBreakerState.CLOSED


async def test_full_lifecycle():
    """Walk through the complete lifecycle: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
    config = _fast_config(error_threshold=2, cooldown_sec=1)
    cb, store = _make_cb(config=config)

    # Start CLOSED
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED

    # 2 errors -> trips
    await cb.record_failure("agent-1")
    await cb.record_failure("agent-1")

    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.OPEN

    # Expire cooldown -> HALF_OPEN
    store._expiry["budgetctl:suspended:agent-1"] = time.monotonic() - 1
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.HALF_OPEN

    # Success -> CLOSED
    await cb.record_success("agent-1")
    info = await cb.check("agent-1")
    assert info.state == CircuitBreakerState.CLOSED
