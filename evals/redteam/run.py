"""Red-team eval: run the input guardrail over evals/redteam/cases.jsonl and report catch / false-positive rate.

    python -m evals.redteam.run            # report
    python -m evals.redteam.run --gate     # exit 1 if below thresholds.yaml (used in CI)

OWNER: Thrishal. Grow cases.jsonl to ~50 attacks + ~50 benign across categories
(direct injection, indirect/document injection, jailbreak personas, encoding tricks, multilingual).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from app.guardrails.input import classify_input

HERE = Path(__file__).parent


def load_cases() -> list[dict]:
    return [
        json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line.strip()
    ]


def run() -> dict:
    cases = load_cases()
    rows, latencies = [], []
    for c in cases:
        t0 = time.perf_counter()
        v = classify_input(c["text"], source=c.get("source", "instructions"))
        latencies.append((time.perf_counter() - t0) * 1000)
        rows.append({**c, "blocked": v.blocked, "pred_category": v.category, "method": v.method})
    attacks = [r for r in rows if r["label"] == "attack"]
    benign = [r for r in rows if r["label"] == "benign"]
    catch = sum(r["blocked"] for r in attacks) / max(1, len(attacks))
    fpr = sum(r["blocked"] for r in benign) / max(1, len(benign))
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] if latencies else 0
    return {
        "n_attack": len(attacks),
        "n_benign": len(benign),
        "catch_rate": round(catch, 3),
        "false_positive_rate": round(fpr, 3),
        "latency_ms_p50": round(p50, 3),
        "latency_ms_p99": round(p99, 3),
        "missed": [r["id"] for r in attacks if not r["blocked"]],
        "false_positives": [r["id"] for r in benign if r["blocked"]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"red-team eval: {report['n_attack']} attacks, {report['n_benign']} benign")
        print(f"  catch rate          : {report['catch_rate']:.1%}   missed: {report['missed']}")
        print(
            f"  false positive rate : {report['false_positive_rate']:.1%}   fps: {report['false_positives']}"
        )
        print(
            f"  classifier latency  : p50 {report['latency_ms_p50']} ms, p99 {report['latency_ms_p99']} ms"
        )
    if args.gate:
        th = yaml.safe_load((HERE / "thresholds.yaml").read_text())
        ok = (
            report["catch_rate"] >= th["min_catch_rate"]
            and report["false_positive_rate"] <= th["max_false_positive_rate"]
        )
        print(
            f"gate: {'PASS' if ok else 'FAIL'} (min catch {th['min_catch_rate']}, max fpr {th['max_false_positive_rate']})"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
