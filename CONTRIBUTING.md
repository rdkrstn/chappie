# Contributing to Chappie

Thank you for your interest in contributing. Chappie is early-stage software and there are meaningful problems to solve. This guide gets you from zero to a submitted PR.

## Table of Contents

- [Dev Environment Setup](#dev-environment-setup)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Submitting a PR](#submitting-a-pr)
- [Good First Issues](#good-first-issues)
- [v0.2 Roadmap](#v02-roadmap)

---

## Dev Environment Setup

**Prerequisites**: Python 3.10+, Git

```bash
git clone https://github.com/rdkrstn/chappie.git
cd chappie
pip install -e ".[dev]"
```

The `-e` flag installs the package in editable mode so your local changes take effect immediately without reinstalling. The `[dev]` extras pull in pytest, fakeredis, and ruff.

To verify the install worked:

```bash
python -c "import budgetctl; print(budgetctl.__version__)"
budgetctl --help
```

### Optional: Redis

Most tests use an in-memory store (no Redis needed). If you want to test against a real Redis instance, the fastest path is Docker:

```bash
docker run -p 6379:6379 redis:7-alpine
export BUDGETCTL_REDIS_URL=redis://localhost:6379
```

---

## Running Tests

```bash
pytest
```

That runs all 81 tests. For a specific module:

```bash
pytest tests/test_loop_detector.py
pytest tests/test_circuit_breaker.py
pytest tests/test_budget_enforcer.py
pytest tests/test_store.py
```

For verbose output with test names:

```bash
pytest -v
```

### Test Report

Chappie ships a report generator that runs all integration demos and outputs a structured Markdown file. To generate it:

```bash
python tests/report.py
```

The report covers the full pipeline: loop detection, circuit breaker state transitions, and budget enforcement. The output is written to `tests/test-report-day6.md` and the raw data to `tests/test-report-day6.json`.

To view the HTML report (Windows):

```bat
view-report.bat
```

Or open `tests/test-report.html` directly in a browser.

### What the Test Suite Covers

| Module | Tests | What Is Tested |
|--------|-------|---------------|
| Loop Detector | 17 | Hash dedup, cycle detection (period 2/3/4), velocity anomaly, warmup, agent isolation, window boundaries |
| Circuit Breaker | 22 | State transitions (CLOSED/OPEN/HALF_OPEN), error counting, cooldown timing, manual reset, trip reasons, multi-agent isolation |
| Budget Enforcer | 18 | Reserve/reconcile/release flow, budget exceeded rejection, threshold alerts, scope independence, spend reset |
| Store Layer | 24 | Get/set/delete, TTL expiry, `incr_float`, hash operations, Lua script evaluation, ping |

---

## Code Style

Chappie uses [ruff](https://github.com/astral-sh/ruff) for linting and formatting.

```bash
ruff check .
ruff format .
```

Both commands run automatically in CI on every push and PR. A failing lint check blocks merge.

Key rules to follow:

- **Line length**: 100 characters (set in `pyproject.toml`)
- **Target**: Python 3.10+ syntax only
- **Type hints**: Required on all public functions
- **Docstrings**: One-line summary for public functions and classes; multi-line for anything non-obvious
- **No em dashes in documentation or docstrings**: Use a plain hyphen or rewrite the sentence

---

## Submitting a PR

1. Fork the repo and create a branch from `main`:

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes. Keep commits focused: one logical change per commit.

3. Add tests for any new behavior. PRs that add code without tests will not be merged.

4. Confirm tests and lint pass locally:

   ```bash
   ruff check .
   pytest
   ```

5. Open a PR against `main`. Fill out the PR description with:
   - What the change does
   - Why it is needed
   - How you tested it

6. A maintainer will review and may request changes. Respond to comments directly in the PR thread.

**PR checklist:**

- [ ] Tests pass (`pytest`)
- [ ] Lint passes (`ruff check .`)
- [ ] New behavior is covered by tests
- [ ] Docstrings updated if the public API changed
- [ ] `CHANGELOG.md` entry added (if one exists by the time you read this)

---

## Good First Issues

These are well-scoped, self-contained tasks that do not require deep knowledge of the full codebase. Each one is tracked as a GitHub issue with the `good first issue` label.

### 1. Add email alert channel

**What**: Chappie supports Slack and generic webhooks. SMTP email is a common request. Add a new alert channel that sends an email when a threshold is crossed or a circuit breaker trips.

**Where to start**: `budgetctl/alerts.py`. See how `SlackChannel` is implemented and follow the same pattern for an `SMTPChannel`.

**Skills needed**: Python, `smtplib` or `aiosmtplib`, Pydantic settings

**Issue**: [#1 Add email alert channel](https://github.com/rdkrstn/chappie/issues)

---

### 2. Build Next.js dashboard

**What**: The REST API (`/api/status`, `/api/budgets`, `/api/events`) already exists. Build a Next.js frontend that displays real-time cost and loop data using the SSE events stream.

**Where to start**: `budgetctl/api.py` for the existing endpoints, `README.md` for the API reference

**Skills needed**: Next.js, React, SSE/EventSource, Tailwind or similar

**Issue**: [#2 Build Next.js dashboard](https://github.com/rdkrstn/chappie/issues)

---

### 3. Add anomaly detection

**What**: The velocity detector uses a simple EMA baseline. Welford's online algorithm computes a running mean and variance in constant time and constant space. This would let Chappie detect statistically significant deviations rather than a fixed multiplier.

**Where to start**: `budgetctl/engine/loop_detector.py`, the `VelocityDetector` class

**Skills needed**: Python, statistics (Welford's algorithm or z-score basics)

**Issue**: [#3 Add anomaly detection](https://github.com/rdkrstn/chappie/issues)

---

### 4. Add model loop leaderboard

**What**: Chappie already tags every loop event with the model name. Aggregate this into a per-model benchmark (calls, loops, loop rate, avg cost) accessible via `budgetctl benchmark` and a new API endpoint.

**Where to start**: `budgetctl/engine/loop_detector.py` for where events are emitted, `cli/main.py` for the CLI pattern, `budgetctl/api.py` for the API pattern

**Skills needed**: Python, Click, Rich tables, FastAPI

**Issue**: [#4 Add model loop leaderboard](https://github.com/rdkrstn/chappie/issues)

---

### 5. Add `budgetctl agent inspect` command

**What**: `budgetctl status` shows a summary. There is no way to drill into a single agent and see its full state in one view: budget, circuit breaker state, recent loops detected, and call history.

**Where to start**: `cli/main.py` for the CLI pattern, `budgetctl/api.py` for the data to expose

**Skills needed**: Python, Click, Rich panels/tables

**Issue**: [#5 Add budgetctl agent inspect command](https://github.com/rdkrstn/chappie/issues)

---

## v0.2 Roadmap

The v0.2 plan covers two major features:

**Adaptive Insights**: After 10 sessions, Chappie switches from static defaults to per-agent adaptive thresholds based on actual P95 behavior. Fewer false positives, tighter detection for agents with naturally high call counts.

**Model Loop Leaderboard**: A per-model benchmark aggregating loop rates, call counts, and cost. Accessible via `budgetctl benchmark` and the REST API.

See the "Coming Soon" section of the [README](README.md#coming-soon-adaptive-insights--model-benchmarking) for the full design.

If you want to work on v0.2 features, open a discussion issue first so we can coordinate.
