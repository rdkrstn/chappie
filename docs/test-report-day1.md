# Chappie Test Report - Day 1

**Generated:** 2026-04-09T15:42:47.958516+00:00
**Version:** 0.1.0

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

---

## Summary

| Metric | Value |
|--------|-------|
| Modules Tested | 1 |
| Modules Passed | 1 |
| Total Tests | 5 |
| Day | 1 |
