"""
Chappie Full Pipeline Demo
===========================

No API keys needed. No LLM calls. Shows the complete flow:
1. Agent makes normal calls (allowed)
2. Agent starts repeating (loop detected)
3. Circuit breaker trips (agent blocked)
4. Budget tracks all costs
5. Cooldown expires, agent recovers

Run:
    python -m examples.loop_demo
    # or from the project root:
    python examples/loop_demo.py
"""

import asyncio
import time

from chappie.config import BudgetConfig, CircuitBreakerConfig, LoopDetectorConfig
from chappie.engine.budget_enforcer import BudgetEnforcer, BudgetScope
from chappie.engine.circuit_breaker import CircuitBreaker, TripReason
from chappie.engine.loop_detector import LoopDetector
from chappie.exceptions import ChappieBudgetExceeded
from chappie.models import CircuitBreakerState
from chappie.store.memory import MemoryStore


DIVIDER = "-" * 60
AGENT_ID = "demo-agent"
COST_PER_CALL = 1.50


def banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def main() -> None:
    # ------------------------------------------------------------------
    # Setup: create all three engines with a shared MemoryStore
    # ------------------------------------------------------------------
    store = MemoryStore()

    detector = LoopDetector(LoopDetectorConfig(
        window_size=10,
        repeat_threshold=3,
    ))

    cb = CircuitBreaker(store, CircuitBreakerConfig(
        error_threshold=5,
        error_window_sec=60,
        cooldown_sec=2,          # short cooldown for the demo
        half_open_max_calls=1,
    ))

    enforcer = BudgetEnforcer(store, BudgetConfig(
        default_budget=20.0,
    ))

    scope = BudgetScope.AGENT
    await enforcer.set_budget(scope, AGENT_ID, 20.0)

    banner("CHAPPIE FULL PIPELINE DEMO")
    print("  Agent budget:    $20.00")
    print(f"  Cost per call:   ${COST_PER_CALL:.2f}")
    print("  Loop threshold:  3 identical prompts")
    print("  CB cooldown:     2 seconds")
    print()

    # ------------------------------------------------------------------
    # Phase 1: Normal operation -- varied prompts, budget deducted
    # ------------------------------------------------------------------
    banner("PHASE 1: Normal Operation (varied prompts)")

    varied_prompts = [
        "What is machine learning?",
        "Explain neural networks",
        "How does backpropagation work?",
    ]

    for i, prompt in enumerate(varied_prompts):
        messages = [{"role": "user", "content": prompt}]

        # Check circuit breaker
        cb_info = await cb.check(AGENT_ID)
        if cb_info.state == CircuitBreakerState.OPEN:
            print(f"  [{i + 1}] BLOCKED by circuit breaker")
            continue

        # Check loop detector
        loop_result = detector.check(AGENT_ID, messages, "gpt-4")
        if loop_result.is_loop:
            print(f"  [{i + 1}] BLOCKED by loop detector: {loop_result.strategy}")
            continue

        # Reserve budget
        try:
            reservation = await enforcer.reserve(scope, AGENT_ID, COST_PER_CALL)
        except ChappieBudgetExceeded as exc:
            print(f"  [{i + 1}] BLOCKED by budget: spent=${exc.spent:.2f} limit=${exc.limit:.2f}")
            continue

        # Simulate LLM call (no actual API call)
        detector.record(AGENT_ID, messages, "gpt-4")
        await cb.record_success(AGENT_ID)
        await enforcer.reconcile(reservation, actual_cost=COST_PER_CALL)

        status = await enforcer.get_budget(scope, AGENT_ID)
        print(
            f"  [{i + 1}] ALLOWED  \"{prompt[:40]}\"\n"
            f"           Cost: ${COST_PER_CALL:.2f}  |  "
            f"Budget: ${status.spent:.2f} / ${status.limit:.2f}  |  "
            f"Remaining: ${status.remaining:.2f}"
        )

    budget_after_p1 = await enforcer.get_budget(scope, AGENT_ID)
    print(f"\n  Phase 1 complete. Budget spent: ${budget_after_p1.spent:.2f}")

    # ------------------------------------------------------------------
    # Phase 2: Agent starts looping -- same prompt repeated
    # ------------------------------------------------------------------
    banner("PHASE 2: Loop Detection (repeated prompt)")

    repeated_prompt = "What is machine learning?"
    messages = [{"role": "user", "content": repeated_prompt}]

    for i in range(4):
        call_num = len(varied_prompts) + i + 1

        # Check circuit breaker
        cb_info = await cb.check(AGENT_ID)
        if cb_info.state == CircuitBreakerState.OPEN:
            print(
                f"  [{call_num}] BLOCKED  Circuit breaker is OPEN\n"
                f"           Budget NOT touched  |  "
                f"Remaining: ${(await enforcer.get_budget(scope, AGENT_ID)).remaining:.2f}"
            )
            continue

        # Check loop detector
        loop_result = detector.check(AGENT_ID, messages, "gpt-4")

        if loop_result.is_loop:
            # Trip the circuit breaker
            await cb.trip(
                AGENT_ID,
                TripReason.LOOP_DETECTED,
                details=f"Loop via {loop_result.strategy}",
            )
            print(
                f"  [{call_num}] TRIPPED  Loop detected ({loop_result.strategy})\n"
                f"           Circuit breaker -> OPEN\n"
                f"           Budget NOT touched  |  "
                f"Remaining: ${(await enforcer.get_budget(scope, AGENT_ID)).remaining:.2f}"
            )
            continue

        # Reserve budget and simulate call
        try:
            reservation = await enforcer.reserve(scope, AGENT_ID, COST_PER_CALL)
        except ChappieBudgetExceeded as exc:
            print(f"  [{call_num}] BLOCKED  Budget exceeded")
            continue

        detector.record(AGENT_ID, messages, "gpt-4")
        await cb.record_success(AGENT_ID)
        await enforcer.reconcile(reservation, actual_cost=COST_PER_CALL)

        status = await enforcer.get_budget(scope, AGENT_ID)
        print(
            f"  [{call_num}] ALLOWED  \"{repeated_prompt[:40]}\"\n"
            f"           Cost: ${COST_PER_CALL:.2f}  |  "
            f"Budget: ${status.spent:.2f} / ${status.limit:.2f}  |  "
            f"Remaining: ${status.remaining:.2f}"
        )

    budget_after_p2 = await enforcer.get_budget(scope, AGENT_ID)
    cb_state = await cb.check(AGENT_ID)
    print(f"\n  Phase 2 complete.")
    print(f"  CB state: {cb_state.state.value}")
    print(f"  Budget spent: ${budget_after_p2.spent:.2f}")

    # ------------------------------------------------------------------
    # Phase 3: Circuit breaker blocks everything
    # ------------------------------------------------------------------
    banner("PHASE 3: Circuit Breaker Blocking")

    for i in range(2):
        call_num = len(varied_prompts) + 4 + i + 1

        cb_info = await cb.check(AGENT_ID)
        if cb_info.state == CircuitBreakerState.OPEN:
            budget_status = await enforcer.get_budget(scope, AGENT_ID)
            print(
                f"  [{call_num}] BLOCKED  Circuit breaker OPEN (reason: {cb_info.reason})\n"
                f"           No budget consumed  |  "
                f"Remaining: ${budget_status.remaining:.2f}"
            )
        else:
            print(f"  [{call_num}] Unexpected: CB not open (state={cb_info.state.value})")

    budget_after_p3 = await enforcer.get_budget(scope, AGENT_ID)
    print(f"\n  Phase 3 complete. Budget unchanged: ${budget_after_p3.spent:.2f}")

    # ------------------------------------------------------------------
    # Phase 4: Cooldown expires, agent recovers
    # ------------------------------------------------------------------
    banner("PHASE 4: Recovery After Cooldown")

    print("  Waiting for CB cooldown (2 seconds)...")
    await asyncio.sleep(2.5)

    # Check CB state -- should be HALF_OPEN now
    cb_info = await cb.check(AGENT_ID)
    print(f"  CB state after cooldown: {cb_info.state.value}")

    # Send a new (different) prompt to prove recovery
    recovery_prompt = "What is quantum entanglement?"
    messages = [{"role": "user", "content": recovery_prompt}]

    # Clear the loop detector session so old hashes do not interfere
    detector.clear_session(AGENT_ID)

    loop_result = detector.check(AGENT_ID, messages, "gpt-4")
    if not loop_result.is_loop and cb_info.state != CircuitBreakerState.OPEN:
        try:
            reservation = await enforcer.reserve(scope, AGENT_ID, COST_PER_CALL)
        except ChappieBudgetExceeded:
            print("  Recovery call blocked by budget")
        else:
            detector.record(AGENT_ID, messages, "gpt-4")
            await cb.record_success(AGENT_ID)
            await enforcer.reconcile(reservation, actual_cost=COST_PER_CALL)

            cb_after = await cb.check(AGENT_ID)
            status = await enforcer.get_budget(scope, AGENT_ID)
            print(
                f"  Recovery call ALLOWED  \"{recovery_prompt[:40]}\"\n"
                f"  CB state: {cb_after.state.value} (recovered)\n"
                f"  Budget: ${status.spent:.2f} / ${status.limit:.2f}  |  "
                f"Remaining: ${status.remaining:.2f}"
            )
    else:
        print(f"  Recovery blocked: loop={loop_result.is_loop} cb={cb_info.state.value}")

    # ------------------------------------------------------------------
    # Phase 5: Budget reconciliation demo
    # ------------------------------------------------------------------
    banner("PHASE 5: Budget Reconciliation")

    recon_prompt = "Explain string theory"
    messages = [{"role": "user", "content": recon_prompt}]

    loop_result = detector.check(AGENT_ID, messages, "gpt-4")
    if not loop_result.is_loop:
        estimated = 3.00
        actual = 1.25

        reservation = await enforcer.reserve(scope, AGENT_ID, estimated)
        status_reserved = await enforcer.get_budget(scope, AGENT_ID)
        print(f"  Reserved ${estimated:.2f} (estimated)")
        print(f"  Budget after reserve: spent=${status_reserved.spent:.2f}")

        # Simulate LLM call returning cheaper than expected
        detector.record(AGENT_ID, messages, "gpt-4")
        await cb.record_success(AGENT_ID)
        await enforcer.reconcile(reservation, actual_cost=actual)

        status_reconciled = await enforcer.get_budget(scope, AGENT_ID)
        released = estimated - actual
        print(f"  Actual cost: ${actual:.2f} (cheaper than estimated)")
        print(f"  Released: ${released:.2f} back to budget")
        print(
            f"  Budget after reconcile: spent=${status_reconciled.spent:.2f}  |  "
            f"Remaining: ${status_reconciled.remaining:.2f}"
        )

    # ------------------------------------------------------------------
    # Phase 6: Threshold alerts
    # ------------------------------------------------------------------
    banner("PHASE 6: Threshold Alerts")

    # Set up a fresh budget scope to show thresholds cleanly
    alert_agent = "alert-demo"
    await enforcer.set_budget(scope, alert_agent, 10.0)
    detector_alert = LoopDetector(LoopDetectorConfig(
        window_size=20, repeat_threshold=100,
    ))

    alert_spend_plan = [
        (5.0, "50%"),
        (3.0, "80%"),
        (1.0, "90%"),
        (1.0, "100%"),
    ]

    for amount, label in alert_spend_plan:
        prompt = f"Spend ${amount:.2f} call"
        messages = [{"role": "user", "content": prompt}]
        detector_alert.record(alert_agent, messages, "gpt-4")

        reservation = await enforcer.reserve(scope, alert_agent, amount)
        await enforcer.reconcile(reservation, actual_cost=amount)
        alert_level = await enforcer.check_thresholds(scope, alert_agent)
        status = await enforcer.get_budget(scope, alert_agent)

        alert_display = alert_level.upper() if alert_level else "none"
        print(
            f"  Spent ${amount:.2f} -> total ${status.spent:.2f}/{status.limit:.2f} "
            f"({label}) -> alert: {alert_display}"
        )

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    banner("FINAL SUMMARY")

    final = await enforcer.get_budget(scope, AGENT_ID)
    print(f"  Agent: {AGENT_ID}")
    print(f"  Total spent:     ${final.spent:.2f}")
    print(f"  Budget limit:    ${final.limit:.2f}")
    print(f"  Remaining:       ${final.remaining:.2f}")
    print(f"  Usage:           {final.percentage:.1f}%")

    cb_final = await cb.check(AGENT_ID)
    print(f"  Circuit breaker: {cb_final.state.value}")
    print()
    print("  Chappie protected this agent from:")
    print("    - Infinite loops (loop detector)")
    print("    - Cascading failures (circuit breaker)")
    print("    - Runaway costs (budget enforcer)")
    print()
    print(DIVIDER)
    print("  Demo complete. No LLM calls were made.")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(main())
