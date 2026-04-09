"""Store interface and factory for Chappie's persistence layer.

Chappie uses a key-value store abstraction so the engine can run against
Redis in production or a plain Python dict in tests / local development.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StoreInterface(ABC):
    """Async key-value store contract.

    Every concrete store (Redis, in-memory, etc.) must implement these
    methods.  The interface is intentionally slim -- it covers the five
    access patterns Chappie actually needs:

      1. Scalar get / set / incr  (budget counters, circuit-breaker state)
      2. Hash get / set           (per-agent metadata)
      3. Lua-style eval           (atomic budget reservation)
      4. Key lifecycle            (TTL, delete, exists)
      5. Health check             (ping)
    """

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Return the string value for *key*, or ``None`` if missing."""
        ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set *key* to *value* with an optional TTL in seconds."""
        ...

    @abstractmethod
    async def incr_float(self, key: str, amount: float) -> float:
        """Increment the float stored at *key* by *amount* and return the
        new value.  Creates the key with value ``amount`` if it does not
        exist."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists and has not expired."""
        ...

    @abstractmethod
    async def hgetall(self, key: str) -> dict[str, str]:
        """Return all field-value pairs for the hash stored at *key*."""
        ...

    @abstractmethod
    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        """Set multiple hash fields at *key* in one call."""
        ...

    @abstractmethod
    async def eval_lua(
        self,
        script: str,
        keys: list[str],
        args: list[str],
    ) -> Any:
        """Execute a Lua-like script against the store.

        In Redis this maps to ``EVAL``.  The in-memory store provides a
        Python-based placeholder that simulates the budget-reservation
        script.
        """
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """Return ``True`` if the store is healthy and reachable."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove *key* from the store."""
        ...

    @abstractmethod
    async def expire(self, key: str, ttl: int) -> None:
        """Set a TTL (in seconds) on an existing *key*."""
        ...
