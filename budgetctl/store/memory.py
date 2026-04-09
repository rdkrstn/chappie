"""In-memory store for development and testing.

Implements :class:`StoreInterface` using plain Python dicts.  Not safe
for multi-process use, but perfectly fine for unit tests, the CLI in
local mode, and single-process dev servers.
"""

from __future__ import annotations

import time
from typing import Any

from budgetctl.store import StoreInterface


class MemoryStore(StoreInterface):
    """Dict-backed async store that passes the same contract as Redis."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._expiry: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_expiry(self, key: str) -> None:
        """Delete *key* from all backing dicts if its TTL has passed."""
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(key, None)
            self._hashes.pop(key, None)
            del self._expiry[key]

    def _set_ttl(self, key: str, ttl: int | None) -> None:
        """Record an absolute expiry timestamp for *key*."""
        if ttl is not None and ttl > 0:
            self._expiry[key] = time.monotonic() + ttl
        # If ttl is None we leave any existing expiry alone (matches
        # Redis SET behaviour when no EX/PX is provided on a key that
        # already has one).

    # ------------------------------------------------------------------
    # Scalar operations
    # ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        self._check_expiry(key)
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._data[key] = value
        if ttl is not None:
            self._set_ttl(key, ttl)

    async def incr_float(self, key: str, amount: float) -> float:
        self._check_expiry(key)
        current = float(self._data.get(key, "0"))
        new_value = current + amount
        self._data[key] = str(new_value)
        return new_value

    # ------------------------------------------------------------------
    # Key lifecycle
    # ------------------------------------------------------------------

    async def exists(self, key: str) -> bool:
        self._check_expiry(key)
        return key in self._data or key in self._hashes

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._hashes.pop(key, None)
        self._expiry.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        # Only set expiry if the key actually exists.
        if key in self._data or key in self._hashes:
            self._set_ttl(key, ttl)

    # ------------------------------------------------------------------
    # Hash operations
    # ------------------------------------------------------------------

    async def hgetall(self, key: str) -> dict[str, str]:
        self._check_expiry(key)
        return dict(self._hashes.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self._check_expiry(key)
        bucket = self._hashes.setdefault(key, {})
        bucket.update(mapping)

    # ------------------------------------------------------------------
    # Lua-script simulation
    # ------------------------------------------------------------------

    async def eval_lua(
        self,
        script: str,
        keys: list[str],
        args: list[str],
    ) -> Any:
        """Simulate Lua scripts used by Chappie.

        Supports two script patterns:

        1. **Budget reservation** (contains ``INCRBYFLOAT``):
           KEYS[1] = spent key, KEYS[2] = reservation key
           ARGV[1] = estimated_cost, ARGV[2] = budget_limit,
           ARGV[3] = reservation_id, ARGV[4] = ttl
           Atomically checks ``spent + cost <= limit``, increments
           spent, sets reservation key with TTL.
           Returns ``[1, new_spent, limit]`` on success or
           ``[0, current_spent, limit]`` on failure.

        2. **Legacy hash-based check** (contains ``HGET``):
           Kept for backwards compatibility with earlier scripts.

        3. **Fallback**: returns ``[1]``.
        """
        # Budget reservation script -- scalar keys
        if "INCRBYFLOAT" in script and len(keys) >= 2 and len(args) >= 2:
            spent_key = keys[0]
            reservation_key = keys[1]
            estimated_cost = float(args[0])
            budget_limit = float(args[1])
            ttl = int(args[3]) if len(args) >= 4 else 120

            self._check_expiry(spent_key)
            current_spent = float(self._data.get(spent_key, "0"))

            if current_spent + estimated_cost > budget_limit:
                return [0, str(current_spent), str(budget_limit)]

            new_spent = current_spent + estimated_cost
            self._data[spent_key] = str(new_spent)
            self._data[reservation_key] = str(estimated_cost)
            self._set_ttl(reservation_key, ttl)

            return [1, str(new_spent), str(budget_limit)]

        # Legacy hash-based budget check (Day 1-2 placeholder)
        if "HGET" in script and len(keys) >= 1 and len(args) >= 1:
            budget_key = keys[0]
            requested_cost = float(args[0])

            bucket = self._hashes.get(budget_key, {})
            current_spent = float(bucket.get("spent", "0"))
            limit = float(bucket.get("limit", "100"))

            available = limit - current_spent
            if available >= requested_cost:
                new_spent = current_spent + requested_cost
                self._hashes.setdefault(budget_key, {})["spent"] = str(new_spent)
                return [1, new_spent, limit]
            else:
                return [0, current_spent, limit]

        return [1]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        return True
