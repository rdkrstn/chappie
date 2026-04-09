# Chappie

**The circuit breaker for AI agent spend.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 55 passing](https://img.shields.io/badge/tests-55%20passing-brightgreen.svg)](#testing)
[![Status: Day 2 of 7](https://img.shields.io/badge/status-Day%202%20of%207-orange.svg)](#build-progress)

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
# Via the REST API (Day 4)
curl -X POST http://localhost:8787/api/agents/my-research-agent/circuit-breaker/reset
```

This immediately transitions the agent from OPEN to CLOSED, allowing requests through again. Use this when you have fixed the underlying issue (bad prompt, misconfigured tool, stuck workflow) and want the agent back online.

### Two Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `observe` (default) | Logs loops, allows all requests | Safe rollout, monitoring, tuning thresholds |
| `enforce` | Returns HTTP 429 when a loop is detected | Production protection |

### Agent Identification

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

### Enforce Mode Response

When Chappie blocks a request in enforce mode, the caller gets a structured 429 response:

```json
{
  "error": "chappie_loop_detected",
  "agent_id": "my-research-agent",
  "strategy": "hash_dedup",
  "details": "Hash 3f2a... seen 3 times in last 20 calls (threshold=3)",
  "message": "Chappie blocked this request: loop detected via hash_dedup"
}
```

## Configuration

All settings load from environment variables with the `CHAPPIE_` prefix:

```bash
# Core
CHAPPIE_MODE=observe              # "observe" or "enforce"
CHAPPIE_REDIS_URL=redis://localhost:6379
CHAPPIE_ON_REDIS_FAILURE=open     # "open" (allow requests) or "closed" (block)

# Loop Detection
CHAPPIE_LOOP_DETECTION__WINDOW_SIZE=20
CHAPPIE_LOOP_DETECTION__REPEAT_THRESHOLD=3
CHAPPIE_LOOP_DETECTION__CYCLE_MAX_PERIOD=4
CHAPPIE_LOOP_DETECTION__VELOCITY_WINDOW_SEC=60
CHAPPIE_LOOP_DETECTION__VELOCITY_MULTIPLIER=5.0

# Circuit Breaker
CHAPPIE_CIRCUIT_BREAKER__ERROR_THRESHOLD=5      # Errors before the breaker trips
CHAPPIE_CIRCUIT_BREAKER__ERROR_WINDOW_SEC=60     # Time window for counting errors
CHAPPIE_CIRCUIT_BREAKER__COOLDOWN_SEC=300        # Seconds the breaker stays open (5 min)
CHAPPIE_CIRCUIT_BREAKER__HALF_OPEN_MAX_CALLS=1   # Probe calls allowed in half-open state
```

See [`.env.example`](.env.example) for the full list.

### Tuning Tips

- **Lowering `REPEAT_THRESHOLD`** to 2 catches loops faster but risks false positives on legitimate retries.
- **Raising `WINDOW_SIZE`** above 20 gives more context but uses more memory per agent.
- **`VELOCITY_MULTIPLIER=5.0`** means "5x the normal rate." Lower it for tighter control on expensive models.
- The velocity detector needs 5 calls to build a baseline. It will never flag during warmup.
- **`COOLDOWN_SEC=300`** keeps a tripped agent blocked for 5 minutes. Shorten it for development environments where agents restart frequently. Lengthen it for production agents that burn expensive tokens.
- **`ERROR_THRESHOLD=5`** controls how many LLM failures (timeouts, 500s, rate limits from the provider) trip the breaker independently of loop detection. Lower this for expensive models where even a few wasted retries matter.

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
│  │                  LoopDetector.check()           │   │
│  │                  ├─ Hash Dedup                  │   │
│  │                  ├─ Cycle Detection             │   │
│  │                  └─ Velocity Anomaly            │   │
│  │                    │                           │   │
│  │                    ├─ LOOP ──► CB.trip() ──► 429│   │
│  │                    └─ OK   ──► allow request    │   │
│  │                                                │   │
│  │  post_call ──► Record call in LoopDetector     │   │
│  │             ──► Record error in CircuitBreaker  │   │
│  │             ──► Log cost                        │   │
│  └───────────────────────────────────────────────┘   │
│                                                      │
│  State: in-memory (per agent)                        │
│  Store: Redis or MemoryStore fallback                │
└──────────────────────────────────────────────────────┘
```

### Design Decisions

**Why in-memory instead of Redis for detection state?**
Loop detection runs on every single request. Adding a Redis round-trip to the hot path would add 1-5ms of latency per call. In-memory keeps it at microseconds.

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
| **What it watches** | Cumulative spend ($) | Agent behavior (patterns, velocity) |
| **When it acts** | After you hit the limit | When it detects a loop forming |
| **Granularity** | Per-key or per-team | Per-agent session |
| **A 200-call loop at $0.03/call** | Stops at your budget cap ($50, $100, whatever) | Stops at call 4 |

They are complementary. Use LiteLLM budgets as a hard ceiling. Use Chappie as the early warning system that prevents you from ever reaching that ceiling.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

55 tests covering:
- Hash dedup detection (exact repeats, threshold boundaries, different models)
- Cycle detection (period 2, period 3, period 4, insufficient data)
- Velocity anomaly detection (spike detection, warmup behavior, baseline drift protection)
- Agent isolation (loops in one agent don't affect another)
- Store layer (Redis commands, MemoryStore equivalence)
- Circuit breaker state transitions (CLOSED to OPEN, OPEN to HALF_OPEN, HALF_OPEN to CLOSED)
- Circuit breaker error counting (threshold, window expiry, reset on close)
- Circuit breaker cooldown timing (expiry, re-trip on probe failure)
- Circuit breaker integration with loop detection (loop triggers trip)

## Build Progress

Chappie is being built in public over 7 days.

| Day | Feature | Status |
|-----|---------|--------|
| **1** | Loop Detector (3 strategies) + LiteLLM integration + Store layer | **Done** |
| **2** | Circuit Breaker (CLOSED/OPEN/HALF_OPEN state machine) | **Done** |
| 3 | Budget Enforcer (reservation-based atomic enforcement) | Planned |
| 4 | CLI (`budgetctl`) + Alerts (Slack/webhook) + REST API | Planned |
| 5 | Docs + Docker + CI pipeline | Planned |
| 6 | Polish + dogfooding | Planned |
| 7 | Launch | Planned |

## Project Structure

```
chappie/
├── chappie/
│   ├── __init__.py          # Package entry point
│   ├── config.py            # Pydantic settings (env var config)
│   ├── exceptions.py        # ChappieError hierarchy
│   ├── logger.py            # ChappieLogger (LiteLLM CustomLogger)
│   ├── models.py            # Shared Pydantic models
│   ├── engine/
│   │   ├── loop_detector.py    # The three detection strategies
│   │   └── circuit_breaker.py  # Per-agent CLOSED/OPEN/HALF_OPEN state machine
│   └── store/
│       ├── __init__.py      # Store protocol + factory
│       ├── memory.py        # In-memory store (no dependencies)
│       └── redis.py         # Redis store
├── tests/
│   ├── test_loop_detector.py
│   ├── test_circuit_breaker.py
│   └── test_store.py
├── examples/
│   └── quick_test.py        # Run the demo (no API key needed)
├── pyproject.toml
└── .env.example
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
