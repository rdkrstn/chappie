# Chappie

**The circuit breaker for AI agent spend.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 40 passing](https://img.shields.io/badge/tests-40%20passing-brightgreen.svg)](#testing)
[![Status: Day 1 of 7](https://img.shields.io/badge/status-Day%201%20of%207-orange.svg)](#build-progress)

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
```

See [`.env.example`](.env.example) for the full list.

### Tuning Tips

- **Lowering `REPEAT_THRESHOLD`** to 2 catches loops faster but risks false positives on legitimate retries.
- **Raising `WINDOW_SIZE`** above 20 gives more context but uses more memory per agent.
- **`VELOCITY_MULTIPLIER=5.0`** means "5x the normal rate." Lower it for tighter control on expensive models.
- The velocity detector needs 5 calls to build a baseline. It will never flag during warmup.

## Architecture

```
┌─────────────────────────────────────────┐
│            LiteLLM Proxy                │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │      ChappieLogger               │   │
│  │      (CustomLogger hook)          │   │
│  │                                   │   │
│  │  pre_call   ──► LoopDetector      │   │
│  │               ├─ Hash Dedup       │   │
│  │               ├─ Cycle Detection  │   │
│  │               └─ Velocity Anomaly │   │
│  │                                   │   │
│  │  post_call  ──► Record call       │   │
│  │               └─ Log cost         │   │
│  └──────────────────────────────────┘   │
│                                         │
│  State: in-memory (per agent)           │
│  Store: Redis or MemoryStore fallback   │
└─────────────────────────────────────────┘
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

40 tests covering:
- Hash dedup detection (exact repeats, threshold boundaries, different models)
- Cycle detection (period 2, period 3, period 4, insufficient data)
- Velocity anomaly detection (spike detection, warmup behavior, baseline drift protection)
- Agent isolation (loops in one agent don't affect another)
- Store layer (Redis commands, MemoryStore equivalence)

## Build Progress

Chappie is being built in public over 7 days.

| Day | Feature | Status |
|-----|---------|--------|
| **1** | Loop Detector (3 strategies) + LiteLLM integration + Store layer | **Done** |
| 2 | Circuit Breaker (CLOSED/OPEN/HALF_OPEN state machine) | Planned |
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
│   │   └── loop_detector.py # The three detection strategies
│   └── store/
│       ├── __init__.py      # Store protocol + factory
│       ├── memory.py        # In-memory store (no dependencies)
│       └── redis.py         # Redis store
├── tests/
│   ├── test_loop_detector.py
│   └── test_store.py
├── examples/
│   └── quick_test.py        # Run the demo (no API key needed)
├── pyproject.toml
└── .env.example
```

## Contributing

This is early-stage software. Issues and PRs are welcome.

If you've had an agent burn through your budget because it got stuck in a loop, I'd like to hear about it. Open an issue describing the pattern, and it might become a new detection strategy.

## License

MIT. See [LICENSE](LICENSE).
