"""Budget enforcer engine -- atomic spend reservations and threshold alerts.

Implements the reservation pattern for pre-call budget enforcement:

    1. ``reserve()``    -- atomically check budget and hold estimated cost
    2. LLM call happens
    3. ``reconcile()``  -- adjust hold to actual cost (release diff or charge extra)
    -or-
    3. ``release()``    -- full release on failure / cancellation

The reservation is atomic: a Lua script (or its in-memory simulation)
checks ``spent + estimated <= limit`` and increments in one round-trip.
Reservations carry a TTL so orphaned holds do not permanently reduce
available budget.

Budget scopes
-------------
Each budget tracks a ``(scope, scope_id)`` pair.  Four scopes exist:

- ``GLOBAL``  -- organisation-wide ceiling
- ``TEAM``    -- department or team
- ``USER``    -- individual human operator
- ``AGENT``   -- single AI agent

Redis key schema
----------------
::

    budgetctl:budget:{scope}:{scope_id}:spent     -> STRING  (INCRBYFLOAT)
    budgetctl:budget:{scope}:{scope_id}:limit     -> STRING  (budget cap in USD)
    budgetctl:reservation:{reservation_id}        -> STRING  (estimated cost, with TTL)
    budgetctl:budget:fired:{scope}:{scope_id}:{threshold}  -> STRING  ("1")
"""

from __future__ import annotations

import enum
import logging
from typing import Any

from budgetctl.config import BudgetConfig
from budgetctl.exceptions import BudgetCtlBudgetExceeded
from budgetctl.models import BudgetStatus, Reservation
from budgetctl.store import StoreInterface

logger = logging.getLogger("budgetctl.budget_enforcer")


# ---------------------------------------------------------------------------
# Scope enum
# ---------------------------------------------------------------------------


class BudgetScope(str, enum.Enum):
    """Hierarchy level at which a budget is tracked."""

    GLOBAL = "global"
    TEAM = "team"
    USER = "user"
    AGENT = "agent"


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

_BUDGET_PREFIX = "budgetctl:budget"
_RESERVATION_PREFIX = "budgetctl:reservation"


def _spent_key(scope: BudgetScope, scope_id: str) -> str:
    return f"{_BUDGET_PREFIX}:{scope.value}:{scope_id}:spent"


def _limit_key(scope: BudgetScope, scope_id: str) -> str:
    return f"{_BUDGET_PREFIX}:{scope.value}:{scope_id}:limit"


def _reservation_key(reservation_id: str) -> str:
    return f"{_RESERVATION_PREFIX}:{reservation_id}"


def _fired_key(scope: BudgetScope, scope_id: str, threshold: float) -> str:
    return f"{_BUDGET_PREFIX}:fired:{scope.value}:{scope_id}:{threshold}"


# ---------------------------------------------------------------------------
# Lua script for atomic reservation
# ---------------------------------------------------------------------------

_RESERVE_LUA = """\
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
local cost = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if spent + cost > limit then
    return {0, tostring(spent), tostring(limit)}
end
redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[4]))
return {1, tostring(spent + cost), tostring(limit)}
"""


# ---------------------------------------------------------------------------
# Threshold levels
# ---------------------------------------------------------------------------

_THRESHOLD_LABELS: dict[float, str] = {
    0.5: "info",
    0.8: "warning",
    0.9: "urgent",
    1.0: "critical",
}


# ---------------------------------------------------------------------------
# Cost estimator helper
# ---------------------------------------------------------------------------


def estimate_cost(
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int = 4096,
) -> float:
    """Estimate cost before an LLM call.

    Uses a rough heuristic:
    - Input tokens: ``len(str(messages)) / 4`` (char-to-token ratio)
    - Output tokens: ``max_tokens`` (worst case)
    - Price: looks up ``litellm.model_cost`` if available, otherwise
      falls back to $0.01 per 1K tokens for both input and output.

    Returns the estimated cost in USD.
    """
    input_chars = len(str(messages))
    input_tokens = input_chars / 4
    output_tokens = max_tokens

    # Try litellm price lookup
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    try:
        import litellm  # noqa: F811

        cost_map = getattr(litellm, "model_cost", None) or {}
        model_info = cost_map.get(model, {})
        input_cost_per_token = model_info.get("input_cost_per_token")
        output_cost_per_token = model_info.get("output_cost_per_token")
    except ImportError:
        pass

    if input_cost_per_token is None:
        input_cost_per_token = 0.01 / 1000  # $0.01 per 1K tokens default
    if output_cost_per_token is None:
        output_cost_per_token = 0.01 / 1000

    cost = (input_tokens * input_cost_per_token) + (output_tokens * output_cost_per_token)
    return round(cost, 8)


# ---------------------------------------------------------------------------
# Budget Enforcer
# ---------------------------------------------------------------------------


class BudgetEnforcer:
    """Atomic budget reservation and threshold alerting.

    Parameters
    ----------
    store:
        Async key-value store (Redis or MemoryStore).
    config:
        Budget tuning knobs -- default limit, reservation TTL, thresholds.
    """

    def __init__(
        self,
        store: StoreInterface,
        config: BudgetConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or BudgetConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reserve(
        self,
        scope: BudgetScope,
        scope_id: str,
        estimated_cost: float,
    ) -> Reservation:
        """Atomically reserve ``estimated_cost`` against the budget.

        Steps:

        1. Read the budget limit for this scope/id (falls back to
           ``config.default_budget`` if none has been set).
        2. Execute the Lua script: atomically checks that
           ``spent + estimated_cost <= limit``.  If yes, increments
           ``spent`` and writes the reservation key with a TTL.
        3. Return a :class:`Reservation` on success.

        Raises :class:`BudgetCtlBudgetExceeded` if the budget cannot
        absorb the estimated cost.
        """
        limit = await self._get_limit(scope, scope_id)

        reservation = Reservation(
            scope=scope.value,
            scope_id=scope_id,
            estimated_cost=estimated_cost,
        )

        spent_k = _spent_key(scope, scope_id)
        res_k = _reservation_key(reservation.id)

        result = await self._store.eval_lua(
            script=_RESERVE_LUA,
            keys=[spent_k, res_k],
            args=[
                str(estimated_cost),
                str(limit),
                reservation.id,
                str(self._config.reservation_ttl_sec),
            ],
        )

        success = int(result[0])
        current_spent = float(result[1])

        if not success:
            logger.warning(
                "Budget exceeded for %s:%s -- spent=%.4f limit=%.4f requested=%.4f",
                scope.value,
                scope_id,
                current_spent,
                limit,
                estimated_cost,
            )
            raise BudgetCtlBudgetExceeded(
                agent_id=f"{scope.value}:{scope_id}",
                spent=current_spent,
                limit=limit,
            )

        logger.info(
            "Reserved %.4f for %s:%s (spent now %.4f / %.4f)",
            estimated_cost,
            scope.value,
            scope_id,
            current_spent,
            limit,
        )
        return reservation

    async def reconcile(
        self,
        reservation: Reservation,
        actual_cost: float,
    ) -> None:
        """Settle a reservation after the LLM call completes.

        - If ``actual_cost < estimated``: release the difference back.
        - If ``actual_cost > estimated``: charge the extra amount.
        - In both cases the reservation key is deleted.
        """
        scope = BudgetScope(reservation.scope)
        spent_k = _spent_key(scope, reservation.scope_id)
        res_k = _reservation_key(reservation.id)

        diff = actual_cost - reservation.estimated_cost
        if diff != 0.0:
            await self._store.incr_float(spent_k, diff)
            logger.info(
                "Reconciled reservation %s: estimated=%.4f actual=%.4f diff=%+.4f",
                reservation.id,
                reservation.estimated_cost,
                actual_cost,
                diff,
            )

        await self._store.delete(res_k)

    async def release(self, reservation: Reservation) -> None:
        """Full release on failure or cancellation.

        Adds the entire ``estimated_cost`` back to available budget
        and deletes the reservation key.
        """
        scope = BudgetScope(reservation.scope)
        spent_k = _spent_key(scope, reservation.scope_id)
        res_k = _reservation_key(reservation.id)

        # Decrement spent by the full estimated cost
        await self._store.incr_float(spent_k, -reservation.estimated_cost)
        await self._store.delete(res_k)

        logger.info(
            "Released reservation %s: %.4f returned to %s:%s",
            reservation.id,
            reservation.estimated_cost,
            reservation.scope,
            reservation.scope_id,
        )

    async def get_budget(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> BudgetStatus:
        """Return current spend, limit, remaining, and percentage."""
        limit = await self._get_limit(scope, scope_id)
        spent = await self._get_spent(scope, scope_id)
        remaining = max(limit - spent, 0.0)
        percentage = (spent / limit * 100.0) if limit > 0 else 0.0

        return BudgetStatus(
            scope=scope.value,
            scope_id=scope_id,
            spent=round(spent, 4),
            limit=round(limit, 4),
            remaining=round(remaining, 4),
            percentage=round(percentage, 2),
        )

    async def set_budget(
        self,
        scope: BudgetScope,
        scope_id: str,
        limit: float,
    ) -> None:
        """Set or update the budget limit for a scope/id pair."""
        limit_k = _limit_key(scope, scope_id)
        await self._store.set(limit_k, str(limit))
        logger.info(
            "Budget limit set for %s:%s = $%.4f",
            scope.value,
            scope_id,
            limit,
        )

    async def check_thresholds(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> str | None:
        """Check spend against configured alert thresholds.

        Thresholds default to ``[0.5, 0.8, 0.9, 1.0]`` and map to
        severity levels: ``info``, ``warning``, ``urgent``, ``critical``.

        Each threshold fires at most once per budget period.  A fired
        threshold is tracked via a key
        ``chappie:budget:fired:{scope}:{scope_id}:{threshold}``.

        Returns the highest crossed threshold label, or ``None`` if
        no new threshold was crossed.
        """
        limit = await self._get_limit(scope, scope_id)
        spent = await self._get_spent(scope, scope_id)

        if limit <= 0:
            return None

        ratio = spent / limit
        highest_label: str | None = None

        for threshold in sorted(self._config.alert_thresholds):
            if ratio < threshold:
                break

            label = _THRESHOLD_LABELS.get(threshold)
            if label is None:
                continue

            fired_k = _fired_key(scope, scope_id, threshold)
            already_fired = await self._store.exists(fired_k)
            if already_fired:
                continue

            # Mark as fired
            await self._store.set(fired_k, "1")
            highest_label = label

            logger.info(
                "Threshold %.0f%% (%s) crossed for %s:%s -- spent=%.4f limit=%.4f",
                threshold * 100,
                label,
                scope.value,
                scope_id,
                spent,
                limit,
            )

        return highest_label

    async def reset_spend(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> None:
        """Reset spend to zero for budget period resets.

        Also clears all fired-threshold flags so they can re-fire
        in the new period.
        """
        spent_k = _spent_key(scope, scope_id)
        await self._store.set(spent_k, "0")

        # Clear all fired threshold flags
        for threshold in self._config.alert_thresholds:
            fired_k = _fired_key(scope, scope_id, threshold)
            await self._store.delete(fired_k)

        logger.info("Spend reset for %s:%s", scope.value, scope_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_limit(self, scope: BudgetScope, scope_id: str) -> float:
        """Read the budget limit, falling back to config default."""
        limit_k = _limit_key(scope, scope_id)
        raw = await self._store.get(limit_k)
        if raw is not None:
            return float(raw)
        return self._config.default_budget

    async def _get_spent(self, scope: BudgetScope, scope_id: str) -> float:
        """Read the current spend total."""
        spent_k = _spent_key(scope, scope_id)
        raw = await self._store.get(spent_k)
        if raw is not None:
            return float(raw)
        return 0.0
