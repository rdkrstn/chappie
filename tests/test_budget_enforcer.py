"""Comprehensive tests for the budget enforcer engine.

Uses MemoryStore for all tests -- no Redis required.
pytest-asyncio with asyncio_mode="auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import pytest

from chappie.config import BudgetConfig
from chappie.engine.budget_enforcer import BudgetEnforcer, BudgetScope, estimate_cost
from chappie.exceptions import ChappieBudgetExceeded
from chappie.store.memory import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fast_config(
    default_budget: float = 100.0,
    reservation_ttl_sec: int = 120,
    alert_thresholds: list[float] | None = None,
) -> BudgetConfig:
    """Config with sensible test defaults."""
    return BudgetConfig(
        default_budget=default_budget,
        reservation_ttl_sec=reservation_ttl_sec,
        alert_thresholds=alert_thresholds or [0.5, 0.8, 0.9, 1.0],
    )


def _make_enforcer(
    store: MemoryStore | None = None,
    config: BudgetConfig | None = None,
) -> tuple[BudgetEnforcer, MemoryStore]:
    """Create a BudgetEnforcer with a fresh MemoryStore."""
    store = store or MemoryStore()
    config = config or _fast_config()
    return BudgetEnforcer(store=store, config=config), store


# ---------------------------------------------------------------------------
# 1. Reserve success -- budget has room, reservation succeeds
# ---------------------------------------------------------------------------


async def test_reserve_success():
    """A reservation within budget should succeed and return a Reservation."""
    enforcer, store = _make_enforcer(config=_fast_config(default_budget=100.0))

    reservation = await enforcer.reserve(BudgetScope.AGENT, "agent-1", 10.0)

    assert reservation.scope == "agent"
    assert reservation.scope_id == "agent-1"
    assert reservation.estimated_cost == 10.0
    assert reservation.id  # non-empty UUID

    # Verify spend was recorded
    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-1")
    assert status.spent == 10.0
    assert status.remaining == 90.0


# ---------------------------------------------------------------------------
# 2. Reserve exceeds budget -- raises ChappieBudgetExceeded
# ---------------------------------------------------------------------------


async def test_reserve_exceeds_budget():
    """Requesting more than available budget should raise ChappieBudgetExceeded."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=10.0))

    with pytest.raises(ChappieBudgetExceeded) as exc_info:
        await enforcer.reserve(BudgetScope.AGENT, "agent-1", 15.0)

    assert exc_info.value.limit == 10.0
    assert exc_info.value.spent == 0.0


# ---------------------------------------------------------------------------
# 3. Reconcile releases difference -- actual < estimated
# ---------------------------------------------------------------------------


async def test_reconcile_releases_difference():
    """When actual cost is less than estimated, the difference is released."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    reservation = await enforcer.reserve(BudgetScope.USER, "user-1", 20.0)

    # Spent should be 20.0 after reservation
    status = await enforcer.get_budget(BudgetScope.USER, "user-1")
    assert status.spent == 20.0

    # Actual cost was only 12.0
    await enforcer.reconcile(reservation, actual_cost=12.0)

    # Difference (8.0) should be released
    status = await enforcer.get_budget(BudgetScope.USER, "user-1")
    assert status.spent == 12.0
    assert status.remaining == 88.0


# ---------------------------------------------------------------------------
# 4. Reconcile charges extra -- actual > estimated
# ---------------------------------------------------------------------------


async def test_reconcile_charges_extra():
    """When actual cost exceeds estimate, the extra is charged."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    reservation = await enforcer.reserve(BudgetScope.AGENT, "agent-2", 10.0)
    await enforcer.reconcile(reservation, actual_cost=15.0)

    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-2")
    assert status.spent == 15.0
    assert status.remaining == 85.0


# ---------------------------------------------------------------------------
# 5. Release restores full amount -- failure releases full reservation
# ---------------------------------------------------------------------------


async def test_release_restores_full_amount():
    """Releasing a reservation should return the full estimated cost."""
    enforcer, store = _make_enforcer(config=_fast_config(default_budget=50.0))

    reservation = await enforcer.reserve(BudgetScope.TEAM, "team-1", 25.0)

    # Budget should show 25.0 spent
    status = await enforcer.get_budget(BudgetScope.TEAM, "team-1")
    assert status.spent == 25.0

    # Release returns the full amount
    await enforcer.release(reservation)

    status = await enforcer.get_budget(BudgetScope.TEAM, "team-1")
    assert status.spent == 0.0
    assert status.remaining == 50.0

    # Reservation key should be deleted
    res_key = f"chappie:reservation:{reservation.id}"
    assert not await store.exists(res_key)


# ---------------------------------------------------------------------------
# 6. Get budget shows correct values
# ---------------------------------------------------------------------------


async def test_get_budget_shows_correct_values():
    """get_budget should return accurate spent, limit, remaining, percentage."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=200.0))

    await enforcer.set_budget(BudgetScope.GLOBAL, "org-1", 200.0)
    await enforcer.reserve(BudgetScope.GLOBAL, "org-1", 50.0)

    status = await enforcer.get_budget(BudgetScope.GLOBAL, "org-1")

    assert status.scope == "global"
    assert status.scope_id == "org-1"
    assert status.spent == 50.0
    assert status.limit == 200.0
    assert status.remaining == 150.0
    assert status.percentage == 25.0


# ---------------------------------------------------------------------------
# 7. Set budget creates and updates
# ---------------------------------------------------------------------------


async def test_set_budget_creates_and_updates():
    """set_budget should create a new limit and allow updating it."""
    enforcer, _ = _make_enforcer()

    # Create new limit
    await enforcer.set_budget(BudgetScope.USER, "user-5", 500.0)
    status = await enforcer.get_budget(BudgetScope.USER, "user-5")
    assert status.limit == 500.0

    # Update existing limit
    await enforcer.set_budget(BudgetScope.USER, "user-5", 750.0)
    status = await enforcer.get_budget(BudgetScope.USER, "user-5")
    assert status.limit == 750.0


# ---------------------------------------------------------------------------
# 8. Threshold 50% fires -- info
# ---------------------------------------------------------------------------


async def test_threshold_50_fires():
    """Spending 50% of budget should fire the info threshold."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "agent-t1", 50.0)
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-t1")

    assert level == "info"


# ---------------------------------------------------------------------------
# 9. Threshold 80% fires -- warning
# ---------------------------------------------------------------------------


async def test_threshold_80_fires():
    """Spending 80% of budget should fire the warning threshold."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "agent-t2", 80.0)
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-t2")

    # Both 50% and 80% crossed; highest returned is "warning"
    assert level == "warning"


# ---------------------------------------------------------------------------
# 10. Threshold 100% fires -- critical
# ---------------------------------------------------------------------------


async def test_threshold_100_fires():
    """Spending 100% of budget should fire the critical threshold."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "agent-t3", 100.0)
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-t3")

    # All thresholds crossed; highest returned is "critical"
    assert level == "critical"


# ---------------------------------------------------------------------------
# 11. Threshold fires only once
# ---------------------------------------------------------------------------


async def test_threshold_fires_only_once():
    """The same threshold should not re-fire once it has been triggered."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "agent-t4", 50.0)

    # First check fires
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-t4")
    assert level == "info"

    # Second check with same spend should return None (already fired)
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-t4")
    assert level is None


# ---------------------------------------------------------------------------
# 12. Reset spend
# ---------------------------------------------------------------------------


async def test_reset_spend():
    """reset_spend should zero out the spend counter."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "agent-r1", 75.0)
    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-r1")
    assert status.spent == 75.0

    await enforcer.reset_spend(BudgetScope.AGENT, "agent-r1")

    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-r1")
    assert status.spent == 0.0
    assert status.remaining == 100.0


# ---------------------------------------------------------------------------
# 13. Multiple reservations accumulate correctly
# ---------------------------------------------------------------------------


async def test_multiple_reservations():
    """Sequential reservations should accumulate spend correctly."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    r1 = await enforcer.reserve(BudgetScope.AGENT, "agent-m1", 20.0)
    r2 = await enforcer.reserve(BudgetScope.AGENT, "agent-m1", 30.0)
    r3 = await enforcer.reserve(BudgetScope.AGENT, "agent-m1", 25.0)

    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-m1")
    assert status.spent == 75.0
    assert status.remaining == 25.0

    # Fourth reservation that would exceed budget
    with pytest.raises(ChappieBudgetExceeded):
        await enforcer.reserve(BudgetScope.AGENT, "agent-m1", 30.0)

    # Release one reservation and try again
    await enforcer.release(r2)

    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-m1")
    assert status.spent == 45.0

    # Now 30.0 should fit (45 + 30 = 75 <= 100)
    r4 = await enforcer.reserve(BudgetScope.AGENT, "agent-m1", 30.0)
    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-m1")
    assert status.spent == 75.0


# ---------------------------------------------------------------------------
# 14. Default budget when no limit set
# ---------------------------------------------------------------------------


async def test_default_budget_when_no_limit_set():
    """When no explicit limit is set, config.default_budget is used."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=42.0))

    # No set_budget call -- should use default
    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-d1")
    assert status.limit == 42.0
    assert status.spent == 0.0
    assert status.remaining == 42.0

    # Reservation should work against the default limit
    await enforcer.reserve(BudgetScope.AGENT, "agent-d1", 40.0)

    status = await enforcer.get_budget(BudgetScope.AGENT, "agent-d1")
    assert status.spent == 40.0
    assert status.remaining == 2.0

    # Exceeding the default should fail
    with pytest.raises(ChappieBudgetExceeded):
        await enforcer.reserve(BudgetScope.AGENT, "agent-d1", 5.0)


# ---------------------------------------------------------------------------
# 15. Reset spend clears fired thresholds
# ---------------------------------------------------------------------------


async def test_reset_spend_clears_fired_thresholds():
    """After reset, thresholds should be able to fire again."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    # Spend to 50% and fire threshold
    await enforcer.reserve(BudgetScope.AGENT, "agent-rt", 50.0)
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-rt")
    assert level == "info"

    # Reset
    await enforcer.reset_spend(BudgetScope.AGENT, "agent-rt")

    # Spend to 50% again
    await enforcer.reserve(BudgetScope.AGENT, "agent-rt", 50.0)

    # Threshold should fire again since reset cleared the flag
    level = await enforcer.check_thresholds(BudgetScope.AGENT, "agent-rt")
    assert level == "info"


# ---------------------------------------------------------------------------
# 16. Cost estimator
# ---------------------------------------------------------------------------


async def test_estimate_cost_returns_positive():
    """estimate_cost should return a positive float for valid input."""
    messages = [{"role": "user", "content": "Hello, how are you?"}]
    cost = estimate_cost(messages, model="gpt-4", max_tokens=1000)

    assert cost > 0
    assert isinstance(cost, float)


async def test_estimate_cost_scales_with_message_length():
    """Longer messages should produce higher cost estimates."""
    short_msg = [{"role": "user", "content": "Hi"}]
    long_msg = [{"role": "user", "content": "Hello " * 500}]

    short_cost = estimate_cost(short_msg, model="gpt-4", max_tokens=100)
    long_cost = estimate_cost(long_msg, model="gpt-4", max_tokens=100)

    assert long_cost > short_cost


# ---------------------------------------------------------------------------
# 17. Budget scopes are independent
# ---------------------------------------------------------------------------


async def test_scopes_are_independent():
    """Spending in one scope should not affect another scope."""
    enforcer, _ = _make_enforcer(config=_fast_config(default_budget=100.0))

    await enforcer.reserve(BudgetScope.AGENT, "id-1", 50.0)
    await enforcer.reserve(BudgetScope.USER, "id-1", 30.0)

    agent_status = await enforcer.get_budget(BudgetScope.AGENT, "id-1")
    user_status = await enforcer.get_budget(BudgetScope.USER, "id-1")

    assert agent_status.spent == 50.0
    assert user_status.spent == 30.0
