# Chappie Test Report - Day 2

**Generated:** 2026-04-09T16:05:32.763809+00:00
**Version:** 0.2.0

## Loop Detector [PASS]

### Hash Dedup Detection

**Result:** 1 blocked, 4 allowed

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-dedup | What is the weather in Tokyo? | **ALLOWED** | - |
| 2 | agent-dedup | What is the weather in Tokyo? | **ALLOWED** | - |
| 3 | agent-dedup | What is the weather in Tokyo? | **ALLOWED** | - |
| 4 | agent-dedup | What is the weather in Tokyo? | **BLOCKED** | hash_dedup |
| 5 | agent-dedup | Tell me about Python | **ALLOWED** | - |

**Stats:**
```json
{
  "agent_id": "agent-dedup",
  "total_calls": 4,
  "window_size": 4,
  "window_capacity": 10,
  "unique_hashes": 2,
  "current_velocity_cpm": 4.0,
  "velocity_baseline_cpm": 2.205
}
```

### Cycle Detection (A-B-A-B)

**Result:** 1 blocked, 6 allowed

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-cycle | Summarize the document | **ALLOWED** | - |
| 2 | agent-cycle | Translate to Spanish | **ALLOWED** | - |
| 3 | agent-cycle | Summarize the document | **ALLOWED** | - |
| 4 | agent-cycle | Translate to Spanish | **ALLOWED** | - |
| 5 | agent-cycle | Summarize the document | **ALLOWED** | - |
| 6 | agent-cycle | Translate to Spanish | **ALLOWED** | - |
| 7 | agent-cycle | Summarize the document | **BLOCKED** | cycle |

**Stats:**
```json
{
  "agent_id": "agent-cycle",
  "total_calls": 6,
  "window_size": 6,
  "window_capacity": 20,
  "unique_hashes": 2,
  "current_velocity_cpm": 6.0,
  "velocity_baseline_cpm": 2.4310125000000005
}
```

### No False Positives (varied prompts)

**Result:** 0 false positives (should be 0)

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-varied | What is machine learning? | **ALLOWED** | - |
| 2 | agent-varied | Explain neural networks | **ALLOWED** | - |
| 3 | agent-varied | How does backpropagation work? | **ALLOWED** | - |
| 4 | agent-varied | What is gradient descent? | **ALLOWED** | - |
| 5 | agent-varied | Tell me about transformers | **ALLOWED** | - |
| 6 | agent-varied | What is attention mechanism? | **ALLOWED** | - |
| 7 | agent-varied | Explain BERT architecture | **ALLOWED** | - |
| 8 | agent-varied | What is GPT? | **ALLOWED** | - |

**Stats:**
```json
{
  "agent_id": "agent-varied",
  "total_calls": 8,
  "window_size": 8,
  "window_capacity": 10,
  "unique_hashes": 8,
  "current_velocity_cpm": 8.0,
  "velocity_baseline_cpm": 2.680191281250001
}
```

### Agent Isolation

**Result:** PASS - agents isolated

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-A | What is the weather? | **ALLOWED** | - |
| 2 | agent-A | What is the weather? | **ALLOWED** | - |
| 1 | agent-B | What is the weather? | **ALLOWED** | - |
| 2 | agent-B | What is the weather? | **ALLOWED** | - |

### Window Size Enforcement

**Result:** Window size: 5 (max 5)

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 8 | agent-window | 8 prompts recorded into window_size=5 | **INFO** | - |

**Stats:**
```json
{
  "agent_id": "agent-window",
  "total_calls": 8,
  "window_size": 5,
  "window_capacity": 5,
  "unique_hashes": 5,
  "current_velocity_cpm": 8.0,
  "velocity_baseline_cpm": 2.680191281250001
}
```

## Circuit Breaker [PASS]

### Circuit Breaker Trip on Loop

**Result:** 3 allowed, 1 tripped, 2 blocked

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-loop-trip | What is the weather in Tokyo? | **ALLOWED** | - |
| 2 | agent-loop-trip | What is the weather in Tokyo? | **ALLOWED** | - |
| 3 | agent-loop-trip | What is the weather in Tokyo? | **ALLOWED** | - |
| 4 | agent-loop-trip | What is the weather in Tokyo? | **TRIPPED** | hash_dedup |
| 5 | agent-loop-trip | What is the weather in Tokyo? | **BLOCKED** | circuit_breaker |
| 6 | agent-loop-trip | What is the weather in Tokyo? | **BLOCKED** | circuit_breaker |

**Stats:**
```json
{
  "total_calls": 6,
  "allowed": 3,
  "tripped": 1,
  "blocked_after_trip": 2
}
```

### Circuit Breaker Auto-Recovery

**Result:** PASS - CB recovered to CLOSED after cooldown + success

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-recovery | (manual trip) | **TRIPPED** | manual |
| 2 | agent-recovery | (probe after cooldown) | **ALLOWED** | auto_recovery |
| 3 | agent-recovery | (success recorded) | **RECOVERED** | auto_recovery |

**Stats:**
```json
{
  "trip_state": "open",
  "half_open_state": "half_open",
  "final_state": "closed",
  "recovered": true
}
```

### Circuit Breaker Manual Reset

**Result:** PASS - manual reset restored CLOSED state

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-manual-reset | (manual trip) | **TRIPPED** | manual |
| 2 | agent-manual-reset | (verify open) | **BLOCKED** | circuit_breaker |
| 3 | agent-manual-reset | (manual reset) | **RECOVERED** | manual_reset |
| 4 | agent-manual-reset | (verify allowed) | **ALLOWED** | - |

**Stats:**
```json
{
  "trip_state": "open",
  "reset_state": "closed",
  "reset_worked": true
}
```

### Error Threshold Trip

**Result:** PASS - CB tripped at call 3 (threshold=3)

| Call | Agent | Prompt | Action | Strategy |
|------|-------|--------|--------|----------|
| 1 | agent-errors | (failure #1) | **ALLOWED** | - |
| 2 | agent-errors | (failure #2) | **ALLOWED** | - |
| 3 | agent-errors | (failure #3) | **TRIPPED** | error_threshold |
| 4 | agent-errors | (failure #4) | **BLOCKED** | circuit_breaker |
| 5 | agent-errors | (failure #5) | **BLOCKED** | circuit_breaker |

**Stats:**
```json
{
  "error_threshold": 3,
  "tripped_at_call": 3,
  "total_errors_sent": 5,
  "tripped": true
}
```

---

## Summary

| Metric | Value |
|--------|-------|
| Modules Tested | 2 |
| Modules Passed | 2 |
| Total Tests | 9 |
| Day | 2 |
