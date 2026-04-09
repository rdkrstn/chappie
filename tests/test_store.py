"""Tests for the in-memory store implementation.

These tests verify that ``MemoryStore`` correctly implements the
``StoreInterface`` contract.  Every assertion here should also hold
for ``RedisStore`` against a real (or fakeredis) Redis instance.
"""

from __future__ import annotations

import time

import pytest

from budgetctl.store.memory import MemoryStore


# ---------------------------------------------------------------------------
# Scalar get / set
# ---------------------------------------------------------------------------


async def test_get_missing_key_returns_none(memory_store: MemoryStore):
    assert await memory_store.get("no-such-key") is None


async def test_set_then_get(memory_store: MemoryStore):
    await memory_store.set("k", "hello")
    assert await memory_store.get("k") == "hello"


async def test_set_overwrites(memory_store: MemoryStore):
    await memory_store.set("k", "v1")
    await memory_store.set("k", "v2")
    assert await memory_store.get("k") == "v2"


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


async def test_set_with_ttl_expires(memory_store: MemoryStore, monkeypatch):
    """Key with a TTL disappears after the deadline passes."""
    fake_time = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

    await memory_store.set("ephemeral", "data", ttl=10)
    assert await memory_store.get("ephemeral") == "data"

    # Advance past the TTL
    fake_time[0] = 111.0
    assert await memory_store.get("ephemeral") is None


async def test_expire_sets_ttl_on_existing_key(memory_store: MemoryStore, monkeypatch):
    fake_time = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

    await memory_store.set("k", "v")
    await memory_store.expire("k", 5)

    # Still alive at t=104
    fake_time[0] = 104.0
    assert await memory_store.get("k") == "v"

    # Expired at t=106
    fake_time[0] = 106.0
    assert await memory_store.get("k") is None


async def test_expire_on_missing_key_is_noop(memory_store: MemoryStore):
    """Calling expire on a non-existent key should not create the key."""
    await memory_store.expire("ghost", 60)
    assert await memory_store.exists("ghost") is False


# ---------------------------------------------------------------------------
# exists / delete
# ---------------------------------------------------------------------------


async def test_exists_false_for_missing(memory_store: MemoryStore):
    assert await memory_store.exists("nope") is False


async def test_exists_true_after_set(memory_store: MemoryStore):
    await memory_store.set("k", "v")
    assert await memory_store.exists("k") is True


async def test_delete_removes_key(memory_store: MemoryStore):
    await memory_store.set("k", "v")
    await memory_store.delete("k")
    assert await memory_store.exists("k") is False
    assert await memory_store.get("k") is None


async def test_delete_missing_key_is_noop(memory_store: MemoryStore):
    """Deleting a key that does not exist should not raise."""
    await memory_store.delete("nope")  # must not raise


# ---------------------------------------------------------------------------
# incr_float
# ---------------------------------------------------------------------------


async def test_incr_float_creates_key(memory_store: MemoryStore):
    result = await memory_store.incr_float("counter", 1.5)
    assert result == pytest.approx(1.5)
    assert await memory_store.get("counter") == "1.5"


async def test_incr_float_accumulates(memory_store: MemoryStore):
    await memory_store.incr_float("c", 1.0)
    await memory_store.incr_float("c", 2.5)
    result = await memory_store.incr_float("c", 0.25)
    assert result == pytest.approx(3.75)


async def test_incr_float_negative(memory_store: MemoryStore):
    await memory_store.set("c", "10.0")
    result = await memory_store.incr_float("c", -3.0)
    assert result == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Hash operations
# ---------------------------------------------------------------------------


async def test_hset_and_hgetall(memory_store: MemoryStore):
    await memory_store.hset("agent:1", {"spent": "5.50", "calls": "12"})
    data = await memory_store.hgetall("agent:1")
    assert data == {"spent": "5.50", "calls": "12"}


async def test_hgetall_missing_returns_empty(memory_store: MemoryStore):
    assert await memory_store.hgetall("no-hash") == {}


async def test_hset_merges_fields(memory_store: MemoryStore):
    await memory_store.hset("h", {"a": "1"})
    await memory_store.hset("h", {"b": "2"})
    data = await memory_store.hgetall("h")
    assert data == {"a": "1", "b": "2"}


async def test_hset_overwrites_field(memory_store: MemoryStore):
    await memory_store.hset("h", {"a": "1"})
    await memory_store.hset("h", {"a": "99"})
    data = await memory_store.hgetall("h")
    assert data == {"a": "99"}


async def test_hgetall_returns_copy(memory_store: MemoryStore):
    """Mutations to the returned dict must not affect the store."""
    await memory_store.hset("h", {"k": "v"})
    data = await memory_store.hgetall("h")
    data["k"] = "mutated"
    assert (await memory_store.hgetall("h"))["k"] == "v"


# ---------------------------------------------------------------------------
# exists covers hashes too
# ---------------------------------------------------------------------------


async def test_exists_true_for_hash(memory_store: MemoryStore):
    await memory_store.hset("h", {"f": "v"})
    assert await memory_store.exists("h") is True


async def test_delete_removes_hash(memory_store: MemoryStore):
    await memory_store.hset("h", {"f": "v"})
    await memory_store.delete("h")
    assert await memory_store.exists("h") is False
    assert await memory_store.hgetall("h") == {}


# ---------------------------------------------------------------------------
# eval_lua simulation
# ---------------------------------------------------------------------------


async def test_eval_lua_budget_success(memory_store: MemoryStore):
    """Simulated budget reservation succeeds when under limit."""
    await memory_store.hset("budget:agent-1", {"spent": "10.0", "limit": "100.0"})
    result = await memory_store.eval_lua(
        "HGET budget check script",
        keys=["budget:agent-1"],
        args=["5.0"],
    )
    assert result[0] == 1  # success
    assert result[1] == pytest.approx(15.0)  # new spent
    assert result[2] == pytest.approx(100.0)  # limit


async def test_eval_lua_budget_rejected(memory_store: MemoryStore):
    """Simulated budget reservation fails when over limit."""
    await memory_store.hset("budget:agent-1", {"spent": "98.0", "limit": "100.0"})
    result = await memory_store.eval_lua(
        "HGET budget check script",
        keys=["budget:agent-1"],
        args=["5.0"],
    )
    assert result[0] == 0  # rejected
    assert result[1] == pytest.approx(98.0)  # unchanged
    assert result[2] == pytest.approx(100.0)  # limit


async def test_eval_lua_generic_returns_success(memory_store: MemoryStore):
    """Scripts without HGET return a generic success response."""
    result = await memory_store.eval_lua("return 1", keys=[], args=[])
    assert result == [1]


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


async def test_ping(memory_store: MemoryStore):
    assert await memory_store.ping() is True
