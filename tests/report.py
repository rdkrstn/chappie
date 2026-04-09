"""
Test report generator for Chappie.
Runs all demos and captures results into a structured .md file
that can be rendered in an HTML data visualizer.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from chappie.engine.loop_detector import LoopDetector
from chappie.config import LoopDetectorConfig


def run_report():
    report_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "day": 1,
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

    # ===== Generate Markdown =====
    md = generate_markdown(report_data)
    report_path = Path(__file__).parent.parent / "docs"
    report_path.mkdir(exist_ok=True)
    report_file = report_path / "test-report-day1.md"
    report_file.write_text(md, encoding="utf-8")

    # Also save raw JSON for the HTML visualizer
    json_file = report_path / "test-report-day1.json"
    json_file.write_text(json.dumps(report_data, indent=2, default=str), encoding="utf-8")

    print(f"Report saved to: {report_file}")
    print(f"JSON data saved to: {json_file}")
    return report_data


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
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## Summary")
    lines.append(f"")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Modules Tested | {len(data['modules_tested'])} |")
    lines.append(f"| Modules Passed | {passed_modules} |")
    lines.append(f"| Total Tests | {total_tests} |")
    lines.append(f"| Day | {data['day']} |")
    lines.append(f"")

    return "\n".join(lines)


if __name__ == "__main__":
    run_report()
