"""Circuit breaker engine -- blocks misbehaving agents.

Implements the standard three-state circuit breaker pattern:

    CLOSED  --[trip]--> OPEN --[cooldown expires]--> HALF_OPEN
    HALF_OPEN --[success]--> CLOSED
    HALF_OPEN --[failure]--> OPEN

The breaker can trip for four reasons:
    - LOOP_DETECTED:    Loop detector flagged the agent
    - ERROR_THRESHOLD:  Too many failures in a sliding window
    - BUDGET_EXCEEDED:  Spend limit hit
    - MANUAL:           Operator intervention

Fast-path rejection uses a single ``EXISTS`` check on a Redis key with
a TTL equal to the cooldown period.  This avoids reading the full HASH
on every call for suspended agents.

Error counting uses an in-memory sliding window (per-process, no Redis
round-trip).  The window tracks timestamps of recent failures and
evicts entries older than ``error_window_sec``.

Redis key schema:
    budgetctl:cb:{agent_id}          -> HASH  (state, reason, details,
                                               tripped_at, open_until,
                                               error_count)
    budgetctl:suspended:{agent_id}   -> STRING "1" with TTL = cooldown_sec
    budgetctl:cb:agents              -> HASH  (agent_id -> "1")
                                       Simulated set via HASH because
                                       the StoreInterface has no native
                                       set operations.
"""

from __future__ import annotations

import enum
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from budgetctl.config import CircuitBreakerConfig
from budgetctl.models import CircuitBreakerInfo, CircuitBreakerState
from budgetctl.store import StoreInterface

logger = logging.getLogger("budgetctl.circuit_breaker")

# Key prefixes ---------------------------------------------------------

_CB_PREFIX = "budgetctl:cb:"
_SUSPENDED_PREFIX = "budgetctl:suspended:"
_AGENTS_KEY = "budgetctl:cb:agents"


def _cb_key(agent_id: str) -> str:
    return f"{_CB_PREFIX}{agent_id}"


def _suspended_key(agent_id: str) -> str:
    return f"{_SUSPENDED_PREFIX}{agent_id}"


# Trip reasons ---------------------------------------------------------


class TripReason(str, enum.Enum):
    """Why a circuit breaker tripped."""

    LOOP_DETECTED = "loop_detected"
    ERROR_THRESHOLD = "error_threshold"
    BUDGET_EXCEEDED = "budget_exceeded"
    MANUAL = "manual"


# Circuit breaker ------------------------------------------------------


class CircuitBreaker:
    """Per-agent circuit breaker with fast-path rejection.

    Parameters
    ----------
    store:
        Async key-value store (Redis or MemoryStore).
    config:
        Tuning knobs -- error thresholds, cooldown duration, etc.
    """

    def __init__(
        self,
        store: StoreInterface,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or CircuitBreakerConfig()

        # In-memory sliding window of error timestamps per agent.
        # Each deque holds floats from ``time.time()``.
        self._error_windows: dict[str, deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, agent_id: str) -> CircuitBreakerInfo:
        """Return the current circuit breaker state for *agent_id*.

        Fast-path: if the ``budgetctl:suspended:{agent_id}`` key exists
        the agent is OPEN and we skip the HASH read entirely.

        If the HASH says OPEN but the suspended key has already expired
        (cooldown elapsed), we auto-transition to HALF_OPEN.

        If no state exists at all, the agent is CLOSED (default).
        """
        # 1. Fast-path -- O(1) EXISTS
        if await self._store.exists(_suspended_key(agent_id)):
            # Still within the cooldown window.  Build info from HASH.
            data = await self._store.hgetall(_cb_key(agent_id))
            return self._info_from_hash(data, override_state=CircuitBreakerState.OPEN)

        # 2. Read full state
        data = await self._store.hgetall(_cb_key(agent_id))
        if not data:
            # No CB state recorded -- agent is healthy.
            return CircuitBreakerInfo(state=CircuitBreakerState.CLOSED, reason="")

        stored_state = data.get("state", CircuitBreakerState.CLOSED.value)

        if stored_state == CircuitBreakerState.OPEN.value:
            # Suspended key expired but HASH still says OPEN.
            # Cooldown has elapsed -- transition to HALF_OPEN.
            await self._transition_half_open(agent_id, data)
            return CircuitBreakerInfo(
                state=CircuitBreakerState.HALF_OPEN,
                reason=data.get("reason", ""),
                tripped_at=self._parse_ts(data.get("tripped_at")),
                open_until=self._parse_ts(data.get("open_until")),
                error_count=int(data.get("error_count", "0")),
            )

        # HALF_OPEN or CLOSED -- return as-is.
        return self._info_from_hash(data)

    async def trip(
        self,
        agent_id: str,
        reason: TripReason,
        details: str = "",
    ) -> None:
        """Transition *agent_id* to OPEN.

        Sets:
        - The CB HASH with state metadata.
        - The ``suspended`` key with a TTL equal to ``cooldown_sec``.
        - Registers the agent in the ``budgetctl:cb:agents`` set.
        """
        now = time.time()
        open_until = now + self._config.cooldown_sec

        # Write HASH
        await self._store.hset(
            _cb_key(agent_id),
            {
                "state": CircuitBreakerState.OPEN.value,
                "reason": reason.value,
                "details": details,
                "tripped_at": str(now),
                "open_until": str(open_until),
                "error_count": str(len(self._error_windows.get(agent_id, []))),
            },
        )

        # Write suspended flag with TTL
        await self._store.set(
            _suspended_key(agent_id),
            "1",
            ttl=self._config.cooldown_sec,
        )

        # Track agent in the agents set (HASH-based)
        await self._store.hset(_AGENTS_KEY, {agent_id: "1"})

        logger.warning(
            "Circuit OPEN for agent=%s reason=%s details=%s cooldown=%ds",
            agent_id,
            reason.value,
            details,
            self._config.cooldown_sec,
        )

    async def record_success(self, agent_id: str) -> None:
        """Record a successful call.

        - HALF_OPEN: transition to CLOSED (agent has recovered).
        - CLOSED: no-op.
        """
        data = await self._store.hgetall(_cb_key(agent_id))
        if not data:
            return  # no CB state -- nothing to do

        current = data.get("state", CircuitBreakerState.CLOSED.value)

        if current == CircuitBreakerState.HALF_OPEN.value:
            await self._transition_closed(agent_id)
            logger.info(
                "Circuit CLOSED for agent=%s (recovered from HALF_OPEN)",
                agent_id,
            )

    async def record_failure(self, agent_id: str) -> None:
        """Record a failed call.

        Appends the current timestamp to the in-memory error window and
        checks whether the error threshold has been reached.

        - If HALF_OPEN: immediately re-trip with ERROR_THRESHOLD.
        - If CLOSED and errors >= threshold: trip with ERROR_THRESHOLD.
        """
        now = time.time()
        window = self._error_windows[agent_id]
        window.append(now)

        # Evict timestamps outside the sliding window.
        cutoff = now - self._config.error_window_sec
        while window and window[0] < cutoff:
            window.popleft()

        error_count = len(window)

        # Check current state
        data = await self._store.hgetall(_cb_key(agent_id))
        current = data.get("state", CircuitBreakerState.CLOSED.value) if data else CircuitBreakerState.CLOSED.value

        if current == CircuitBreakerState.HALF_OPEN.value:
            # Probe call failed -- back to OPEN.
            await self.trip(
                agent_id,
                TripReason.ERROR_THRESHOLD,
                details=f"Probe failed in HALF_OPEN (errors={error_count})",
            )
            return

        if error_count >= self._config.error_threshold:
            await self.trip(
                agent_id,
                TripReason.ERROR_THRESHOLD,
                details=(
                    f"{error_count} errors in {self._config.error_window_sec}s "
                    f"(threshold={self._config.error_threshold})"
                ),
            )

    async def reset(self, agent_id: str) -> None:
        """Manual reset to CLOSED.

        Clears: the CB HASH, the suspended key, and the in-memory error
        window.  The agent remains in the agents set for observability.
        """
        await self._store.delete(_cb_key(agent_id))
        await self._store.delete(_suspended_key(agent_id))
        self._error_windows.pop(agent_id, None)

        logger.info("Circuit RESET for agent=%s (manual)", agent_id)

    async def get_all_states(self) -> dict[str, CircuitBreakerInfo]:
        """Return the CB state for every known agent.

        Uses the ``budgetctl:cb:agents`` HASH to enumerate agent IDs,
        then calls ``check()`` on each one.
        """
        agents_map = await self._store.hgetall(_AGENTS_KEY)
        if not agents_map:
            return {}

        result: dict[str, CircuitBreakerInfo] = {}
        for agent_id in agents_map:
            result[agent_id] = await self.check(agent_id)

        return result

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    async def _transition_half_open(
        self,
        agent_id: str,
        data: dict[str, str],
    ) -> None:
        """Update the HASH to reflect HALF_OPEN state."""
        await self._store.hset(
            _cb_key(agent_id),
            {
                "state": CircuitBreakerState.HALF_OPEN.value,
                "reason": data.get("reason", ""),
                "details": data.get("details", ""),
                "tripped_at": data.get("tripped_at", ""),
                "open_until": data.get("open_until", ""),
                "error_count": data.get("error_count", "0"),
            },
        )
        logger.info(
            "Circuit HALF_OPEN for agent=%s (cooldown expired)",
            agent_id,
        )

    async def _transition_closed(self, agent_id: str) -> None:
        """Clear all CB state and move to CLOSED."""
        await self._store.hset(
            _cb_key(agent_id),
            {
                "state": CircuitBreakerState.CLOSED.value,
                "reason": "",
                "details": "",
                "tripped_at": "",
                "open_until": "",
                "error_count": "0",
            },
        )
        await self._store.delete(_suspended_key(agent_id))
        self._error_windows.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _info_from_hash(
        data: dict[str, str],
        override_state: CircuitBreakerState | None = None,
    ) -> CircuitBreakerInfo:
        """Build a ``CircuitBreakerInfo`` from a raw HASH dict."""
        state_str = data.get("state", CircuitBreakerState.CLOSED.value)
        state = override_state or CircuitBreakerState(state_str)

        return CircuitBreakerInfo(
            state=state,
            reason=data.get("reason", ""),
            tripped_at=CircuitBreaker._parse_ts(data.get("tripped_at")),
            open_until=CircuitBreaker._parse_ts(data.get("open_until")),
            error_count=int(data.get("error_count", "0")),
        )

    @staticmethod
    def _parse_ts(raw: str | None) -> datetime | None:
        """Convert a Unix timestamp string to a timezone-aware datetime."""
        if not raw:
            return None
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (ValueError, OSError):
            return None
