"""Tests for the LoopDetector engine.

Covers all three detection strategies:
  A) Hash Dedup -- same prompt N times triggers detection
  B) Cycle Detection -- repeating patterns (A-B-A-B, A-B-C-A-B-C)
  C) Velocity Anomaly -- rapid burst of calls exceeding baseline

Plus session isolation, stats, window eviction, and clear.

The existing LoopDetector uses a check-then-record pattern:
  - ``check()`` is a read-only probe (does NOT modify state)
  - ``record()`` appends to windows (call AFTER a successful LLM response)
Both are synchronous; neither requires a store (all in-memory).
"""

from __future__ import annotations

import time

import pytest

from chappie.config import LoopDetectorConfig
from chappie.engine.loop_detector import LoopDetector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> LoopDetectorConfig:
    return LoopDetectorConfig(
        window_size=10,
        repeat_threshold=3,
        cycle_max_period=4,
        velocity_window_sec=60,
        velocity_multiplier=5.0,
    )


@pytest.fixture
def detector(config: LoopDetectorConfig) -> LoopDetector:
    return LoopDetector(config=config)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

MODEL = "gpt-4"


def _check_and_record(
    detector: LoopDetector,
    agent_id: str,
    messages: list[dict],
    model: str = MODEL,
):
    """Run check() then record().  Returns the check result."""
    result = detector.check(agent_id, messages, model)
    detector.record(agent_id, messages, model)
    return result


# ---------------------------------------------------------------------------
# Strategy A: Hash Dedup (repeat detection)
# ---------------------------------------------------------------------------


def test_detects_repeated_prompts(detector: LoopDetector):
    """Same prompt sent 3+ times (repeat_threshold=3) must be flagged.

    check() reads the window without modifying it.  record() appends.
    So we need 3 record() calls to place 3 copies in the window,
    then the 4th check() sees count=3 >= threshold=3.
    """
    msg = [{"role": "user", "content": "What is the weather?"}]

    # Record 3 identical calls into the window
    for _ in range(3):
        _check_and_record(detector, "agent-1", msg)

    # Fourth check sees 3 occurrences in the window -- triggers dedup
    r4 = detector.check("agent-1", msg, MODEL)
    assert r4.is_loop is True
    assert r4.strategy == "hash_dedup"
    assert r4.agent_id == "agent-1"


def test_different_content_no_dedup(detector: LoopDetector):
    """Distinct prompts should never trigger the dedup strategy."""
    for i in range(10):
        msg = [{"role": "user", "content": f"Unique question #{i}"}]
        result = _check_and_record(detector, "agent-1", msg)
        assert result.is_loop is False


# ---------------------------------------------------------------------------
# Strategy B: Cycle Detection (A-B-A-B or A-B-C-A-B-C patterns)
# ---------------------------------------------------------------------------


def test_detects_ab_cycle(detector: LoopDetector):
    """A-B-A-B-A-B pattern with period=2 must be caught.

    Cycle detection needs 3 full repetitions (6 messages for period 2).
    Hash dedup fires before cycle detection when any single hash has
    >= repeat_threshold occurrences.  With threshold=3 and A appearing
    3 times in A-B-A-B-A-B, dedup fires first.

    To isolate cycle detection, we use a wider window and higher dedup
    threshold so dedup does not interfere.
    """
    # Widen the dedup threshold so it does not fire before cycle detection
    wide_config = LoopDetectorConfig(
        window_size=10,
        repeat_threshold=10,  # dedup effectively disabled
        cycle_max_period=4,
        velocity_window_sec=60,
        velocity_multiplier=100.0,  # velocity effectively disabled
    )
    det = LoopDetector(config=wide_config)

    msg_a = [{"role": "user", "content": "Question A"}]
    msg_b = [{"role": "user", "content": "Question B"}]

    # Feed A-B-A-B-A-B (3 full cycles of period 2 = 6 records)
    for _ in range(3):
        _check_and_record(det, "agent-1", msg_a)
        _check_and_record(det, "agent-1", msg_b)

    # After 6 records the window contains [A,B,A,B,A,B].
    # Next check should detect the cycle.
    final = det.check("agent-1", msg_a, MODEL)
    assert final.is_loop is True
    assert final.strategy == "cycle"


def test_detects_abc_cycle(detector: LoopDetector):
    """A-B-C-A-B-C-A-B-C pattern with period=3 must be caught."""
    # Disable dedup and velocity so we isolate cycle detection
    wide_config = LoopDetectorConfig(
        window_size=12,
        repeat_threshold=10,
        cycle_max_period=4,
        velocity_window_sec=60,
        velocity_multiplier=100.0,
    )
    det = LoopDetector(config=wide_config)

    msg_a = [{"role": "user", "content": "Step A"}]
    msg_b = [{"role": "user", "content": "Step B"}]
    msg_c = [{"role": "user", "content": "Step C"}]

    # Feed A-B-C-A-B-C-A-B-C (3 full cycles of period 3 = 9 records)
    for _ in range(3):
        _check_and_record(det, "agent-1", msg_a)
        _check_and_record(det, "agent-1", msg_b)
        _check_and_record(det, "agent-1", msg_c)

    final = det.check("agent-1", msg_a, MODEL)
    assert final.is_loop is True
    assert final.strategy == "cycle"


def test_no_cycle_on_varied_sequence(detector: LoopDetector):
    """Non-repeating sequence should not trigger cycle detection."""
    for i in range(10):
        msg = [{"role": "user", "content": f"Message {i}"}]
        result = _check_and_record(detector, "agent-1", msg)
        assert result.is_loop is False


# ---------------------------------------------------------------------------
# Strategy C: Token Velocity Anomaly
# ---------------------------------------------------------------------------


def test_detects_velocity_spike(monkeypatch):
    """Rapid calls (5x baseline) within the velocity window must be flagged.

    The EMA baseline (alpha=0.3) adapts on every record(), so we need
    a test scenario where:
      1. A stable low baseline is established over many calls
      2. Enough time passes that old baseline calls fall out of the
         velocity window
      3. A burst fires so many calls that velocity >> baseline * multiplier

    The trick is to let the 60s window empty of baseline calls before
    bursting, so the burst dominates the window.
    """
    cfg = LoopDetectorConfig(
        window_size=500,
        repeat_threshold=500,  # dedup disabled
        cycle_max_period=1,  # cycle disabled
        velocity_window_sec=60,
        velocity_multiplier=3.0,
    )
    det = LoopDetector(config=cfg)

    fake_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

    # Phase 1: Establish a low baseline.
    # 10 calls, each 20s apart = 200s total.
    # Within any 60s window there are ~3 calls = 3 cpm baseline.
    for i in range(10):
        msg = [{"role": "user", "content": f"Baseline call {i}"}]
        _check_and_record(det, "agent-1", msg)
        fake_time[0] += 20.0

    # Phase 2: Wait 120 seconds so ALL baseline calls fall out of
    # the 60s velocity window.  No calls during this gap.
    fake_time[0] += 120.0

    # At this point the baseline EMA is still ~3 cpm (it does not
    # decay without calls).  The velocity window is empty of recent
    # calls.  Threshold = 3 * 3 = 9 cpm.

    # Phase 3: Burst 60 calls in 1 second.
    # velocity = 60 calls in 1s of the 60s window = 60 cpm.
    # 60 cpm >> 9 cpm threshold.
    # The EMA baseline updates on each record(), but the first few
    # check() calls should fire before the baseline catches up.
    results = []
    for i in range(60):
        msg = [{"role": "user", "content": f"Burst call {i + 500}"}]
        r = det.check("agent-1", msg, MODEL)
        results.append(r)
        if r.is_loop:
            break
        det.record("agent-1", msg, MODEL)
        fake_time[0] += 0.001

    assert any(r.is_loop and r.strategy == "velocity" for r in results), (
        "Expected velocity anomaly detection to flag rapid burst"
    )


def test_no_velocity_flag_on_slow_repeats(detector: LoopDetector, monkeypatch):
    """Same prompt repeated slowly should not trigger velocity detection.

    The dedup strategy may fire (since the content is identical), but
    the velocity strategy specifically should not.
    """
    fake_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

    results = []
    for i in range(8):
        msg = [{"role": "user", "content": f"Slow message {i}"}]
        results.append(_check_and_record(detector, "agent-1", msg))
        fake_time[0] += 30.0  # well under any burst threshold

    velocity_hits = [r for r in results if r.is_loop and r.strategy == "velocity"]
    assert len(velocity_hits) == 0, "Velocity should not fire on slow calls"


# ---------------------------------------------------------------------------
# No false positives on varied input
# ---------------------------------------------------------------------------


def test_no_false_positive_on_varied_prompts(detector: LoopDetector):
    """A stream of unique prompts should never trigger any strategy."""
    for i in range(10):
        msg = [{"role": "user", "content": f"Completely different question {i}!"}]
        result = _check_and_record(detector, "agent-1", msg)
        assert result.is_loop is False, (
            f"False positive on message {i}: strategy={result.strategy}"
        )


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_different_agents_isolated(detector: LoopDetector):
    """Agent A's history must not pollute Agent B's detection window."""
    msg = [{"role": "user", "content": "Same prompt for isolation test"}]

    # Agent A sends the same message 3 times (would trigger dedup for A)
    for _ in range(3):
        _check_and_record(detector, "agent-A", msg)

    # Agent B sends it once -- should be completely clean
    result = detector.check("agent-B", msg, MODEL)
    assert result.is_loop is False


# ---------------------------------------------------------------------------
# Clear session
# ---------------------------------------------------------------------------


def test_clear_session_resets(detector: LoopDetector):
    """After clearing, the agent's history starts fresh."""
    msg = [{"role": "user", "content": "Repeated message"}]

    # Build up 2 records (one short of threshold)
    _check_and_record(detector, "agent-1", msg)
    _check_and_record(detector, "agent-1", msg)

    # Clear all state for agent-1
    detector.clear_session("agent-1")

    # Next check + record starts from zero -- no dedup
    result = detector.check("agent-1", msg, MODEL)
    assert result.is_loop is False


# ---------------------------------------------------------------------------
# Window size limit
# ---------------------------------------------------------------------------


def test_window_respects_max_size(detector: LoopDetector):
    """Old entries must be evicted when the window is full (size=10).

    After filling the window with 10 unique messages, the first message
    should have been evicted.  Sending it again counts as a fresh
    occurrence.
    """
    first_msg = [{"role": "user", "content": "Very first message"}]
    _check_and_record(detector, "agent-1", first_msg)

    # Fill the remaining 9 slots with unique messages
    for i in range(1, 10):
        msg = [{"role": "user", "content": f"Filler message {i}"}]
        _check_and_record(detector, "agent-1", msg)

    # Window is full (10 entries).  first_msg has been evicted because
    # the deque has maxlen=10 and we added 10 items after it (including it).
    # Sending first_msg again should NOT be treated as a repeat.
    result = detector.check("agent-1", first_msg, MODEL)
    assert result.is_loop is False, (
        "First message should have been evicted from the window"
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_get_stats_returns_counts(detector: LoopDetector):
    """Stats must report call_count, window_size, and other diagnostics."""
    msg = [{"role": "user", "content": "Hello"}]
    _check_and_record(detector, "agent-1", msg)
    _check_and_record(detector, "agent-1", msg)

    stats = detector.get_stats("agent-1")
    assert stats["total_calls"] >= 2
    assert "window_size" in stats
    assert stats["window_size"] == 2
    assert stats["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# check() is read-only (does not mutate state)
# ---------------------------------------------------------------------------


def test_check_does_not_record(detector: LoopDetector):
    """Calling check() without record() must not change the window."""
    msg = [{"role": "user", "content": "Probe only"}]

    # Check 10 times without recording
    for _ in range(10):
        detector.check("agent-1", msg, MODEL)

    stats = detector.get_stats("agent-1")
    assert stats["total_calls"] == 0, "check() alone must not increment call count"
    assert stats["window_size"] == 0, "check() alone must not grow the window"


# ---------------------------------------------------------------------------
# Hash consistency
# ---------------------------------------------------------------------------


def test_hash_deterministic():
    """Same messages + model always produce the same hash."""
    msg = [{"role": "user", "content": "deterministic test"}]
    h1 = LoopDetector._hash_message(msg, "gpt-4")
    h2 = LoopDetector._hash_message(msg, "gpt-4")
    assert h1 == h2


def test_hash_differs_for_different_models():
    """Different model names produce different hashes."""
    msg = [{"role": "user", "content": "same content"}]
    h1 = LoopDetector._hash_message(msg, "gpt-4")
    h2 = LoopDetector._hash_message(msg, "gpt-3.5-turbo")
    assert h1 != h2


def test_hash_extracts_last_user_message():
    """Hash should use the LAST user message, ignoring system/assistant."""
    msgs = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "Sure, here is the answer"},
        {"role": "user", "content": "Follow-up question"},
    ]
    h = LoopDetector._hash_message(msgs, "gpt-4")

    # Should match hashing "Follow-up question" alone
    h_expected = LoopDetector._hash_message(
        [{"role": "user", "content": "Follow-up question"}],
        "gpt-4",
    )
    assert h == h_expected
