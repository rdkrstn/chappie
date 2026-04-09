"""Loop detection engine -- BudgetCtl's hero feature.

Three independent strategies run on every ``check()`` call.  The first
positive match wins and is returned immediately.

Strategy A -- Hash Dedup:
    SHA-256 the last user message + model name.  If the same hash
    appears >= ``repeat_threshold`` times in the sliding window,
    the agent is looping.

Strategy B -- Cycle Detection:
    Look for repeating subsequences (period 2..``cycle_max_period``)
    in the hash window.  e.g. [A,B,A,B,A,B] is a cycle of period 2.

Strategy C -- Token Velocity Anomaly:
    Track call timestamps per agent.  Compute calls/minute over the
    last ``velocity_window_sec`` seconds and compare against an
    exponential moving average baseline.  If velocity exceeds
    ``baseline * velocity_multiplier``, flag it.  The first 5 calls
    build the baseline (never flagged).

All state lives in-memory (Python dicts of deques).  No Redis required.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter, defaultdict, deque

from budgetctl.config import LoopDetectorConfig
from budgetctl.models import LoopCheckResult

logger = logging.getLogger("budgetctl.loop_detector")

# Minimum calls before the velocity baseline can trigger a flag.
_VELOCITY_WARMUP_CALLS = 5

# EMA smoothing factor -- controls how quickly baseline adapts.
# Deliberately low so bursts cannot rapidly re-anchor the baseline.
_EMA_ALPHA = 0.1

# Maximum factor by which the baseline can grow in a single update.
# Prevents a sudden burst from instantly lifting the baseline above
# the detection threshold.  Must be small enough that even many
# consecutive updates during a burst cannot compound past the
# velocity_multiplier threshold (default 5x).
_BASELINE_GROWTH_CAP = 1.05


class LoopDetector:
    """In-memory, per-agent loop detection across three strategies."""

    def __init__(self, config: LoopDetectorConfig | None = None) -> None:
        self._cfg = config or LoopDetectorConfig()

        # Strategy A + B: sliding window of content hashes per agent.
        # Key = agent_id, value = deque[str] (hex digests, maxlen=window_size)
        self._hash_windows: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self._cfg.window_size),
        )

        # Strategy C: call timestamps per agent.
        self._call_times: dict[str, deque[float]] = defaultdict(deque)

        # Strategy C: exponential moving average of calls/minute.
        self._velocity_baseline: dict[str, float] = {}

        # Total call counts for stats.
        self._call_counts: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        agent_id: str,
        messages: list[dict],
        model: str,
    ) -> LoopCheckResult:
        """Run all three strategies.  Return the first hit (or a clean result).

        **Important:** ``check()`` does NOT mutate state.  Call ``record()``
        separately after a successful LLM response to avoid recording
        calls that were blocked.
        """
        content_hash = self._hash_message(messages, model)

        # --- Strategy A: Hash Dedup ---
        result_a = self._check_hash_dedup(agent_id, content_hash)
        if result_a.is_loop:
            logger.warning(
                "Loop detected (hash_dedup) for agent=%s: %s",
                agent_id,
                result_a.details,
            )
            return result_a

        # --- Strategy B: Cycle Detection ---
        result_b = self._check_cycle(agent_id)
        if result_b.is_loop:
            logger.warning(
                "Loop detected (cycle) for agent=%s: %s",
                agent_id,
                result_b.details,
            )
            return result_b

        # --- Strategy C: Token Velocity Anomaly ---
        result_c = self._check_velocity(agent_id)
        if result_c.is_loop:
            logger.warning(
                "Loop detected (velocity) for agent=%s: %s",
                agent_id,
                result_c.details,
            )
            return result_c

        return LoopCheckResult(
            is_loop=False,
            strategy="none",
            details="All strategies passed",
            agent_id=agent_id,
        )

    def record(
        self,
        agent_id: str,
        messages: list[dict],
        model: str,
    ) -> None:
        """Record a successful call in the sliding windows.

        Called *after* the LLM responds so that blocked requests are
        never counted.
        """
        content_hash = self._hash_message(messages, model)

        # Strategy A + B: append hash.
        self._hash_windows[agent_id].append(content_hash)

        # Strategy C: append timestamp and update baseline.
        now = time.monotonic()
        self._call_times[agent_id].append(now)
        self._call_counts[agent_id] += 1
        self._update_velocity_baseline(agent_id)

    def clear_session(self, agent_id: str) -> None:
        """Reset all in-memory state for the given agent."""
        self._hash_windows.pop(agent_id, None)
        self._call_times.pop(agent_id, None)
        self._velocity_baseline.pop(agent_id, None)
        self._call_counts.pop(agent_id, None)

    def get_stats(self, agent_id: str) -> dict:
        """Return diagnostic info for an agent's current detection state."""
        window = list(self._hash_windows.get(agent_id, []))
        timestamps = list(self._call_times.get(agent_id, []))
        baseline = self._velocity_baseline.get(agent_id)

        # Compute current velocity (calls/min) over the configured window.
        now = time.monotonic()
        cutoff = now - self._cfg.velocity_window_sec
        recent = [t for t in timestamps if t >= cutoff]

        return {
            "agent_id": agent_id,
            "total_calls": self._call_counts.get(agent_id, 0),
            "window_size": len(window),
            "window_capacity": self._cfg.window_size,
            "unique_hashes": len(set(window)),
            "current_velocity_cpm": (
                len(recent) / (self._cfg.velocity_window_sec / 60)
                if recent
                else 0.0
            ),
            "velocity_baseline_cpm": baseline,
        }

    # ------------------------------------------------------------------
    # Strategy A: Hash Dedup
    # ------------------------------------------------------------------

    def _check_hash_dedup(
        self,
        agent_id: str,
        content_hash: str,
    ) -> LoopCheckResult:
        window = self._hash_windows.get(agent_id)
        if window is None or len(window) == 0:
            return self._clean(agent_id, "hash_dedup")

        counts = Counter(window)
        count = counts.get(content_hash, 0)

        if count >= self._cfg.repeat_threshold:
            return LoopCheckResult(
                is_loop=True,
                strategy="hash_dedup",
                details=(
                    f"Hash {content_hash[:8]}... seen {count} times "
                    f"in last {len(window)} calls "
                    f"(threshold={self._cfg.repeat_threshold})"
                ),
                agent_id=agent_id,
            )

        return self._clean(agent_id, "hash_dedup")

    # ------------------------------------------------------------------
    # Strategy B: Cycle Detection
    # ------------------------------------------------------------------

    def _check_cycle(self, agent_id: str) -> LoopCheckResult:
        window = self._hash_windows.get(agent_id)
        if window is None:
            return self._clean(agent_id, "cycle")

        hashes = list(window)

        for period in range(2, self._cfg.cycle_max_period + 1):
            needed = period * 3  # need 3 full repetitions to confirm
            if len(hashes) < needed:
                continue

            tail = hashes[-needed:]
            chunks = [
                tail[i * period : (i + 1) * period]
                for i in range(3)
            ]

            if chunks[0] == chunks[1] == chunks[2]:
                return LoopCheckResult(
                    is_loop=True,
                    strategy="cycle",
                    details=(
                        f"Repeating cycle of period {period} detected: "
                        f"{[h[:8] for h in chunks[0]]}"
                    ),
                    agent_id=agent_id,
                )

        return self._clean(agent_id, "cycle")

    # ------------------------------------------------------------------
    # Strategy C: Token Velocity Anomaly
    # ------------------------------------------------------------------

    def _check_velocity(self, agent_id: str) -> LoopCheckResult:
        timestamps = self._call_times.get(agent_id)
        total_calls = self._call_counts.get(agent_id, 0)

        # Not enough data to judge -- still in warmup.
        if timestamps is None or total_calls < _VELOCITY_WARMUP_CALLS:
            return self._clean(agent_id, "velocity")

        baseline = self._velocity_baseline.get(agent_id)
        if baseline is None or baseline == 0.0:
            return self._clean(agent_id, "velocity")

        now = time.monotonic()
        cutoff = now - self._cfg.velocity_window_sec
        recent_count = sum(1 for t in timestamps if t >= cutoff)

        # Convert to calls per minute for consistent units.
        window_minutes = self._cfg.velocity_window_sec / 60
        current_velocity = recent_count / window_minutes

        threshold = baseline * self._cfg.velocity_multiplier

        if current_velocity > threshold:
            return LoopCheckResult(
                is_loop=True,
                strategy="velocity",
                details=(
                    f"Velocity {current_velocity:.1f} calls/min "
                    f"exceeds baseline {baseline:.1f} * "
                    f"{self._cfg.velocity_multiplier}x = "
                    f"{threshold:.1f} calls/min"
                ),
                agent_id=agent_id,
            )

        return self._clean(agent_id, "velocity")

    def _update_velocity_baseline(self, agent_id: str) -> None:
        """Maintain an exponential moving average of calls per minute.

        The baseline uses a low alpha (_EMA_ALPHA = 0.1) and a growth cap
        so that a sudden burst cannot instantly re-anchor the baseline
        above the detection threshold.  This ensures that velocity
        anomaly detection works even when the burst arrives in a tight
        cluster of calls.
        """
        timestamps = self._call_times[agent_id]
        if len(timestamps) < 2:
            return

        now = time.monotonic()
        cutoff = now - self._cfg.velocity_window_sec
        recent_count = sum(1 for t in timestamps if t >= cutoff)
        window_minutes = self._cfg.velocity_window_sec / 60
        current_velocity = recent_count / window_minutes

        prev = self._velocity_baseline.get(agent_id)
        if prev is None:
            self._velocity_baseline[agent_id] = current_velocity
        else:
            candidate = _EMA_ALPHA * current_velocity + (1 - _EMA_ALPHA) * prev
            # Cap upward growth so a burst can't lift the baseline past
            # the detection threshold in a single window.
            max_allowed = prev * _BASELINE_GROWTH_CAP
            self._velocity_baseline[agent_id] = min(candidate, max_allowed)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_message(messages: list[dict], model: str) -> str:
        """SHA-256 of the last user message content + model name.

        Returns the first 16 hex chars.  If no user message is found,
        hashes the model name alone (still produces a stable fingerprint).
        """
        content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                raw = msg.get("content", "")
                # content may be a list of blocks (vision API) or a string
                if isinstance(raw, list):
                    content = "".join(
                        block.get("text", "")
                        for block in raw
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                else:
                    content = str(raw)
                break

        payload = f"{content}|{model}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _clean(agent_id: str, strategy: str) -> LoopCheckResult:
        return LoopCheckResult(
            is_loop=False,
            strategy=strategy,
            details="OK",
            agent_id=agent_id,
        )
