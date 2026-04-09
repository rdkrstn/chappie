"""Redis-backed store for production use.

Thin wrapper around ``redis.asyncio`` that implements
:class:`StoreInterface`.  No connection-pooling magic -- the
``from_url`` factory in ``redis.asyncio`` handles that for us.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from chappie.store import StoreInterface


class RedisStore(StoreInterface):
    """Production store backed by Redis (or a protocol-compatible proxy
    like DragonflyDB / KeyDB)."""

    def __init__(self, url: str) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            url,
            decode_responses=True,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Verify the connection is alive by issuing a PING."""
        await self._redis.ping()

    async def close(self) -> None:
        """Release the underlying connection pool."""
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Scalar operations
    # ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl is not None:
            await self._redis.set(key, value, ex=ttl)
        else:
            await self._redis.set(key, value)

    async def incr_float(self, key: str, amount: float) -> float:
        return await self._redis.incrbyfloat(key, amount)

    # ------------------------------------------------------------------
    # Key lifecycle
    # ------------------------------------------------------------------

    async def exists(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def expire(self, key: str, ttl: int) -> None:
        await self._redis.expire(key, ttl)

    # ------------------------------------------------------------------
    # Hash operations
    # ------------------------------------------------------------------

    async def hgetall(self, key: str) -> dict[str, str]:
        return await self._redis.hgetall(key)

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        await self._redis.hset(key, mapping=mapping)

    # ------------------------------------------------------------------
    # Lua-script execution
    # ------------------------------------------------------------------

    async def eval_lua(
        self,
        script: str,
        keys: list[str],
        args: list[str],
    ) -> Any:
        return await self._redis.eval(script, len(keys), *keys, *args)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            return await self._redis.ping()
        except (aioredis.ConnectionError, aioredis.TimeoutError, OSError):
            return False
