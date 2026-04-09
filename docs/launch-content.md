# Chappie Launch Content

---

## 1. Show HN Post

**Title:**
Show HN: Chappie - LiteLLM plugin that detects agent loops before they drain your budget

**Body:**

A few weeks ago I watched a research agent burn $40 in about 20 minutes. It was stuck alternating between two tool calls, A-B-A-B, making progress on nothing. LiteLLM's budget cap eventually killed it. But "eventually" was after 300+ calls at $0.12 each.

LiteLLM budgets are a hard ceiling. Chappie is the behavior detection layer that keeps you from ever reaching that ceiling.

It runs as a LiteLLM CustomLogger callback. Before every LLM call, it:
1. Checks if the circuit breaker is already open for this agent
2. Reserves the estimated cost against the agent's budget (atomic, no race conditions)
3. Runs three loop detection strategies on the call history

If it detects a loop, it trips the circuit breaker for that specific agent and returns HTTP 429. Other agents keep running. The blocked agent waits out a cooldown, then gets one probe call to confirm it has recovered before being let back in.

Demo output from the included quick_test.py (no API key needed):

```
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

The three detection strategies:
- Hash dedup: SHA-256 fingerprint of the last user message + model. Flags when the same hash appears 3+ times in a sliding window of 20 calls.
- Cycle detection: Looks for repeating subsequences with period 2-4 across 3 full repetitions.
- Velocity anomaly: Compares current calls/min against an exponential moving average. Flags at 5x baseline.

Detection state is in-memory (no Redis on the hot path, microsecond latency). Budgets and circuit breaker state live in Redis with a fallback to in-memory if Redis is unavailable.

Stack: Python 3.10+, FastAPI for the API, Click + Rich for the `budgetctl` CLI, Redis for persistence.

81 tests. MIT license. Ships with a `budgetctl` CLI and a Docker Compose setup that spins up Redis + the Chappie API in one command.

Two modes: observe (default, logs everything, blocks nothing, good for tuning) and enforce (actually blocks).

GitHub: https://github.com/rdkrstn/chappie

Would especially like feedback on: the velocity anomaly thresholds, the reservation TTL approach for budget enforcement, and whether the agent identification fallback chain covers your setup.

---

## 2. r/MachineLearning Post

**Title:**
Chappie: open-source LiteLLM CustomLogger that detects agent behavioral loops using hash dedup, cycle detection, and velocity anomaly

**Body:**

**The problem**

AI agents loop. Common failure modes:

- Identical prompt repeated verbatim (agent gets a bad response, retries the same prompt)
- Alternating pattern (A-B-A-B-A-B): agent cycles between two actions without converging
- Velocity spike: agent suddenly fires 10x its baseline calls/min, usually stuck somewhere

LiteLLM's budget enforcement is post-hoc. It tells you you spent $50. Chappie detects the behavioral signal and stops the loop at call 4.

**What it is**

Chappie is a LiteLLM `CustomLogger` plugin. It hooks into `pre_call_hook` and `post_call_hook` on the LiteLLM proxy.

Three detection strategies run on every pre-call, first match wins:

**Hash Dedup**
SHA-256 fingerprint of `(last_user_message_content, model)`. Tracks occurrences in a sliding window of the last 20 calls per agent. Trips when the same hash appears `repeat_threshold` (default: 3) times. O(1) lookup.

**Cycle Detection**
Looks for repeating subsequences of period 2 through 4 in the call history deque. Requires 3 full repetitions of the pattern before flagging. This is what catches the A-B-A-B problem without false-positiving on legitimate short sequences.

**Velocity Anomaly**
Maintains a per-agent exponential moving average of calls/min. On each call, computes the current rate over the last `velocity_window_sec` (default: 60s). Flags when `current_rate > ema * velocity_multiplier` (default: 5.0). Has a warmup period of 5 calls to avoid flagging agents that are legitimately bursty at startup.

All detection state is in-memory per agent. No Redis on the hot path. Each agent gets its own isolated deque and EMA. The design tradeoff is that state does not survive process restarts, which is acceptable because loops tend to manifest quickly and the cooldown window is short.

**Circuit Breaker**

When a loop is detected, Chappie trips a per-agent circuit breaker:

```
CLOSED -> OPEN (loop detected)
OPEN -> HALF_OPEN (cooldown expires, default 300s)
HALF_OPEN -> CLOSED (probe call succeeds)
HALF_OPEN -> OPEN (probe call fails or triggers another loop)
```

Blocked agents receive HTTP 429 with a structured JSON body including `open_until` and `cooldown_remaining_sec`.

**Budget Enforcer**

Separate from loop detection. Uses a reservation pattern:

1. Pre-call: estimate cost, atomically reserve against budget via Lua script
2. Execute: LLM call
3. Post-call: reconcile estimate vs actual (release delta or charge overage)
4. On failure: release full reservation

The Lua script makes the reserve step atomic: `if (spent + estimate <= limit) then INCR`. No race conditions under concurrent load. Reservations have a TTL (default 120s) so orphaned holds from process crashes expire automatically.

Budgets operate at four independent scopes: agent, user, team, global. Most restrictive wins.

**How to use it**

```bash
pip install chappie
```

```yaml
# litellm_config.yaml
litellm_settings:
  callbacks:
    - chappie.logger
```

```bash
export CHAPPIE_MODE=enforce
```

Pass agent identity via LiteLLM metadata:

```python
response = litellm.completion(
    model="gpt-4",
    messages=[{"role": "user", "content": "..."}],
    metadata={"agent_id": "my-research-agent"}
)
```

**Test it without an API key**

```bash
python examples/quick_test.py
```

**Stats**
- 81 tests (loop detection: 17, circuit breaker: 22, budget enforcer: 18, store layer: 24)
- Python 3.10+, MIT license
- Starts in observe mode by default (logs, does not block)
- GitHub: https://github.com/rdkrstn/chappie

Interested in feedback on the cycle detection period bounds, the velocity EMA approach, and whether the in-memory detection state tradeoff makes sense for multi-instance deployments (currently each instance maintains its own state independently).

---

## 3. r/LocalLLaMA Post

**Title:**
Your local agent can still loop and cost you money on API orchestration - built a LiteLLM plugin that kills the loop at call 4

**Body:**

Running local models does not make you immune to the agent loop problem. Your LLM inference might be free, but:

- If you are using LiteLLM as a proxy in front of Ollama or LM Studio, you are still paying for the orchestration overhead, tool calls, and any external API calls the agent triggers
- If the agent is mixed (local LLM + external search/retrieval APIs), a looping agent will hammer those paid endpoints
- Even at $0 inference cost, a stuck agent holds compute, blocks other work, and fills your context window with garbage

I built Chappie to solve this. It plugs into LiteLLM as a CustomLogger and watches your agent's behavior, not just its spend.

**What it actually does**

```
Phase 1: Normal Operation
  [1] ALLOWED  "What is machine learning?"       Cost: $1.50 | Budget: $1.50/$20.00
  [2] ALLOWED  "Explain neural networks"          Cost: $1.50 | Budget: $3.00/$20.00
  [3] ALLOWED  "How does backpropagation work?"   Cost: $1.50 | Budget: $4.50/$20.00

Phase 2: Loop Detection
  [4] ALLOWED  "What is machine learning?"        Cost: $1.50 | Budget: $6.00/$20.00
  [5] ALLOWED  "What is machine learning?"        Cost: $1.50 | Budget: $7.50/$20.00
  [6] TRIPPED  Loop detected (hash_dedup) -> Circuit breaker OPEN
  [7] BLOCKED  Circuit breaker is OPEN            Budget NOT touched
```

Notice call 7: the budget does not move. Once the circuit breaker trips, blocked calls cost nothing. The agent sits in a box until cooldown expires, then gets one probe call to see if it has recovered.

Three ways it detects loops:

1. **Hash dedup** -- same prompt repeated 3+ times in the last 20 calls. Catches the "agent retries exact same question forever" case.
2. **Cycle detection** -- A-B-A-B-A-B patterns. Catches the agent that alternates between two tool calls without making progress.
3. **Velocity anomaly** -- sudden 5x spike in calls/min vs the agent's normal baseline. Catches confused agents firing off requests at full speed.

**Works with any model LiteLLM supports**

Including Ollama, LM Studio, llama.cpp server, anything behind an OpenAI-compatible endpoint. The loop detection does not care what model you are using. It watches call patterns, not model responses.

**Setup in two steps**

```bash
pip install chappie
```

```yaml
# litellm_config.yaml
litellm_settings:
  callbacks:
    - chappie.logger
```

Start in observe mode (default) to see what it would catch without blocking anything. Switch to enforce when you are ready:

```bash
export CHAPPIE_MODE=enforce
```

**See it work right now**

No API key needed, no LLM calls:

```bash
git clone https://github.com/rdkrstn/chappie
cd chappie
pip install -e .
python examples/quick_test.py
```

**The budgetctl CLI**

Ships with a CLI for monitoring and managing agents:

```bash
budgetctl status
```

```
  Mode:           enforce
  Agents tracked: 12
  Total spend:    $847.32
  Loops caught:   23
  CB tripped:     4

Active Circuit Breakers
  Agent              State      Reason                     Cooldown
  email-drafter      OPEN       Loop: hash_dedup            3m 42s
```

MIT license. 81 tests. Python 3.10+.

If you have been burned by a looping agent (local or otherwise), the Contributing section of the README is worth reading. I want to add detection strategies based on real failure patterns people have actually hit.

GitHub: https://github.com/rdkrstn/chappie

---

## 4. Twitter/X Thread

**Tweet 1 (hook)**
Your AI agent loops. You do not notice until you check your bill.

LiteLLM's budget cap stops the damage at $50.

I built Chappie to stop it at call 4.

It's a LiteLLM plugin that watches behavior, not spend. Open source, MIT, ships today.

github.com/rdkrstn/chappie

**Tweet 2 (demo)**
Here's what it looks like when Chappie catches a loop:

```
[4] ALLOWED  "What is machine learning?"
[5] ALLOWED  "What is machine learning?"
[6] TRIPPED  Loop detected (hash_dedup) -> Circuit breaker OPEN
[7] BLOCKED  Circuit breaker is OPEN   Budget NOT touched
```

Call 7 does not touch the budget. Once the breaker trips, blocked calls cost nothing.

No API key needed to see this yourself:
python examples/quick_test.py

**Tweet 3 (detection)**
Three strategies, first match wins:

Hash dedup: same prompt 3+ times in the last 20 calls -> flag it. SHA-256, O(1) lookup.

Cycle detection: A-B-A-B-A-B repeating pattern -> flag it. Catches the agent that alternates between two tools forever.

Velocity anomaly: 5x spike over baseline calls/min -> flag it. Exponential moving average per agent.

**Tweet 4 (circuit breaker)**
When a loop is detected, the circuit breaker trips for that specific agent only.

CLOSED -> OPEN: loop detected, agent blocked (HTTP 429)
OPEN -> HALF_OPEN: cooldown expires (default 5 min)
HALF_OPEN -> CLOSED: one probe call succeeds, agent resumes
HALF_OPEN -> OPEN: probe fails, clock resets

Other agents keep running. State is per-agent.

**Tweet 5 (budget enforcer)**
The budget enforcer is separate from loop detection and uses a reservation pattern.

Before every LLM call:
1. Estimate cost
2. Atomically reserve that amount (Lua script, single Redis round-trip)
3. If reservation fails: HTTP 429, no LLM call
4. After call: reconcile estimate vs actual

No race conditions. If the process crashes mid-call, the reservation TTL (120s default) releases it automatically.

**Tweet 6 (CLI)**
Ships with budgetctl, a CLI for monitoring everything:

```
budgetctl status

  Agents tracked: 12
  Total spend:    $847.32
  Loops caught:   23
  CB tripped:     4

Active Circuit Breakers
  email-drafter   OPEN    Loop: hash_dedup   3m 42s
  code-reviewer   HALF_OPEN  Error threshold  probing...
```

Also: budgetctl budget list, budget set, budget get. All commands support --format json for scripting.

**Tweet 7 (open source)**
Open source. MIT license.

Python 3.10+
81 tests (loop detection, circuit breaker, budget enforcer, store layer)
Works with any LiteLLM-supported model: GPT-4, Claude, Ollama, LM Studio, anything behind an OpenAI-compatible endpoint
Starts in observe mode (logs, does not block) so you can tune thresholds before enforcing

github.com/rdkrstn/chappie

**Tweet 8 (call to action)**
If you have been burned by a looping agent, I want to hear about it.

What pattern caused it? How much did it cost? Did you catch it, or did your budget cap catch it?

Open an issue describing the failure pattern. If it is not covered by the three existing strategies, it might become a new one.

Star if you build with agents. Share if you have been there.

github.com/rdkrstn/chappie
