"""Chappie REST API -- serves data from the shared store to the CLI and dashboard.

Reads from the same Redis (or MemoryStore) that the ChappieLogger plugin
writes to.  The two processes share a key schema:

    chappie:cb:{agent_id}          HASH   (state, reason, details, tripped_at,
                                            open_until, error_count)
    chappie:suspended:{agent_id}   STRING  "1" with TTL = cooldown_sec
    chappie:cb:agents              HASH   (agent_id -> "1")
    chappie:budget:{scope}:{id}    HASH   (spent, limit)
    chappie:events                 LIST   (JSON-encoded ChappieEvent objects)

Start with:
    uvicorn chappie.api:app --port 8787
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from chappie.config import ChappieConfig
from chappie.models import CircuitBreakerInfo, CircuitBreakerState
from chappie.store import StoreInterface

logger = logging.getLogger("chappie.api")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

store: StoreInterface | None = None
config: ChappieConfig | None = None


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


async def _init_store(cfg: ChappieConfig) -> StoreInterface:
    """Create and connect the backing store."""
    if cfg.redis_url:
        try:
            from chappie.store.redis import RedisStore

            redis_store = RedisStore(cfg.redis_url)
            await redis_store.connect()
            logger.info("API store: Redis (%s)", cfg.redis_url)
            return redis_store
        except Exception as exc:
            logger.warning(
                "Redis connection failed, falling back to MemoryStore: %s",
                exc,
            )

    from chappie.store.memory import MemoryStore

    mem = MemoryStore()
    logger.info("API store: MemoryStore (in-memory)")
    return mem


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage store lifecycle via the modern lifespan protocol."""
    global store, config
    config = ChappieConfig.from_env()
    store = await _init_store(config)
    yield
    if store is not None and hasattr(store, "close"):
        await store.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Chappie API",
    version="0.1.0",
    description="REST API for Chappie -- the circuit breaker for AI agent spend.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Key prefixes -- must match chappie.engine.circuit_breaker
_CB_PREFIX = "chappie:cb:"
_SUSPENDED_PREFIX = "chappie:suspended:"
_AGENTS_KEY = "chappie:cb:agents"
_BUDGET_PREFIX = "chappie:budget:"
_EVENTS_KEY = "chappie:events"


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(raw: str | None) -> str | None:
    """Convert a Unix timestamp string to ISO 8601."""
    if not raw:
        return None
    try:
        dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, OSError):
        return None


def _require_store() -> StoreInterface:
    """Return the store or raise 503 if unavailable."""
    if store is None:
        raise HTTPException(status_code=503, detail="Store not initialized")
    return store


async def _get_all_agents(s: StoreInterface) -> dict[str, str]:
    """Return the agent registry hash."""
    return await s.hgetall(_AGENTS_KEY)


async def _get_cb_info(s: StoreInterface, agent_id: str) -> dict:
    """Build a circuit breaker info dict for a single agent."""
    data = await s.hgetall(f"{_CB_PREFIX}{agent_id}")
    is_suspended = await s.exists(f"{_SUSPENDED_PREFIX}{agent_id}")

    if not data:
        return {
            "agent_id": agent_id,
            "state": CircuitBreakerState.CLOSED.value,
            "reason": "",
            "tripped_at": None,
            "open_until": None,
            "error_count": 0,
        }

    # Determine effective state.
    stored_state = data.get("state", CircuitBreakerState.CLOSED.value)
    if is_suspended:
        effective_state = CircuitBreakerState.OPEN.value
    elif stored_state == CircuitBreakerState.OPEN.value and not is_suspended:
        # Cooldown expired -- effectively half_open.
        effective_state = CircuitBreakerState.HALF_OPEN.value
    else:
        effective_state = stored_state

    return {
        "agent_id": agent_id,
        "state": effective_state,
        "reason": data.get("reason", ""),
        "tripped_at": _parse_ts(data.get("tripped_at")),
        "open_until": _parse_ts(data.get("open_until")),
        "error_count": int(data.get("error_count", "0")),
    }


async def _get_budget_info(s: StoreInterface, scope: str, scope_id: str) -> dict:
    """Build a budget info dict for a single scope."""
    key = f"{_BUDGET_PREFIX}{scope}:{scope_id}"
    data = await s.hgetall(key)

    if not data:
        return {
            "scope": scope,
            "scope_id": scope_id,
            "spent": 0.0,
            "limit": 0.0,
            "remaining": 0.0,
            "percentage": 0.0,
        }

    spent = float(data.get("spent", "0"))
    limit = float(data.get("limit", "0"))
    remaining = max(0.0, limit - spent)
    percentage = (spent / limit * 100.0) if limit > 0 else 0.0

    return {
        "scope": scope,
        "scope_id": scope_id,
        "spent": round(spent, 4),
        "limit": round(limit, 4),
        "remaining": round(remaining, 4),
        "percentage": round(percentage, 2),
    }


async def _discover_budget_keys(s: StoreInterface) -> list[tuple[str, str]]:
    """Discover all budget scopes.

    Since the StoreInterface does not expose SCAN/KEYS, we check the
    agents registry and look for budget keys by convention:

        chappie:budget:agent:{agent_id}
        chappie:budget:team:{team_id}
        chappie:budget:global:default

    Also checks for a global budget key.
    """
    pairs: list[tuple[str, str]] = []

    # Always check global budget.
    global_key = f"{_BUDGET_PREFIX}global:default"
    if await s.exists(global_key):
        pairs.append(("global", "default"))

    # Check per-agent budgets.
    agents = await _get_all_agents(s)
    for agent_id in agents:
        agent_key = f"{_BUDGET_PREFIX}agent:{agent_id}"
        if await s.exists(agent_key):
            pairs.append(("agent", agent_id))

        # Some setups use team-level budgets derived from agent_id.
        if ":" in agent_id:
            team = agent_id.split(":")[0]
            team_key = f"{_BUDGET_PREFIX}team:{team}"
            if await s.exists(team_key) and ("team", team) not in pairs:
                pairs.append(("team", team))

    return pairs


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SetBudgetRequest(BaseModel):
    """Body for PUT /api/budgets/{scope}/{scope_id}."""

    limit: float


# ---------------------------------------------------------------------------
# Routes: System status
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def get_status() -> dict:
    """System overview: mode, store type, agent count, CB states, total spend."""
    s = _require_store()

    agents = await _get_all_agents(s)
    agent_count = len(agents)

    # Aggregate CB states.
    cb_counts = {"closed": 0, "open": 0, "half_open": 0}
    total_loops = 0
    for agent_id in agents:
        info = await _get_cb_info(s, agent_id)
        state = info["state"]
        cb_counts[state] = cb_counts.get(state, 0) + 1
        if info.get("reason") == "loop_detected":
            total_loops += 1

    # Aggregate spend from known budgets.
    budget_keys = await _discover_budget_keys(s)
    total_spent = 0.0
    for scope, scope_id in budget_keys:
        binfo = await _get_budget_info(s, scope, scope_id)
        total_spent += binfo["spent"]

    return {
        "status": "ok",
        "mode": config.mode if config else "unknown",
        "store_type": type(s).__name__,
        "agent_count": agent_count,
        "total_spent": round(total_spent, 4),
        "loops_caught": total_loops,
        "circuit_breakers": cb_counts,
    }


# ---------------------------------------------------------------------------
# Routes: Agents
# ---------------------------------------------------------------------------


@app.get("/api/agents")
async def list_agents() -> dict:
    """List all tracked agents with their CB state."""
    s = _require_store()
    agents = await _get_all_agents(s)

    results = []
    for agent_id in agents:
        info = await _get_cb_info(s, agent_id)

        # Check if there is a per-agent budget.
        budget = await _get_budget_info(s, "agent", agent_id)

        results.append({
            **info,
            "total_cost": budget["spent"],
            "budget_limit": budget["limit"],
        })

    return {"agents": results, "count": len(results)}


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """Single agent detail: CB state, budget, loop history."""
    s = _require_store()

    cb_info = await _get_cb_info(s, agent_id)
    budget = await _get_budget_info(s, "agent", agent_id)

    return {
        **cb_info,
        "budget": budget,
    }


# ---------------------------------------------------------------------------
# Routes: Budgets
# ---------------------------------------------------------------------------


@app.get("/api/budgets")
async def list_budgets() -> dict:
    """List all budget scopes with spent/limit/remaining."""
    s = _require_store()
    keys = await _discover_budget_keys(s)

    results = []
    for scope, scope_id in keys:
        info = await _get_budget_info(s, scope, scope_id)
        results.append(info)

    return {"budgets": results, "count": len(results)}


@app.get("/api/budgets/{scope}/{scope_id}")
async def get_budget(scope: str, scope_id: str) -> dict:
    """Single budget detail."""
    s = _require_store()
    return await _get_budget_info(s, scope, scope_id)


@app.put("/api/budgets/{scope}/{scope_id}")
async def set_budget(scope: str, scope_id: str, body: SetBudgetRequest) -> dict:
    """Set or update a budget limit.

    Creates the budget key if it does not exist.  Preserves the current
    ``spent`` value so in-flight tracking is not lost.
    """
    s = _require_store()

    key = f"{_BUDGET_PREFIX}{scope}:{scope_id}"
    existing = await s.hgetall(key)
    current_spent = existing.get("spent", "0")

    await s.hset(key, {
        "spent": current_spent,
        "limit": str(body.limit),
    })

    logger.info(
        "Budget set: %s:%s limit=$%.4f",
        scope,
        scope_id,
        body.limit,
    )

    return await _get_budget_info(s, scope, scope_id)


# ---------------------------------------------------------------------------
# Routes: Circuit breakers
# ---------------------------------------------------------------------------


@app.get("/api/circuit-breakers")
async def list_circuit_breakers() -> dict:
    """List all circuit breaker states."""
    s = _require_store()
    agents = await _get_all_agents(s)

    results = []
    for agent_id in agents:
        info = await _get_cb_info(s, agent_id)
        results.append(info)

    return {"circuit_breakers": results, "count": len(results)}


@app.post("/api/circuit-breakers/{agent_id}/reset")
async def reset_circuit_breaker(agent_id: str) -> dict:
    """Manual circuit breaker reset to CLOSED.

    Clears the CB hash and the suspended key.  The agent stays in the
    agents registry for observability.
    """
    s = _require_store()

    # Clear CB state.
    await s.delete(f"{_CB_PREFIX}{agent_id}")
    await s.delete(f"{_SUSPENDED_PREFIX}{agent_id}")

    logger.info("Circuit breaker reset via API: agent=%s", agent_id)

    return {
        "agent_id": agent_id,
        "state": CircuitBreakerState.CLOSED.value,
        "message": f"Circuit breaker for '{agent_id}' has been reset to CLOSED",
    }


# ---------------------------------------------------------------------------
# Routes: SSE event stream
# ---------------------------------------------------------------------------


async def _event_generator() -> AsyncGenerator[str, None]:
    """Poll the store for new events and yield SSE-formatted lines.

    Uses a simple cursor (last seen timestamp) to avoid re-sending
    events.  Polls every 1 second.
    """
    last_check = time.time()

    # Send initial keepalive so clients know the stream is alive.
    yield "event: connected\ndata: {\"status\": \"connected\"}\n\n"

    while True:
        try:
            if store is not None:
                # Read recent events from the events list.
                # Events are stored as JSON strings in a Redis LIST.
                raw = await store.get(_EVENTS_KEY)
                if raw:
                    try:
                        events = json.loads(raw)
                        if isinstance(events, list):
                            for event in events:
                                ts = event.get("timestamp", "")
                                # Only yield events newer than last check.
                                try:
                                    event_time = datetime.fromisoformat(ts).timestamp()
                                except (ValueError, TypeError):
                                    event_time = 0

                                if event_time > last_check:
                                    event_json = json.dumps(event)
                                    event_type = event.get("event_type", "update")
                                    yield f"event: {event_type}\ndata: {event_json}\n\n"
                    except json.JSONDecodeError:
                        pass

                last_check = time.time()

                # Also emit a status heartbeat every poll cycle.
                agents = await _get_all_agents(store)
                heartbeat = {
                    "type": "heartbeat",
                    "agent_count": len(agents),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat)}\n\n"

        except Exception as exc:
            logger.warning("SSE poll error: %s", exc)
            error_data = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {error_data}\n\n"

        await asyncio.sleep(1.0)


@app.get("/api/events")
async def event_stream() -> StreamingResponse:
    """SSE stream for live events.

    Returns a ``text/event-stream`` response that the CLI or dashboard
    can consume for real-time updates.
    """
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    """Simple health check endpoint."""
    store_healthy = False
    if store is not None:
        try:
            store_healthy = await store.ping()
        except Exception:
            store_healthy = False

    return {
        "status": "healthy" if store_healthy else "degraded",
        "store": type(store).__name__ if store else "none",
        "store_connected": store_healthy,
    }


# ---------------------------------------------------------------------------
# Entrypoint for direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CHAPPIE_API_PORT", "8787"))
    uvicorn.run(
        "chappie.api:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
