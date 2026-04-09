# Chappie

**The circuit breaker for AI agent spend.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 81 passing](https://img.shields.io/badge/tests-81%20passing-brightgreen.svg)](#testing)
[![Status: Day 6 of 7](https://img.shields.io/badge/status-Day%206%20of%207-blue.svg)](#build-progress)

LiteLLM gives you budget caps. Chappie gives you behavior detection.

Your agent is stuck in a loop, burning $0.03/call, 200 calls deep. LiteLLM's budget limit will eventually stop it at $50. Chappie stops it at call 4.

```
  Call 1: ALLOWED  'What is the weather in Tokyo?'
  Call 2: ALLOWED  'What is the weather in Tokyo?'
  Call 3: ALLOWED  'What is the weather in Tokyo?'
  Call 4: BLOCKED  'What is the weather in Tokyo?' -> hash_dedup
```

## The Problem

AI agents loop. A lot. And every loop iteration costs money.

Common patterns that drain budgets:

- **Repeated identical prompts.** Agent gets a bad response, retries the exact same prompt forever.
- **Cycling patterns.** Agent alternates between two actions (summarize, translate, summarize, translate...) without making progress.
- **Velocity spikes.** Agent suddenly fires 10x more calls per minute than its baseline, usually because it's stuck.

LiteLLM's built-in budget limits work like a spending cap on a credit card. They tell you *after* the damage is done. Chappie watches the agent's behavior in real time and pulls the plug before the bill adds up.

## Quick Start

```bash
pip install chappie
```

Add Chappie to your LiteLLM proxy config:

```yaml
# litellm_config.yaml
litellm_settings:
  callbacks:
    - chappie.logger
```

That's it. Chappie starts in **observe mode** by default. It logs detected loops but does not block requests. When you're confident, switch to enforce mode:

```bash
export CHAPPIE_MODE=enforce
```

## See It Work

No API key needed. No LLM calls. Just run the demo:

```bash
python examples/quick_test.py
```

```
=== Chappie Loop Detector Demo ===

Strategy A: Hash Dedup (same prompt repeated)

  Call 1: ALLOWED  'What is the weather in Tokyo?'
  Call 2: ALLOWED  'What is the weather in Tokyo?'
  Call 3: ALLOWED  'What is the weather in Tokyo?'
  Call 4: BLOCKED  'What is the weather in Tokyo?' -> hash_dedup

Strategy B: Cycle Detection (A-B-A-B pattern)

  Call 1: ALLOWED  'Summarize the document'
  Call 2: ALLOWED  'Translate to Spanish'
  Call 3: ALLOWED  'Summarize the document'
  Call 4: ALLOWED  'Translate to Spanish'
  Call 5: ALLOWED  'Summarize the document'
  Call 6: ALLOWED  'Translate to Spanish'
  Call 7: BLOCKED  'Summarize the document' -> cycle
```

## How It Works

Chappie plugs into LiteLLM as a `CustomLogger`. It inspects every request *before* it reaches the LLM and every response *after* it comes back.

Three detection strategies run on every call. The first match wins:

| Strategy | What It Catches | How |
|----------|----------------|-----|
| **Hash Dedup** | Same prompt repeated 3+ times | SHA-256 fingerprint of the last user message + model, tracked in a sliding window of 20 calls |
| **Cycle Detection** | A-B-A-B-A-B patterns | Looks for repeating subsequences (period 2-4) across 3 full repetitions |
| **Velocity Anomaly** | Sudden call-rate spikes | Compares current calls/min against an exponential moving average baseline. Flags at 5x baseline |

Detection state is kept **in-memory** (no Redis on the hot path). Each agent gets its own isolated window.

## Circuit Breaker

Loop detection tells you something is wrong. The circuit breaker acts on it.

When Chappie detects a loop, it trips the circuit breaker for that specific agent. The agent is blocked from making further LLM calls until a cooldown period expires or an operator manually resets it. Other agents are unaffected.

### The Three States

```
               loop detected
  CLOSED ─────────────────────► OPEN
    ▲                             │
    │                             │ cooldown expires
    │ probe call succeeds         ▼
    └──────────────────────── HALF_OPEN
                               │
           probe call fails    │
           ────────────────────► OPEN (reset cooldown)
```

| State | What Happens | Duration |
|-------|-------------|----------|
| **CLOSED** | Normal operation. All requests pass through. Chappie monitors for loops in the background. | Default state |
| **OPEN** | Agent is blocked. Every request returns HTTP 429 immediately, before it reaches the LLM. No tokens burned, no cost incurred. | `cooldown_sec` (default: 300s / 5 min) |
| **HALF_OPEN** | Cooldown expired. Chappie allows one probe call through to test if the agent has recovered. | 1 call |

After the probe call in HALF_OPEN:
- **Probe succeeds**: circuit closes, agent resumes normal operation.
- **Probe fails or triggers another loop**: circuit re-opens, cooldown resets.

### How It Connects to Loop Detection

The full flow from detection to enforcement:

```
  Agent sends request
       │
       ▼
  ChappieLogger.pre_call_hook()
       │
       ├─► Is circuit breaker OPEN for this agent?
       │     YES ──► Return 429 immediately (no LLM call)
       │     NO  ──► Continue
       │
       ├─► Run loop detection (hash dedup, cycle, velocity)
       │     LOOP DETECTED ──► Trip circuit breaker ──► Return 429
       │     NO LOOP       ──► Allow request through to LLM
       │
       ▼
  LLM processes request
       │
       ▼
  ChappieLogger.post_call_hook()
       │
       ├─► Record call in loop detector
       └─► Record error count for circuit breaker (failures only)
```

The circuit breaker also tracks raw error counts independently of loop detection. If an agent accumulates `error_threshold` (default: 5) LLM failures within `error_window_sec` (default: 60s), the breaker trips. This catches scenarios where the agent is not looping but is hammering a failing endpoint repeatedly.

### Blocked Agent Response

When an agent is blocked by an open circuit breaker, the caller receives:

```json
{
  "error": "chappie_circuit_open",
  "agent_id": "my-research-agent",
  "state": "open",
  "reason": "Loop detected via hash_dedup: Hash 3f2a... seen 3 times in last 20 calls",
  "open_until": "2025-01-15T14:35:00Z",
  "cooldown_remaining_sec": 287,
  "message": "Chappie circuit breaker is OPEN for this agent. Retry after cooldown or request a manual reset."
}
```

HTTP status: `429 Too Many Requests`

The `open_until` timestamp and `cooldown_remaining_sec` field tell the caller exactly when the agent will be eligible for a probe call.

### Trip Event Log

When the circuit breaker trips, Chappie emits a structured event:

```json
{
  "event_type": "circuit_breaker.tripped",
  "agent_id": "my-research-agent",
  "data": {
    "trigger": "loop_detected",
    "strategy": "hash_dedup",
    "previous_state": "closed",
    "new_state": "open",
    "error_count": 3,
    "cooldown_sec": 300,
    "open_until": "2025-01-15T14:35:00Z"
  },
  "timestamp": "2025-01-15T14:30:00Z"
}
```

### Manual Reset

Operators can reset a tripped circuit breaker without waiting for the cooldown:

```bash
curl -X POST http://localhost:8787/api/agents/my-research-agent/circuit-breaker/reset
```

This immediately transitions the agent from OPEN to CLOSED, allowing requests through again. Use this when you have fixed the underlying issue (bad prompt, misconfigured tool, stuck workflow) and want the agent back online.

## Budget Enforcer

Loop detection catches behavioral problems. The budget enforcer catches financial ones.

Chappie uses a **reservation-based** enforcement model. Before every LLM call, it estimates the cost, atomically reserves that amount against the budget, and only allows the call if the reservation succeeds. After the call, it reconciles the estimate with the actual cost.

### The Reservation Flow

```
  1. ESTIMATE    Calculate expected cost from message length + model pricing
       │
       ▼
  2. RESERVE     Atomically: if (spent + estimate <= limit) then hold
       │
       ├─ FAIL ──► 429 ChappieBudgetExceeded (no LLM call)
       │
       ▼
  3. EXECUTE     LLM call happens
       │
       ▼
  4. RECONCILE   Adjust hold to actual cost (release diff or charge extra)
       │
       └─ On failure: RELEASE full amount back to budget
```

The reservation is atomic. A Lua script checks `spent + estimated <= limit` and increments in a single Redis round-trip. No race conditions, even under concurrent load.

Reservations carry a TTL (default: 120 seconds). If a process crashes mid-call, the reservation expires and the budget recovers automatically. No orphaned holds.

### Budget Scopes

Budgets are enforced at four levels. Each scope is independent.

| Scope | What It Controls | Example |
|-------|-----------------|---------|
| `agent` | Single AI agent | `agent:email-drafter` has a $25/month cap |
| `user` | Individual human operator | `user:john` shares $200 across all agents he runs |
| `team` | Department or team | `team:engineering` shares $1,000 across all team members |
| `global` | Organization-wide ceiling | Hard stop at $5,000/month, period |

All four can be active at the same time. The most restrictive budget wins. An agent can be under-budget on its own cap but still get blocked if the team or global budget is exhausted.

### Threshold Alerts

Chappie fires alerts at configurable spend thresholds. Each threshold fires exactly once per budget period, so you do not get spammed.

| Threshold | Level | What It Means |
|-----------|-------|---------------|
| 50% | `info` | Heads up. Halfway through the budget. |
| 80% | `warning` | Slow down. Time to review agent behavior. |
| 90% | `urgent` | Almost out. Consider pausing non-critical agents. |
| 100% | `critical` | Budget exhausted. New requests are blocked with 429. |

### Budget Exceeded Response

When a reservation fails because the budget cannot absorb the estimated cost:

```json
{
  "error": "chappie_budget_exceeded",
  "agent_id": "agent:email-drafter",
  "spent": 24.87,
  "limit": 25.00,
  "message": "Budget exceeded for agent agent:email-drafter: spent $24.8700 / limit $25.0000"
}
```

HTTP status: `429 Too Many Requests`

### Budget Configuration

```bash
# Default budget when none is explicitly set for an agent/user/team
CHAPPIE_BUDGETS__DEFAULT_BUDGET=100.0

# Budget period reset cycle
CHAPPIE_BUDGETS__RESET_PERIOD=monthly       # "daily", "weekly", or "monthly"

# Reservation TTL in seconds (orphan protection)
CHAPPIE_BUDGETS__RESERVATION_TTL_SEC=120

# Alert thresholds (as decimal ratios)
CHAPPIE_BUDGETS__ALERT_THRESHOLDS=[0.5, 0.8, 0.9, 1.0]
```

## CLI (budgetctl)

Chappie ships with `budgetctl`, a command-line tool for managing budgets, monitoring agents, and inspecting circuit breakers.

Installed automatically with `pip install chappie`.

### System Status

```bash
budgetctl status
```

```
╭───── Chappie Status ─────╮
│                           │
│  Mode:           enforce  │
│  Store:          redis (connected) │
│  Agents tracked: 12      │
│  Total spend:    $847.32  │
│  Loops caught:   23       │
│  CB tripped:     4        │
│                           │
╰───────────────────────────╯

Active Circuit Breakers
  Agent              State      Reason                     Cooldown
  email-drafter      OPEN       Loop: hash_dedup            3m 42s
  code-reviewer      HALF_OPEN  Error threshold exceeded   probing...
```

### Budget List

```bash
budgetctl budget list
```

```
                       Budgets
  Scope    ID               Spent     Limit    Used   Status
  agent    email-drafter    $24.87    $25.00   99%    WARNING
  agent    code-reviewer    $82.10    $200.00  41%    OK
  user     john             $142.30   $200.00  71%    OK
  team     engineering      $847.32   $1000.00 85%    WARNING
  global   org              $847.32   $5000.00 17%    OK
```

### Set a Budget

```bash
budgetctl budget set agent email-drafter 50.00
# Budget set: agent/email-drafter = $50.00

budgetctl budget set team engineering 2000.00
# Budget set: team/engineering = $2000.00

budgetctl budget set global org 10000.00
# Budget set: global/org = $10000.00
```

### Get Budget Details

```bash
budgetctl budget get agent email-drafter
```

```
╭──── Budget: agent/email-drafter ────╮
│                                      │
│  Scope:     agent                    │
│  ID:        email-drafter            │
│  Spent:     $24.87                   │
│  Limit:     $50.00                   │
│  Remaining: $25.13                   │
│  Used:      50%                      │
│  Status:    OK                       │
│                                      │
╰──────────────────────────────────────╯
```

### JSON Output

Every command supports `--format json` for scripting and piping:

```bash
budgetctl --format json budget list | jq '.[] | select(.percentage > 80)'
```

### Connection

`budgetctl` talks to the Chappie API over HTTP. Set the API URL if it is not running on localhost:

```bash
export CHAPPIE_API_URL=http://your-server:8787
```

## Alerts

Chappie sends alerts when thresholds are crossed, circuit breakers trip, or loops are detected.

### Slack

Point Chappie at a Slack incoming webhook and it posts alerts to the channel of your choice:

```bash
CHAPPIE_ALERTS__SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00/B00/xxxx
```

### Generic Webhook

For PagerDuty, Datadog, or your own alerting system, use the generic webhook. Chappie POSTs a JSON payload on every alert:

```bash
CHAPPIE_ALERTS__WEBHOOK_URL=https://your-system.com/alerts/chappie
```

The payload:

```json
{
  "event_type": "budget.threshold_crossed",
  "agent_id": "email-drafter",
  "data": {
    "scope": "agent",
    "scope_id": "email-drafter",
    "threshold": 0.8,
    "level": "warning",
    "spent": 40.12,
    "limit": 50.00,
    "percentage": 80.24
  },
  "timestamp": "2025-01-15T14:30:00Z"
}
```

### Alert Levels

| Level | When | Example |
|-------|------|---------|
| `info` | 50% budget threshold crossed | Informational, no action needed yet |
| `warning` | 80% budget threshold or circuit breaker trip | Review agent behavior |
| `urgent` | 90% budget threshold | Consider pausing non-critical agents |
| `critical` | 100% budget exhausted | Agent is now blocked |

### Alert Configuration

```bash
CHAPPIE_ALERTS__SLACK_WEBHOOK_URL=        # Slack incoming webhook URL
CHAPPIE_ALERTS__WEBHOOK_URL=              # Generic webhook URL (receives JSON POST)
CHAPPIE_ALERTS__ENABLED=true              # Kill switch for all alerts
```

## API

Chappie exposes a REST API for programmatic access to agent state, budgets, and circuit breakers.

### Start the API Server

```bash
uvicorn chappie.api:app --host 0.0.0.0 --port 8787
```

Or use the default port from config:

```bash
# Reads CHAPPIE_API_PORT (default: 8787)
uvicorn chappie.api:app
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | System overview: mode, store, agent count, total spend, active breakers |
| `GET` | `/api/budgets` | List all budgets with spend, limit, and usage percentage |
| `GET` | `/api/budgets/{scope}/{id}` | Get budget details for a specific scope/id pair |
| `PUT` | `/api/budgets/{scope}/{id}` | Set or update a budget limit |
| `POST` | `/api/agents/{agent_id}/circuit-breaker/reset` | Manually reset a tripped circuit breaker |
| `GET` | `/api/events` | SSE stream of real-time events (loops, trips, alerts) |

### SSE Events Stream

Subscribe to the events endpoint for live monitoring. Events stream as Server-Sent Events:

```bash
curl -N http://localhost:8787/api/events
```

```
event: circuit_breaker.tripped
data: {"agent_id": "email-drafter", "data": {"trigger": "loop_detected", "strategy": "hash_dedup"}, "timestamp": "2025-01-15T14:30:00Z"}

event: budget.threshold_crossed
data: {"agent_id": "code-reviewer", "data": {"threshold": 0.8, "level": "warning", "spent": 160.42, "limit": 200.00}, "timestamp": "2025-01-15T14:31:12Z"}
```

## Docker

Get Chappie running with Redis in one command:

```bash
docker compose up
```

This starts:
- **Redis 7** on port 6379 (with persistence and health checks)
- **Chappie API** on port 8787 (connected to Redis, observe mode by default)

### What the Compose File Does

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  chappie-api:
    build: .
    ports:
      - "8787:8787"
    environment:
      - CHAPPIE_REDIS_URL=redis://redis:6379
      - CHAPPIE_MODE=observe
    depends_on:
      redis:
        condition: service_healthy
```

Switch to enforce mode:

```bash
CHAPPIE_MODE=enforce docker compose up
```

### Dockerfile

The image is based on `python:3.12-slim`. It copies the package, installs dependencies, and starts the API server with uvicorn.

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY chappie/ chappie/
COPY cli/ cli/
RUN pip install --no-cache-dir .
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "chappie.api:app", "--host", "0.0.0.0", "--port", "8787"]
```

## Two Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `observe` (default) | Logs loops, allows all requests | Safe rollout, monitoring, tuning thresholds |
| `enforce` | Returns HTTP 429 when a loop is detected or budget is exceeded | Production protection |

## Agent Identification

Chappie identifies agents using a fallback chain:

1. `metadata.agent_id` (explicit, preferred)
2. `metadata.session_id` (LangChain / AutoGen convention)
3. `team_id:user_id` (LiteLLM proxy identity)
4. API key suffix (last resort)

Pass your agent ID through LiteLLM metadata:

```python
import litellm

response = litellm.completion(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello"}],
    metadata={"agent_id": "my-research-agent"}
)
```

## Configuration

All settings load from environment variables with the `CHAPPIE_` prefix:

```bash
# Core
CHAPPIE_MODE=observe              # "observe" or "enforce"
CHAPPIE_REDIS_URL=redis://localhost:6379
CHAPPIE_ON_REDIS_FAILURE=open     # "open" (allow requests) or "closed" (block)
CHAPPIE_API_PORT=8787

# Loop Detection
CHAPPIE_LOOP_DETECTION__WINDOW_SIZE=20
CHAPPIE_LOOP_DETECTION__REPEAT_THRESHOLD=3
CHAPPIE_LOOP_DETECTION__CYCLE_MAX_PERIOD=4
CHAPPIE_LOOP_DETECTION__VELOCITY_WINDOW_SEC=60
CHAPPIE_LOOP_DETECTION__VELOCITY_MULTIPLIER=5.0

# Circuit Breaker
CHAPPIE_CIRCUIT_BREAKER__ERROR_THRESHOLD=5
CHAPPIE_CIRCUIT_BREAKER__ERROR_WINDOW_SEC=60
CHAPPIE_CIRCUIT_BREAKER__COOLDOWN_SEC=300
CHAPPIE_CIRCUIT_BREAKER__HALF_OPEN_MAX_CALLS=1

# Budgets
CHAPPIE_BUDGETS__DEFAULT_BUDGET=100.0
CHAPPIE_BUDGETS__RESET_PERIOD=monthly
CHAPPIE_BUDGETS__RESERVATION_TTL_SEC=120

# Alerts
CHAPPIE_ALERTS__SLACK_WEBHOOK_URL=
CHAPPIE_ALERTS__WEBHOOK_URL=
CHAPPIE_ALERTS__ENABLED=true
```

See [`.env.example`](.env.example) for the full list.

### Tuning Tips

- **Lowering `REPEAT_THRESHOLD`** to 2 catches loops faster but risks false positives on legitimate retries.
- **Raising `WINDOW_SIZE`** above 20 gives more context but uses more memory per agent.
- **`VELOCITY_MULTIPLIER=5.0`** means "5x the normal rate." Lower it for tighter control on expensive models.
- The velocity detector needs 5 calls to build a baseline. It will never flag during warmup.
- **`COOLDOWN_SEC=300`** keeps a tripped agent blocked for 5 minutes. Shorten it for development environments where agents restart frequently. Lengthen it for production agents that burn expensive tokens.
- **`ERROR_THRESHOLD=5`** controls how many LLM failures (timeouts, 500s, rate limits from the provider) trip the breaker independently of loop detection. Lower this for expensive models where even a few wasted retries matter.
- **`DEFAULT_BUDGET=100.0`** applies when no per-agent budget has been set. Agents running expensive models (GPT-4, Claude Opus) should get explicit budgets via `budgetctl budget set` rather than relying on the default.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   LiteLLM Proxy                      │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │      ChappieLogger (CustomLogger hook)         │   │
│  │                                                │   │
│  │  pre_call ──► CircuitBreaker.check()           │   │
│  │               │                                │   │
│  │               ├─ OPEN? ──► 429 (blocked)       │   │
│  │               │                                │   │
│  │               └─ CLOSED/HALF_OPEN?             │   │
│  │                    │                           │   │
│  │                    ▼                           │   │
│  │               BudgetEnforcer.reserve()          │   │
│  │               ├─ EXCEEDED? ──► 429 (blocked)    │   │
│  │               └─ OK?                           │   │
│  │                    │                           │   │
│  │                    ▼                           │   │
│  │                  LoopDetector.check()           │   │
│  │                  ├─ Hash Dedup                  │   │
│  │                  ├─ Cycle Detection             │   │
│  │                  └─ Velocity Anomaly            │   │
│  │                    │                           │   │
│  │                    ├─ LOOP ──► CB.trip() ──► 429│   │
│  │                    └─ OK   ──► allow request    │   │
│  │                                                │   │
│  │  post_call ──► BudgetEnforcer.reconcile()      │   │
│  │             ──► Record call in LoopDetector     │   │
│  │             ──► Record error in CircuitBreaker  │   │
│  │             ──► Check threshold alerts          │   │
│  └───────────────────────────────────────────────┘   │
│                                                      │
│  State: in-memory (loop detection)                   │
│  Store: Redis or MemoryStore (budgets, CB, alerts)   │
│  API: FastAPI on port 8787 (budgetctl, SSE events)   │
└──────────────────────────────────────────────────────┘
```

### Design Decisions

**Why in-memory instead of Redis for detection state?**
Loop detection runs on every single request. Adding a Redis round-trip to the hot path would add 1-5ms of latency per call. In-memory keeps it at microseconds.

**Why reservation-based budget enforcement?**
A simple "check then charge" has a race condition: two concurrent calls could both pass the check, then both charge, pushing the total over the limit. The Lua-script reservation is atomic, so concurrent calls cannot overspend.

**Why observe mode by default?**
Every team's "normal" agent behavior is different. Observe mode lets you see what Chappie would catch before it starts blocking. Deploy, watch the logs, tune the thresholds, then switch to enforce.

**Why fail-open when Redis is down?**
If your state store goes offline, blocking all AI requests is worse than temporarily losing protection. Chappie logs a warning and keeps going.

**Why only proxy mode can enforce?**
LiteLLM's proxy sits between the caller and the LLM, so it can intercept and block. SDK mode (direct `litellm.completion()` calls) does not have that interception point, so it observes only.

## Why Not Just Use LiteLLM Budgets?

LiteLLM has built-in budget limits. They are useful. But they solve a different problem.

| | LiteLLM Budgets | Chappie |
|---|---|---|
| **What it watches** | Cumulative spend ($) | Agent behavior (patterns, velocity) + spend |
| **When it acts** | After you hit the limit | When it detects a loop forming OR before the call if budget is tight |
| **Granularity** | Per-key or per-team | Per-agent, per-user, per-team, per-global |
| **A 200-call loop at $0.03/call** | Stops at your budget cap ($50, $100, whatever) | Stops at call 4 |
| **Cost enforcement** | Post-call check | Pre-call atomic reservation |

They are complementary. Use LiteLLM budgets as a hard ceiling. Use Chappie as the early warning system that prevents you from ever reaching that ceiling.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

81 tests covering:
- **Loop detection** (17 tests): Hash dedup, cycle detection (period 2/3/4), velocity anomaly, warmup, agent isolation, window boundaries, hash determinism
- **Circuit breaker** (22 tests): State transitions (CLOSED/OPEN/HALF_OPEN), error counting, cooldown timing, manual reset, trip reasons (loop/error/budget/manual), multi-agent isolation, full lifecycle
- **Budget enforcer** (18 tests): Reserve/reconcile/release flow, budget exceeded rejection, threshold alerts (50/80/90/100%), threshold deduplication, scope independence, spend reset, cost estimation
- **Store layer** (24 tests): Get/set/delete, TTL expiry, incr_float, hash operations, Lua script evaluation, ping

## Build Progress

Chappie is being built in public over 7 days.

| Day | Feature | Status |
|-----|---------|--------|
| **1** | Loop Detector (3 strategies) + LiteLLM integration + Store layer | **Done** |
| **2** | Circuit Breaker (CLOSED/OPEN/HALF_OPEN state machine) | **Done** |
| **3** | Budget Enforcer (reservation-based atomic enforcement) | **Done** |
| **4** | CLI (`budgetctl`) + Alerts (Slack/webhook) + REST API | **Done** |
| **5** | Docker + CI pipeline + Docs | **Done** |
| **6** | Polish + dogfooding + final tests | **Done** |
| 7 | Launch | Tomorrow |

## Project Structure

```
chappie/
├── chappie/
│   ├── __init__.py              # Package entry point, exports ChappieLogger
│   ├── config.py                # Pydantic settings (all env var config)
│   ├── exceptions.py            # ChappieError hierarchy (loop, budget, circuit)
│   ├── logger.py                # ChappieLogger (LiteLLM CustomLogger hook)
│   ├── models.py                # Shared Pydantic models (events, reservations, status)
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── loop_detector.py     # Hash dedup + cycle detection + velocity anomaly
│   │   ├── circuit_breaker.py   # Per-agent CLOSED/OPEN/HALF_OPEN state machine
│   │   └── budget_enforcer.py   # Atomic reservation + threshold alerts
│   └── store/
│       ├── __init__.py          # Store protocol + factory
│       ├── memory.py            # In-memory store (no dependencies)
│       └── redis.py             # Redis store (production)
├── cli/
│   ├── __init__.py
│   └── main.py                  # budgetctl CLI (Click + Rich)
├── tests/
│   ├── conftest.py              # Shared fixtures
│   ├── test_loop_detector.py    # 17 tests
│   ├── test_circuit_breaker.py  # 22 tests
│   ├── test_budget_enforcer.py  # 18 tests
│   └── test_store.py            # 24 tests
├── examples/
│   └── quick_test.py            # Run the demo (no API key needed)
├── .github/
│   └── workflows/
│       └── ci.yml               # Lint (ruff) + test (pytest) on every push
├── Dockerfile                   # Python 3.12-slim + uvicorn
├── docker-compose.yml           # Redis + Chappie API
├── pyproject.toml               # Package config, dependencies, budgetctl entry point
├── .env.example                 # All configuration env vars with defaults
└── LICENSE                      # MIT
```

## Coming Soon: Adaptive Insights + Model Benchmarking

### Adaptive Insights (v0.2)

Static thresholds are a starting point. An email-drafting agent that makes 5-8 calls per task is different from a code-reviewer that makes 15-25 calls. Chappie will learn each agent's normal behavior and auto-adjust thresholds.

```
budgetctl insights email-drafter

  Sessions:           142
  Avg calls/session:  6.3
  Avg cost/session:   $0.42
  P95 calls:          11 (auto-threshold)
  Velocity baseline:  3.2 calls/min
  Loops caught:       3 (2.1% anomaly rate)
  Recommendation:     Threshold auto-set to 11 (default was 3)
```

After 10 sessions, Chappie switches from static defaults to per-agent adaptive thresholds based on the agent's actual P95 behavior. Fewer false positives, tighter detection.

### Model Loop Leaderboard (v0.2)

Every loop detection is tagged with the model name. Chappie aggregates this into a per-model benchmark. Which LLMs loop the most?

```
budgetctl benchmark

  Model                  Calls    Loops    Rate     Avg Cost/Call
  claude-haiku-4-5       12,450   89       0.71%    $0.003
  gpt-4o-mini            8,200    142      1.73%    $0.008
  claude-sonnet-4        5,100    18       0.35%    $0.024
  gpt-4o                 3,800    31       0.82%    $0.041
  mistral-large          2,100    67       3.19%    $0.015

  Most loop-prone: mistral-large (3.19%)
  Most cost-efficient: claude-haiku-4-5 ($0.003/call, 0.71% loop rate)
```

Privacy-safe: only model names, call counts, loop counts, and cost aggregates. No prompt content stored.

## Contributing

This is early-stage software. Issues and PRs are welcome.

If you've had an agent burn through your budget because it got stuck in a loop, I'd like to hear about it. Open an issue describing the pattern, and it might become a new detection strategy.

## License

MIT. See [LICENSE](LICENSE).
