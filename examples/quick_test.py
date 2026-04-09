"""
Quick test - see BudgetCtl's loop detector in action.
No LLM calls, no API key needed. Just run it.
"""
from budgetctl.engine.loop_detector import LoopDetector
from budgetctl.config import LoopDetectorConfig

print("=== BudgetCtl Loop Detector Demo ===\n")
print("Strategy A: Hash Dedup (same prompt repeated)\n")

detector = LoopDetector(LoopDetectorConfig(
    window_size=10,
    repeat_threshold=3,
))

# Simulate an agent stuck repeating the same prompt
# record() adds to history, check() reads history to detect
prompts = [
    "What is the weather in Tokyo?",
    "What is the weather in Tokyo?",
    "What is the weather in Tokyo?",
    "What is the weather in Tokyo?",   # 4th attempt - should be caught (3 already recorded)
    "Tell me about Python",            # different prompt, resets
]

for i, prompt in enumerate(prompts):
    messages = [{"role": "user", "content": prompt}]
    result = detector.check("demo-agent", messages, "gpt-4")

    if result.is_loop:
        print(f"  Call {i+1}: BLOCKED  '{prompt[:40]}' -> {result.strategy}")
    else:
        print(f"  Call {i+1}: ALLOWED  '{prompt[:40]}'")
        detector.record("demo-agent", messages, "gpt-4")

print(f"\n  Stats: {detector.get_stats('demo-agent')}")

print("\n---")
print("\nStrategy B: Cycle Detection (A-B-A-B pattern)\n")

detector2 = LoopDetector(LoopDetectorConfig(
    window_size=20,
    repeat_threshold=100,  # disable dedup so cycle detection fires
    cycle_max_period=4,
))

cycle_prompts = [
    "Summarize the document",
    "Translate to Spanish",
    "Summarize the document",
    "Translate to Spanish",
    "Summarize the document",
    "Translate to Spanish",
    "Summarize the document",  # 7th call - cycle should be caught
]

for i, prompt in enumerate(cycle_prompts):
    messages = [{"role": "user", "content": prompt}]
    result = detector2.check("cycle-agent", messages, "gpt-4")

    if result.is_loop:
        print(f"  Call {i+1}: BLOCKED  '{prompt[:40]}' -> {result.strategy}")
    else:
        print(f"  Call {i+1}: ALLOWED  '{prompt[:40]}'")
        detector2.record("cycle-agent", messages, "gpt-4")

print("\nDone. Chappie is working.\n")
