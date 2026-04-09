# GitHub Issues for Launch

Open these after v0.1.0 is published. Each one maps to a `good first issue` label.

---

## Issue 1: Add email alert channel

**Title**: Add email alert channel (SMTP)

**Labels**: `enhancement`, `good first issue`, `alerts`

**Body**:

Chappie currently supports two alert channels: Slack (incoming webhooks) and generic webhooks. Email is the most common request in tools like this, and many teams prefer email for non-urgent threshold alerts.

### Expected Behavior

Users configure SMTP credentials in environment variables and Chappie sends an email when a threshold is crossed or a circuit breaker trips.

```bash
CHAPPIE_ALERTS__SMTP_HOST=smtp.gmail.com
CHAPPIE_ALERTS__SMTP_PORT=587
CHAPPIE_ALERTS__SMTP_USERNAME=alerts@yourorg.com
CHAPPIE_ALERTS__SMTP_PASSWORD=your-app-password
CHAPPIE_ALERTS__SMTP_FROM=alerts@yourorg.com
CHAPPIE_ALERTS__SMTP_TO=on-call@yourorg.com
```

The email body should include: alert level, agent ID, spend data or circuit breaker state, and a timestamp.

### Implementation Hints

- Look at `chappie/alerts.py`. The `SlackChannel` class is the reference implementation. Follow the same interface.
- Add an `SMTPChannel` class that implements `send(alert: AlertEvent)`.
- Use `smtplib` (stdlib) or `aiosmtplib` for async sending.
- Add the SMTP fields to `AlertsConfig` in `chappie/config.py` using `pydantic-settings`.
- Wire the new channel into the channel factory (also in `alerts.py`).
- Add at least 2 tests to `tests/test_alerts.py` (or create the file if it does not exist): one for a successful send, one for a connection failure that does not crash the process.

---

## Issue 2: Build Next.js dashboard

**Title**: Build Next.js real-time dashboard

**Labels**: `enhancement`, `good first issue`, `frontend`

**Body**:

Chappie exposes a complete REST API and an SSE events stream. There is no visual interface. A simple Next.js dashboard would make it much easier to monitor agent spend and loop activity at a glance.

### Expected Behavior

A web dashboard that shows:

- System status (mode, store connection, agents tracked)
- Budget table: each scope, spend vs. limit, percentage, status badge
- Active circuit breakers and their cooldown remaining
- A live event feed powered by the SSE stream at `/api/events`

### Implementation Hints

The API is already running at `http://localhost:8787`. Key endpoints:

| Endpoint | What it returns |
|----------|----------------|
| `GET /api/status` | Mode, store, agent count, total spend, active breakers |
| `GET /api/budgets` | All budgets with spend, limit, percentage |
| `GET /api/events` | SSE stream of circuit breaker trips, threshold alerts |

Use `EventSource` (or the `eventsource` npm package) for the live feed. Polling `/api/status` and `/api/budgets` every 5 seconds is fine for the initial version.

Suggested stack: Next.js App Router, Tailwind CSS, shadcn/ui for the table and badge components. Keep it a standalone app in a top-level `dashboard/` directory.

A good first version does not need auth. It is a local monitoring tool.

---

## Issue 3: Add anomaly detection (Welford's algorithm)

**Title**: Replace EMA velocity detector with Welford's online algorithm

**Labels**: `enhancement`, `good first issue`, `loop-detection`

**Body**:

The current velocity anomaly detector uses an exponential moving average (EMA) baseline and flags calls when the current rate exceeds `VELOCITY_MULTIPLIER * ema_baseline`. This works but has two limitations:

1. It does not track variance, so it cannot distinguish between an agent that normally runs at a steady 5 calls/min vs. one that naturally spikes between 2 and 12 calls/min.
2. The fixed multiplier (default 5x) is a guess. It produces false positives for high-variance agents.

### Expected Behavior

Replace the EMA with [Welford's online algorithm](https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_online_algorithm), which maintains a running mean and variance in O(1) time and O(1) space. Flag a call as anomalous when the current rate exceeds `mean + N * stddev` (z-score threshold).

```
# Example config
CHAPPIE_LOOP_DETECTION__VELOCITY_ZSCORE_THRESHOLD=3.0   # flag at 3 standard deviations
```

A threshold of 3.0 corresponds to a ~0.3% false positive rate under a normal distribution. This is a well-understood, tunable value that teams can reason about more easily than a raw multiplier.

### Implementation Hints

- The velocity detector lives in `chappie/engine/loop_detector.py`, in the `VelocityDetector` class.
- Welford's algorithm is straightforward: maintain `count`, `mean`, and `M2` (sum of squared deviations). See the Wikipedia pseudocode.
- Keep the existing EMA path as a fallback while warming up (fewer than 5 samples). The z-score is undefined with no variance data.
- Add tests for: normal traffic (no flag), genuine spike detection, warm-up window behavior, single-sample edge case.
- The existing `test_loop_detector.py` has the test pattern to follow.

---

## Issue 4: Add model loop leaderboard

**Title**: Add model loop leaderboard (budgetctl benchmark)

**Labels**: `enhancement`, `good first issue`, `cli`, `observability`

**Body**:

Chappie detects loops and tags each event with the model name. That data is currently not aggregated anywhere. A per-model leaderboard would answer a useful question: which LLMs loop the most?

### Expected Behavior

A new CLI command:

```
budgetctl benchmark
```

Output:

```
  Model                  Calls    Loops    Rate     Avg Cost/Call
  claude-haiku-4-5       12,450   89       0.71%    $0.003
  gpt-4o-mini            8,200    142      1.73%    $0.008
  claude-sonnet-4        5,100    18       0.35%    $0.024
  gpt-4o                 3,800    31       0.82%    $0.041
  mistral-large          2,100    67       3.19%    $0.015

  Most loop-prone: mistral-large (3.19%)
  Most cost-efficient: claude-haiku-4-5 ($0.003/call, 0.71% loop rate)
```

Also exposed at a new API endpoint: `GET /api/benchmark`

The `--format json` flag works here too, consistent with all other `budgetctl` commands.

### Implementation Hints

- Look at how `budgetctl budget list` is implemented in `cli/main.py` and the corresponding API endpoint in `chappie/api.py`. Follow the same pattern.
- Store per-model counters in the same store used for budgets (`RedisStore` or `MemoryStore`). Keys like `model:gpt-4o:calls`, `model:gpt-4o:loops`, `model:gpt-4o:cost` work fine.
- Increment these counters in `ChappieLogger.post_call_hook()` in `chappie/logger.py`.
- Privacy note: store only model name, call count, loop count, and cost aggregates. No prompt content.

---

## Issue 5: Add `budgetctl agent inspect` command

**Title**: Add budgetctl agent inspect command

**Labels**: `enhancement`, `good first issue`, `cli`

**Body**:

`budgetctl status` shows a summary across all agents. There is no way to drill into a single agent and see all of its state in one view.

### Expected Behavior

```
budgetctl agent inspect email-drafter
```

Output:

```
Agent: email-drafter
------------------------------------------------------------

Budget
  Scope:      agent
  Spent:      $24.87 / $50.00  (49%)
  Remaining:  $25.13
  Status:     OK

Circuit Breaker
  State:      CLOSED
  Trips:      2 total
  Last trip:  2025-01-14T09:12:00Z  (reason: hash_dedup)

Loop Detection
  Calls tracked:    47 (last 20 in window)
  Loops detected:   2
  Last loop:        2025-01-14T09:12:00Z  (strategy: hash_dedup)

Recent Activity
  Last call:        2025-01-15T14:28:00Z
  Calls today:      23
  Cost today:       $3.21
```

If the agent does not exist in the store, print a clear message: `No data found for agent 'email-drafter'.`

### Implementation Hints

- The data is already available across `GET /api/status`, `GET /api/budgets/{scope}/{id}`, and the circuit breaker state.
- Add a `GET /api/agents/{agent_id}` endpoint in `chappie/api.py` that assembles all per-agent data into one response.
- Add the `agent inspect <agent_id>` subcommand in `cli/main.py`. Use Rich's `Panel` and `Table` for layout, consistent with the existing `budgetctl budget get` output.
- The `--format json` flag should work here too.
- Add at least one test covering the case where the agent exists and one where it does not.
