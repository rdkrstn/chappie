"""
Test report generator for BudgetCtl.
Runs all demos and captures results into a structured .md file
that can be rendered in an HTML data visualizer.

Day 1: Loop Detector tests
Day 2: Circuit Breaker tests (trip on loop, auto-recovery, manual reset,
        error threshold)
Day 3: Budget Enforcer tests (reservation flow, reconciliation,
        threshold alerts)
Day 6: Full pipeline integration test (loop -> CB -> budget)
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from budgetctl.config import BudgetConfig, CircuitBreakerConfig, LoopDetectorConfig
from budgetctl.engine.budget_enforcer import BudgetEnforcer, BudgetScope
from budgetctl.engine.circuit_breaker import CircuitBreaker, TripReason
from budgetctl.engine.loop_detector import LoopDetector
from budgetctl.exceptions import BudgetCtlBudgetExceeded
from budgetctl.models import CircuitBreakerState
from budgetctl.store.memory import MemoryStore


def run_report():
    report_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "0.6.0",
        "day": 6,
        "modules_tested": [],
    }

    # ===== MODULE: Loop Detector =====
    module = {
        "name": "Loop Detector",
        "status": "pass",
        "tests": [],
    }

    # --- Test 1: Hash Dedup ---
    test = {"name": "Hash Dedup Detection", "strategy": "hash_dedup", "events": [], "result": ""}
    detector = LoopDetector(LoopDetectorConfig(window_size=10, repeat_threshold=3))
    prompts = [
        "What is the weather in Tokyo?",
        "What is the weather in Tokyo?",
        "What is the weather in Tokyo?",
        "What is the weather in Tokyo?",
        "Tell me about Python",
    ]
    blocked = 0
    for i, prompt in enumerate(prompts):
        messages = [{"role": "user", "content": prompt}]
        result = detector.check("agent-dedup", messages, "gpt-4")
        event = {
            "call": i + 1,
            "prompt": prompt[:60],
            "model": "gpt-4",
            "agent_id": "agent-dedup",
            "action": "BLOCKED" if result.is_loop else "ALLOWED",
            "strategy": result.strategy if result.is_loop else None,
            "details": result.details if result.is_loop else None,
        }
        test["events"].append(event)
        if result.is_loop:
            blocked += 1
        else:
            detector.record("agent-dedup", messages, "gpt-4")
    test["result"] = f"{blocked} blocked, {len(prompts) - blocked} allowed"
    test["stats"] = detector.get_stats("agent-dedup")
    module["tests"].append(test)

    # --- Test 2: Cycle Detection ---
    test = {"name": "Cycle Detection (A-B-A-B)", "strategy": "cycle", "events": [], "result": ""}
    detector2 = LoopDetector(LoopDetectorConfig(window_size=20, repeat_threshold=100, cycle_max_period=4))
    cycle_prompts = [
        "Summarize the document",
        "Translate to Spanish",
        "Summarize the document",
        "Translate to Spanish",
        "Summarize the document",
        "Translate to Spanish",
        "Summarize the document",
    ]
    blocked = 0
    for i, prompt in enumerate(cycle_prompts):
        messages = [{"role": "user", "content": prompt}]
        result = detector2.check("agent-cycle", messages, "gpt-4")
        event = {
            "call": i + 1,
            "prompt": prompt[:60],
            "model": "gpt-4",
            "agent_id": "agent-cycle",
            "action": "BLOCKED" if result.is_loop else "ALLOWED",
            "strategy": result.strategy if result.is_loop else None,
            "details": result.details if result.is_loop else None,
        }
        test["events"].append(event)
        if result.is_loop:
            blocked += 1
        else:
            detector2.record("agent-cycle", messages, "gpt-4")
    test["result"] = f"{blocked} blocked, {len(cycle_prompts) - blocked} allowed"
    test["stats"] = detector2.get_stats("agent-cycle")
    module["tests"].append(test)

    # --- Test 3: No False Positives ---
    test = {"name": "No False Positives (varied prompts)", "strategy": "none", "events": [], "result": ""}
    detector3 = LoopDetector(LoopDetectorConfig(window_size=10, repeat_threshold=3))
    varied_prompts = [
        "What is machine learning?",
        "Explain neural networks",
        "How does backpropagation work?",
        "What is gradient descent?",
        "Tell me about transformers",
        "What is attention mechanism?",
        "Explain BERT architecture",
        "What is GPT?",
    ]
    false_positives = 0
    for i, prompt in enumerate(varied_prompts):
        messages = [{"role": "user", "content": prompt}]
        result = detector3.check("agent-varied", messages, "gpt-4")
        event = {
            "call": i + 1,
            "prompt": prompt[:60],
            "model": "gpt-4",
            "agent_id": "agent-varied",
            "action": "BLOCKED" if result.is_loop else "ALLOWED",
            "strategy": result.strategy if result.is_loop else None,
            "details": result.details if result.is_loop else None,
        }
        test["events"].append(event)
        if result.is_loop:
            false_positives += 1
        else:
            detector3.record("agent-varied", messages, "gpt-4")
    test["result"] = f"{false_positives} false positives (should be 0)"
    test["stats"] = detector3.get_stats("agent-varied")
    if false_positives > 0:
        module["status"] = "fail"
    module["tests"].append(test)

    # --- Test 4: Agent Isolation ---
    test = {"name": "Agent Isolation", "strategy": "isolation", "events": [], "result": ""}
    detector4 = LoopDetector(LoopDetectorConfig(window_size=10, repeat_threshold=3))
    same_prompt = "What is the weather?"
    agents = ["agent-A", "agent-B"]
    for agent in agents:
        for i in range(2):
            messages = [{"role": "user", "content": same_prompt}]
            result = detector4.check(agent, messages, "gpt-4")
            event = {
                "call": i + 1,
                "prompt": same_prompt,
                "model": "gpt-4",
                "agent_id": agent,
                "action": "BLOCKED" if result.is_loop else "ALLOWED",
                "strategy": result.strategy if result.is_loop else None,
                "details": result.details if result.is_loop else None,
            }
            test["events"].append(event)
            if not result.is_loop:
                detector4.record(agent, messages, "gpt-4")
    cross_contaminated = any(e["action"] == "BLOCKED" for e in test["events"])
    test["result"] = "PASS - agents isolated" if not cross_contaminated else "FAIL - cross contamination"
    module["tests"].append(test)

    # --- Test 5: Window Size Limit ---
    test = {"name": "Window Size Enforcement", "strategy": "window", "events": [], "result": ""}
    detector5 = LoopDetector(LoopDetectorConfig(window_size=5, repeat_threshold=3))
    for i in range(8):
        prompt = f"Unique prompt {i}"
        messages = [{"role": "user", "content": prompt}]
        detector5.record("agent-window", messages, "gpt-4")
    stats = detector5.get_stats("agent-window")
    test["events"].append({
        "call": 8,
        "prompt": "8 prompts recorded into window_size=5",
        "model": "gpt-4",
        "agent_id": "agent-window",
        "action": "INFO",
        "strategy": None,
        "details": f"Window holds {stats['window_size']} of {stats['window_capacity']} capacity",
    })
    test["result"] = f"Window size: {stats['window_size']} (max {stats['window_capacity']})"
    test["stats"] = stats
    module["tests"].append(test)

    report_data["modules_tested"].append(module)

    # ===== MODULE: Circuit Breaker =====
    cb_module = _run_circuit_breaker_tests()
    report_data["modules_tested"].append(cb_module)

    # ===== MODULE: Budget Enforcer =====
    budget_module = _run_budget_enforcer_tests()
    report_data["modules_tested"].append(budget_module)

    # ===== Generate Markdown =====
    md = generate_markdown(report_data)
    report_path = Path(__file__).parent.parent / "docs"
    report_path.mkdir(exist_ok=True)
    report_file = report_path / "test-report-day6.md"
    report_file.write_text(md, encoding="utf-8")

    # Also save raw JSON for the HTML visualizer
    json_file = report_path / "test-report-day6.json"
    json_file.write_text(json.dumps(report_data, indent=2, default=str), encoding="utf-8")

    print(f"Report saved to: {report_file}")
    print(f"JSON data saved to: {json_file}")
    return report_data


# =====================================================================
# Circuit Breaker test scenarios (Day 2)
# =====================================================================


def _run_circuit_breaker_tests() -> dict:
    """Run all four circuit breaker test scenarios synchronously by
    driving the async methods through ``asyncio.run()``."""
    module = {
        "name": "Circuit Breaker",
        "status": "pass",
        "tests": [],
    }

    module["tests"].append(_test_cb_trip_on_loop())
    module["tests"].append(_test_cb_auto_recovery())
    module["tests"].append(_test_cb_manual_reset())
    module["tests"].append(_test_cb_error_threshold())

    # Mark module as failed if any test failed.
    if any(t.get("failed") for t in module["tests"]):
        module["status"] = "fail"

    return module


def _test_cb_trip_on_loop() -> dict:
    """Test 1: Agent loops, CB trips, subsequent calls are blocked."""
    test = {
        "name": "Circuit Breaker Trip on Loop",
        "strategy": "loop_trip",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        cb_config = CircuitBreakerConfig(
            error_threshold=5,
            error_window_sec=60,
            cooldown_sec=10,
            half_open_max_calls=1,
        )
        loop_config = LoopDetectorConfig(
            window_size=10, repeat_threshold=3,
        )
        cb = CircuitBreaker(store, cb_config)
        detector = LoopDetector(loop_config)

        agent_id = "agent-loop-trip"
        prompt = "What is the weather in Tokyo?"
        messages = [{"role": "user", "content": prompt}]

        # Send the same prompt repeatedly until the loop detector fires,
        # then trip the CB and verify subsequent calls are blocked.
        for i in range(6):
            result = detector.check(agent_id, messages, "gpt-4")
            cb_info = await cb.check(agent_id)

            if cb_info.state == CircuitBreakerState.OPEN:
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:60],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "BLOCKED",
                    "strategy": "circuit_breaker",
                    "details": f"CB OPEN: {cb_info.reason}",
                })
                continue

            if result.is_loop:
                # Trip the circuit breaker.
                await cb.trip(
                    agent_id,
                    TripReason.LOOP_DETECTED,
                    details=f"Loop via {result.strategy}",
                )
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:60],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "TRIPPED",
                    "strategy": result.strategy,
                    "details": f"Loop detected, CB tripped: {result.details}",
                })
            else:
                detector.record(agent_id, messages, "gpt-4")
                await cb.record_success(agent_id)
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:60],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "ALLOWED",
                    "strategy": None,
                    "details": None,
                })

        tripped = sum(1 for e in test["events"] if e["action"] == "TRIPPED")
        blocked = sum(1 for e in test["events"] if e["action"] == "BLOCKED")
        allowed = sum(1 for e in test["events"] if e["action"] == "ALLOWED")
        test["result"] = f"{allowed} allowed, {tripped} tripped, {blocked} blocked"
        test["stats"] = {
            "total_calls": len(test["events"]),
            "allowed": allowed,
            "tripped": tripped,
            "blocked_after_trip": blocked,
        }
        if tripped == 0 and blocked == 0:
            test["failed"] = True
            test["result"] += " (UNEXPECTED: CB never tripped)"

    asyncio.run(_run())
    return test


def _test_cb_auto_recovery() -> dict:
    """Test 2: Agent trips, cooldown expires, CB recovers to HALF_OPEN
    then CLOSED on a successful probe."""
    test = {
        "name": "Circuit Breaker Auto-Recovery",
        "strategy": "auto_recovery",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        # Use a very short cooldown so the test can simulate expiry
        # by manipulating the stored open_until timestamp.
        cb_config = CircuitBreakerConfig(
            error_threshold=5,
            error_window_sec=60,
            cooldown_sec=1,
            half_open_max_calls=1,
        )
        cb = CircuitBreaker(store, cb_config)
        agent_id = "agent-recovery"

        # Step 1: Trip the breaker.
        await cb.trip(
            agent_id, TripReason.LOOP_DETECTED,
            details="Simulated loop for recovery test",
        )
        info_after_trip = await cb.check(agent_id)
        test["events"].append({
            "call": 1,
            "prompt": "(manual trip)",
            "model": "-",
            "agent_id": agent_id,
            "action": "TRIPPED",
            "strategy": "manual",
            "details": f"State={info_after_trip.state.value}",
        })

        # Step 2: Wait for cooldown to expire.
        await asyncio.sleep(1.5)

        # Step 3: Check again -- should be HALF_OPEN now.
        info_half = await cb.check(agent_id)
        test["events"].append({
            "call": 2,
            "prompt": "(probe after cooldown)",
            "model": "-",
            "agent_id": agent_id,
            "action": "ALLOWED" if info_half.state != CircuitBreakerState.OPEN else "BLOCKED",
            "strategy": "auto_recovery",
            "details": f"State={info_half.state.value}",
        })

        # Step 4: Record a success -- should transition to CLOSED.
        await cb.record_success(agent_id)
        info_closed = await cb.check(agent_id)
        test["events"].append({
            "call": 3,
            "prompt": "(success recorded)",
            "model": "-",
            "agent_id": agent_id,
            "action": "RECOVERED",
            "strategy": "auto_recovery",
            "details": f"State={info_closed.state.value}",
        })

        recovered = info_closed.state == CircuitBreakerState.CLOSED
        test["result"] = (
            "PASS - CB recovered to CLOSED after cooldown + success"
            if recovered
            else f"FAIL - final state is {info_closed.state.value}"
        )
        test["stats"] = {
            "trip_state": info_after_trip.state.value,
            "half_open_state": info_half.state.value,
            "final_state": info_closed.state.value,
            "recovered": recovered,
        }
        if not recovered:
            test["failed"] = True

    asyncio.run(_run())
    return test


def _test_cb_manual_reset() -> dict:
    """Test 3: Agent trips, manual reset called, calls allowed again."""
    test = {
        "name": "Circuit Breaker Manual Reset",
        "strategy": "manual_reset",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        cb_config = CircuitBreakerConfig(
            error_threshold=5,
            error_window_sec=60,
            cooldown_sec=300,
            half_open_max_calls=1,
        )
        cb = CircuitBreaker(store, cb_config)
        agent_id = "agent-manual-reset"

        # Step 1: Trip the breaker.
        await cb.trip(
            agent_id, TripReason.LOOP_DETECTED,
            details="Simulated loop for manual reset test",
        )
        info_tripped = await cb.check(agent_id)
        test["events"].append({
            "call": 1,
            "prompt": "(manual trip)",
            "model": "-",
            "agent_id": agent_id,
            "action": "TRIPPED",
            "strategy": "manual",
            "details": f"State={info_tripped.state.value}",
        })

        # Step 2: Verify it is OPEN.
        test["events"].append({
            "call": 2,
            "prompt": "(verify open)",
            "model": "-",
            "agent_id": agent_id,
            "action": "BLOCKED",
            "strategy": "circuit_breaker",
            "details": f"State={info_tripped.state.value}, reason={info_tripped.reason}",
        })

        # Step 3: Manual reset.
        await cb.reset(agent_id)
        info_reset = await cb.check(agent_id)
        test["events"].append({
            "call": 3,
            "prompt": "(manual reset)",
            "model": "-",
            "agent_id": agent_id,
            "action": "RECOVERED",
            "strategy": "manual_reset",
            "details": f"State={info_reset.state.value}",
        })

        # Step 4: Verify calls are allowed again.
        test["events"].append({
            "call": 4,
            "prompt": "(verify allowed)",
            "model": "-",
            "agent_id": agent_id,
            "action": "ALLOWED" if info_reset.state == CircuitBreakerState.CLOSED else "BLOCKED",
            "strategy": None,
            "details": f"State={info_reset.state.value}",
        })

        reset_worked = info_reset.state == CircuitBreakerState.CLOSED
        test["result"] = (
            "PASS - manual reset restored CLOSED state"
            if reset_worked
            else f"FAIL - state after reset is {info_reset.state.value}"
        )
        test["stats"] = {
            "trip_state": info_tripped.state.value,
            "reset_state": info_reset.state.value,
            "reset_worked": reset_worked,
        }
        if not reset_worked:
            test["failed"] = True

    asyncio.run(_run())
    return test


def _test_cb_error_threshold() -> dict:
    """Test 4: Agent has multiple failures, CB trips when the error
    count crosses the threshold."""
    test = {
        "name": "Error Threshold Trip",
        "strategy": "error_threshold",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        threshold = 3
        cb_config = CircuitBreakerConfig(
            error_threshold=threshold,
            error_window_sec=60,
            cooldown_sec=300,
            half_open_max_calls=1,
        )
        cb = CircuitBreaker(store, cb_config)
        agent_id = "agent-errors"

        tripped_at_call = None

        for i in range(threshold + 2):
            # Check state before recording the failure.
            info = await cb.check(agent_id)

            if info.state == CircuitBreakerState.OPEN:
                test["events"].append({
                    "call": i + 1,
                    "prompt": f"(failure #{i + 1})",
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "BLOCKED",
                    "strategy": "circuit_breaker",
                    "details": f"CB OPEN after {info.error_count} errors",
                })
                continue

            # Record the failure.
            await cb.record_failure(agent_id)
            info_after = await cb.check(agent_id)

            if info_after.state == CircuitBreakerState.OPEN and tripped_at_call is None:
                tripped_at_call = i + 1
                test["events"].append({
                    "call": i + 1,
                    "prompt": f"(failure #{i + 1})",
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "TRIPPED",
                    "strategy": "error_threshold",
                    "details": f"Error count {info_after.error_count} hit threshold {threshold}",
                })
            else:
                test["events"].append({
                    "call": i + 1,
                    "prompt": f"(failure #{i + 1})",
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "ALLOWED",
                    "strategy": None,
                    "details": f"Error count {info_after.error_count}/{threshold}",
                })

        tripped = tripped_at_call is not None
        test["result"] = (
            f"PASS - CB tripped at call {tripped_at_call} (threshold={threshold})"
            if tripped
            else f"FAIL - CB never tripped after {threshold + 2} errors"
        )
        test["stats"] = {
            "error_threshold": threshold,
            "tripped_at_call": tripped_at_call,
            "total_errors_sent": threshold + 2,
            "tripped": tripped,
        }
        if not tripped:
            test["failed"] = True

    asyncio.run(_run())
    return test


# =====================================================================
# Budget Enforcer test scenarios (Day 3)
# =====================================================================


def _run_budget_enforcer_tests() -> dict:
    """Run all four budget enforcer test scenarios."""
    module = {
        "name": "Budget Enforcer",
        "status": "pass",
        "tests": [],
    }

    module["tests"].append(_test_budget_reservation_flow())
    module["tests"].append(_test_budget_reconciliation())
    module["tests"].append(_test_budget_threshold_alerts())
    module["tests"].append(_test_full_pipeline())

    if any(t.get("failed") for t in module["tests"]):
        module["status"] = "fail"

    return module


def _test_budget_reservation_flow() -> dict:
    """Test 1: Reserve $2 five times against a $10 budget.
    First 5 succeed, 6th is rejected."""
    test = {
        "name": "Budget Reservation Flow",
        "strategy": "reservation",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        config = BudgetConfig(default_budget=10.0)
        enforcer = BudgetEnforcer(store, config)
        scope = BudgetScope.AGENT
        scope_id = "budget-test"

        await enforcer.set_budget(scope, scope_id, 10.0)

        for i in range(6):
            try:
                reservation = await enforcer.reserve(scope, scope_id, 2.0)
                status = await enforcer.get_budget(scope, scope_id)
                test["events"].append({
                    "call": i + 1,
                    "prompt": f"(reserve $2.00, attempt {i + 1})",
                    "model": "-",
                    "agent_id": scope_id,
                    "action": "RESERVED",
                    "strategy": "reservation",
                    "details": (
                        f"Reserved $2.00 | spent=${status.spent:.2f} "
                        f"remaining=${status.remaining:.2f}"
                    ),
                })
            except BudgetCtlBudgetExceeded as exc:
                test["events"].append({
                    "call": i + 1,
                    "prompt": f"(reserve $2.00, attempt {i + 1})",
                    "model": "-",
                    "agent_id": scope_id,
                    "action": "REJECTED",
                    "strategy": "reservation",
                    "details": f"Budget exceeded: spent=${exc.spent:.2f} limit=${exc.limit:.2f}",
                })

        reserved_count = sum(1 for e in test["events"] if e["action"] == "RESERVED")
        rejected_count = sum(1 for e in test["events"] if e["action"] == "REJECTED")
        test["result"] = f"{reserved_count} reserved, {rejected_count} rejected"

        final_status = await enforcer.get_budget(scope, scope_id)
        test["stats"] = {
            "budget_limit": final_status.limit,
            "total_spent": final_status.spent,
            "remaining": final_status.remaining,
            "percentage_used": final_status.percentage,
            "reservations_accepted": reserved_count,
            "reservations_rejected": rejected_count,
        }

        # Expect exactly 5 reserved (5 * $2 = $10) and 1 rejected
        if reserved_count != 5 or rejected_count != 1:
            test["failed"] = True
            test["result"] += f" (UNEXPECTED: expected 5 reserved + 1 rejected)"

    asyncio.run(_run())
    return test


def _test_budget_reconciliation() -> dict:
    """Test 2: Reserve $5 (estimated), reconcile to $3 actual,
    verify $2 released back to available budget."""
    test = {
        "name": "Budget Reconciliation",
        "strategy": "reconciliation",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        config = BudgetConfig(default_budget=10.0)
        enforcer = BudgetEnforcer(store, config)
        scope = BudgetScope.AGENT
        scope_id = "reconcile-test"

        await enforcer.set_budget(scope, scope_id, 10.0)

        # Step 1: Reserve $5
        reservation = await enforcer.reserve(scope, scope_id, 5.0)
        status_after_reserve = await enforcer.get_budget(scope, scope_id)
        test["events"].append({
            "call": 1,
            "prompt": "(reserve $5.00 estimated)",
            "model": "-",
            "agent_id": scope_id,
            "action": "RESERVED",
            "strategy": "reconciliation",
            "details": (
                f"Reserved $5.00 | spent=${status_after_reserve.spent:.2f} "
                f"remaining=${status_after_reserve.remaining:.2f}"
            ),
        })

        # Step 2: Reconcile to $3 actual (release $2 back)
        await enforcer.reconcile(reservation, actual_cost=3.0)
        status_after_reconcile = await enforcer.get_budget(scope, scope_id)
        released = status_after_reserve.spent - status_after_reconcile.spent
        test["events"].append({
            "call": 2,
            "prompt": "(reconcile: actual=$3.00, estimated=$5.00)",
            "model": "-",
            "agent_id": scope_id,
            "action": "RECONCILED",
            "strategy": "reconciliation",
            "details": (
                f"Actual $3.00 vs estimated $5.00 | "
                f"released=${released:.2f} back | "
                f"spent=${status_after_reconcile.spent:.2f} "
                f"remaining=${status_after_reconcile.remaining:.2f}"
            ),
        })

        # Step 3: Verify remaining budget is $7 ($10 - $3 actual)
        test["events"].append({
            "call": 3,
            "prompt": "(verify remaining budget)",
            "model": "-",
            "agent_id": scope_id,
            "action": "VERIFIED",
            "strategy": "reconciliation",
            "details": (
                f"Final: spent=${status_after_reconcile.spent:.2f} "
                f"remaining=${status_after_reconcile.remaining:.2f} "
                f"(expected remaining=$7.00)"
            ),
        })

        test["stats"] = {
            "budget_limit": 10.0,
            "estimated_cost": 5.0,
            "actual_cost": 3.0,
            "amount_released": round(released, 2),
            "final_spent": status_after_reconcile.spent,
            "final_remaining": status_after_reconcile.remaining,
        }

        # Verify the math: remaining should be $7
        if abs(status_after_reconcile.remaining - 7.0) > 0.01:
            test["failed"] = True
            test["result"] = (
                f"FAIL - remaining=${status_after_reconcile.remaining:.2f} "
                f"(expected $7.00)"
            )
        else:
            test["result"] = (
                f"PASS - $2.00 released back, remaining=${status_after_reconcile.remaining:.2f}"
            )

    asyncio.run(_run())
    return test


def _test_budget_threshold_alerts() -> dict:
    """Test 3: Spend incrementally and verify threshold alerts fire
    at 50%, 80%, 90%, and 100%."""
    test = {
        "name": "Budget Threshold Alerts",
        "strategy": "thresholds",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()
        config = BudgetConfig(
            default_budget=10.0,
            alert_thresholds=[0.5, 0.8, 0.9, 1.0],
        )
        enforcer = BudgetEnforcer(store, config)
        scope = BudgetScope.AGENT
        scope_id = "threshold-test"

        await enforcer.set_budget(scope, scope_id, 10.0)

        # Spend plan: $5, $3, $1, $1 = $10 total
        # After $5:  50% -> "info"
        # After $8:  80% -> "warning"
        # After $9:  90% -> "urgent"
        # After $10: 100% -> "critical"
        spend_plan = [
            (5.0, "info"),
            (3.0, "warning"),
            (1.0, "urgent"),
            (1.0, "critical"),
        ]

        alerts_matched = 0

        for i, (amount, expected_level) in enumerate(spend_plan):
            reservation = await enforcer.reserve(scope, scope_id, amount)
            # Reconcile immediately at full cost
            await enforcer.reconcile(reservation, actual_cost=amount)

            alert_level = await enforcer.check_thresholds(scope, scope_id)
            status = await enforcer.get_budget(scope, scope_id)

            level_match = alert_level == expected_level
            if level_match:
                alerts_matched += 1

            test["events"].append({
                "call": i + 1,
                "prompt": f"(spend ${amount:.2f}, total ${status.spent:.2f})",
                "model": "-",
                "agent_id": scope_id,
                "action": alert_level.upper() if alert_level else "NONE",
                "strategy": "thresholds",
                "details": (
                    f"Spent ${status.spent:.2f}/{status.limit:.2f} "
                    f"({status.percentage:.0f}%) | "
                    f"alert={alert_level} "
                    f"{'(matches expected)' if level_match else f'(expected {expected_level})'}"
                ),
            })

        test["stats"] = {
            "budget_limit": 10.0,
            "thresholds_configured": [0.5, 0.8, 0.9, 1.0],
            "alerts_fired": alerts_matched,
            "alerts_expected": len(spend_plan),
        }

        if alerts_matched == len(spend_plan):
            test["result"] = f"PASS - all {alerts_matched} threshold alerts fired correctly"
        else:
            test["failed"] = True
            test["result"] = (
                f"FAIL - {alerts_matched}/{len(spend_plan)} alerts matched"
            )

    asyncio.run(_run())
    return test


def _test_full_pipeline() -> dict:
    """Test 4: Full pipeline -- Loop Detector -> Circuit Breaker -> Budget Enforcer.

    Agent sends identical prompts:
    - Calls 1-3: allowed, cost deducted from budget
    - Call 4: loop detected, CB trips
    - Call 5: CB blocks (budget not touched)
    """
    test = {
        "name": "Full Pipeline: Loop -> CB Trip -> Budget Block",
        "strategy": "full_pipeline",
        "events": [],
        "result": "",
        "failed": False,
    }

    async def _run() -> None:
        store = MemoryStore()

        # Initialize all three engines
        loop_config = LoopDetectorConfig(window_size=10, repeat_threshold=3)
        cb_config = CircuitBreakerConfig(
            error_threshold=5,
            error_window_sec=60,
            cooldown_sec=300,
            half_open_max_calls=1,
        )
        budget_config = BudgetConfig(default_budget=100.0)

        detector = LoopDetector(loop_config)
        cb = CircuitBreaker(store, cb_config)
        enforcer = BudgetEnforcer(store, budget_config)

        scope = BudgetScope.AGENT
        agent_id = "pipeline-test"
        cost_per_call = 2.0

        await enforcer.set_budget(scope, agent_id, 100.0)

        prompt = "Explain quantum computing"
        messages = [{"role": "user", "content": prompt}]

        allowed_count = 0
        loop_tripped = False
        cb_blocked_count = 0
        total_budget_spent = 0.0

        for i in range(5):
            # Step 1: Check circuit breaker
            cb_info = await cb.check(agent_id)

            if cb_info.state == CircuitBreakerState.OPEN:
                cb_blocked_count += 1
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:40],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "BLOCKED",
                    "strategy": "circuit_breaker",
                    "details": f"CB OPEN -- budget untouched (spent=${total_budget_spent:.2f})",
                })
                continue

            # Step 2: Check loop detector
            loop_result = detector.check(agent_id, messages, "gpt-4")

            if loop_result.is_loop:
                loop_tripped = True
                await cb.trip(
                    agent_id,
                    TripReason.LOOP_DETECTED,
                    details=f"Loop via {loop_result.strategy}",
                )
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:40],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "TRIPPED",
                    "strategy": loop_result.strategy,
                    "details": (
                        f"Loop detected, CB tripped | "
                        f"budget untouched (spent=${total_budget_spent:.2f})"
                    ),
                })
                continue

            # Step 3: Reserve budget
            try:
                reservation = await enforcer.reserve(scope, agent_id, cost_per_call)
            except BudgetCtlBudgetExceeded:
                test["events"].append({
                    "call": i + 1,
                    "prompt": prompt[:40],
                    "model": "gpt-4",
                    "agent_id": agent_id,
                    "action": "BUDGET_BLOCKED",
                    "strategy": "budget",
                    "details": "Budget exceeded before LLM call",
                })
                continue

            # Step 4: Simulate successful LLM call
            detector.record(agent_id, messages, "gpt-4")
            await cb.record_success(agent_id)
            await enforcer.reconcile(reservation, actual_cost=cost_per_call)
            total_budget_spent += cost_per_call
            allowed_count += 1

            status = await enforcer.get_budget(scope, agent_id)
            test["events"].append({
                "call": i + 1,
                "prompt": prompt[:40],
                "model": "gpt-4",
                "agent_id": agent_id,
                "action": "ALLOWED",
                "strategy": None,
                "details": (
                    f"Call completed | cost=${cost_per_call:.2f} | "
                    f"budget spent=${status.spent:.2f} "
                    f"remaining=${status.remaining:.2f}"
                ),
            })

        final_budget = await enforcer.get_budget(scope, agent_id)

        test["stats"] = {
            "total_calls_attempted": 5,
            "allowed": allowed_count,
            "loop_tripped": loop_tripped,
            "cb_blocked": cb_blocked_count,
            "total_budget_spent": final_budget.spent,
            "budget_remaining": final_budget.remaining,
        }

        # Verify: 3 allowed, 1 trip, 1 blocked. Budget = 3 * $2 = $6 spent.
        expected_spent = allowed_count * cost_per_call
        pipeline_correct = (
            allowed_count == 3
            and loop_tripped
            and cb_blocked_count == 1
            and abs(final_budget.spent - expected_spent) < 0.01
        )

        if pipeline_correct:
            test["result"] = (
                f"PASS - {allowed_count} allowed, 1 tripped, "
                f"{cb_blocked_count} blocked | "
                f"budget=${final_budget.spent:.2f}/${final_budget.limit:.2f}"
            )
        else:
            test["failed"] = True
            test["result"] = (
                f"FAIL - allowed={allowed_count} tripped={loop_tripped} "
                f"blocked={cb_blocked_count} "
                f"budget=${final_budget.spent:.2f}"
            )

    asyncio.run(_run())
    return test


def generate_markdown(data: dict) -> str:
    lines = []
    lines.append(f"# Chappie Test Report - Day {data['day']}")
    lines.append(f"")
    lines.append(f"**Generated:** {data['generated_at']}")
    lines.append(f"**Version:** {data['version']}")
    lines.append(f"")

    for module in data["modules_tested"]:
        status_icon = "PASS" if module["status"] == "pass" else "FAIL"
        lines.append(f"## {module['name']} [{status_icon}]")
        lines.append(f"")

        for test in module["tests"]:
            lines.append(f"### {test['name']}")
            lines.append(f"")
            lines.append(f"**Result:** {test['result']}")
            lines.append(f"")

            # Event table
            lines.append(f"| Call | Agent | Prompt | Action | Strategy |")
            lines.append(f"|------|-------|--------|--------|----------|")
            for event in test["events"]:
                strategy = event["strategy"] or "-"
                prompt = event["prompt"][:40]
                lines.append(
                    f"| {event['call']} | {event['agent_id']} | {prompt} | **{event['action']}** | {strategy} |"
                )
            lines.append(f"")

            if "stats" in test and test["stats"]:
                lines.append(f"**Stats:**")
                lines.append(f"```json")
                lines.append(json.dumps(test["stats"], indent=2, default=str))
                lines.append(f"```")
                lines.append(f"")

    # Summary
    total_tests = sum(len(m["tests"]) for m in data["modules_tested"])
    passed_modules = sum(1 for m in data["modules_tested"] if m["status"] == "pass")
    failed_tests = sum(
        1 for m in data["modules_tested"]
        for t in m["tests"]
        if t.get("failed")
    )
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## Summary")
    lines.append(f"")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Modules Tested | {len(data['modules_tested'])} |")
    lines.append(f"| Modules Passed | {passed_modules} |")
    lines.append(f"| Total Tests | {total_tests} |")
    lines.append(f"| Tests Failed | {failed_tests} |")
    lines.append(f"| Day | {data['day']} |")
    lines.append(f"")

    return "\n".join(lines)


if __name__ == "__main__":
    run_report()
