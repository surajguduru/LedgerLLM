"""Red-team eval: run the input guardrail over evals/redteam/cases.jsonl and report catch / false-positive rate.

    python -m evals.redteam.run                 # heuristics only (GUARDRAIL_LLM=off): free, what CI runs
    python -m evals.redteam.run --gate          # exit 1 if below thresholds.yaml
    python -m evals.redteam.run --llm on        # cascade: needs LLM_PROVIDER + LLM_API_KEY; paced for the free tier
    python -m evals.redteam.run --json          # machine-readable report

The report states which configuration it measured. The CI gate runs heuristics only, so the thresholds
in thresholds.yaml are the floor for the free path; the cascade numbers are recorded in docs/MEASUREMENTS.md.

Cases: 50 attacks (direct 15 · indirect/document 15 · persona 8 · obfuscation 6 · multilingual 6) and 50
benign (25 look-alikes with trigger words in ordinary context, 10 ordinary instructions, 15 ordinary
documents). A benign case may carry a `note` explaining why it is hard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).parent

# Slice an attack id belongs to, by position in cases.jsonl (see the generator comments in the file).
ATTACK_SLICES = {
    "direct": range(1, 16),
    "indirect": range(16, 31),
    "persona": range(31, 39),
    "obfuscation": range(39, 45),
    "multilingual": range(45, 51),
}


def load_cases() -> list[dict]:
    return [
        json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line.strip()
    ]


def _slice(case: dict) -> str:
    if case["label"] == "benign":
        return "benign"
    n = int(case["id"].split("-")[1])
    for name, rng in ATTACK_SLICES.items():
        if n in rng:
            return name
    return "other"


def _pct(hits: int, n: int) -> float:
    return round(hits / n, 3) if n else 0.0


def run(*, pace_s: float = 0.0) -> dict:
    from app.billing.pricing import compute_cost_microusd
    from app.config import get_settings
    from app.guardrails import llm_classifier
    from app.guardrails.input import classify_input
    from app.plans import microusd_to_usd

    settings = get_settings()
    llm_on = llm_classifier.enabled()
    llm_classifier.clear_cache()  # measure the model, not a warm cache

    cases = load_cases()
    rows, latencies = [], []
    llm_calls, llm_tokens_in, llm_tokens_out, llm_cost = 0, 0, 0, 0
    for c in cases:
        t0 = time.perf_counter()
        v = classify_input(c["text"], source=c.get("source", "instructions"))
        latencies.append((time.perf_counter() - t0) * 1000)
        if v.model:
            llm_calls += 1
            llm_tokens_in += v.input_tokens
            llm_tokens_out += v.output_tokens
            llm_cost += compute_cost_microusd(v.model, v.input_tokens, v.output_tokens)
            if pace_s:
                time.sleep(pace_s)
        rows.append(
            {
                **c,
                "slice": _slice(c),
                "blocked": v.blocked,
                "score": v.score,
                "pred_category": v.category,
                "method": v.method,
                "signals": [s["name"] for s in v.details.get("signals", [])],
                "llm": v.details.get("llm"),
            }
        )

    attacks = [r for r in rows if r["label"] == "attack"]
    benign = [r for r in rows if r["label"] == "benign"]
    by_slice: dict[str, dict] = {}
    for name in [*ATTACK_SLICES, "benign"]:
        group = [r for r in rows if r["slice"] == name]
        blocked = sum(r["blocked"] for r in group)
        by_slice[name] = {
            "n": len(group),
            "blocked": blocked,
            "rate": _pct(blocked, len(group)),
        }
    by_source: dict[str, dict] = defaultdict(
        lambda: {"attacks": 0, "caught": 0, "benign": 0, "fp": 0}
    )
    for r in rows:
        s = by_source[r.get("source", "instructions")]
        if r["label"] == "attack":
            s["attacks"] += 1
            s["caught"] += int(r["blocked"])
        else:
            s["benign"] += 1
            s["fp"] += int(r["blocked"])

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] if latencies else 0
    n = len(rows)
    return {
        "config": {
            "guardrail_llm": "on" if llm_on else "off",
            "classifier_model": settings.guardrail_llm_model if llm_on else None,
            "llm_provider": settings.llm_provider if llm_on else None,
            "method": "cascade" if llm_on else "heuristics_only",
        },
        "n_attack": len(attacks),
        "n_benign": len(benign),
        "catch_rate": _pct(sum(r["blocked"] for r in attacks), len(attacks)),
        "false_positive_rate": _pct(sum(r["blocked"] for r in benign), len(benign)),
        "latency_ms_p50": round(p50, 3),
        "latency_ms_p99": round(p99, 3),
        "missed": [r["id"] for r in attacks if not r["blocked"]],
        "false_positives": [r["id"] for r in benign if r["blocked"]],
        "by_slice": by_slice,
        "by_source": dict(by_source),
        "classifier": {
            "calls": llm_calls,
            "share_of_requests": _pct(llm_calls, n),
            "input_tokens": llm_tokens_in,
            "output_tokens": llm_tokens_out,
            "cost_usd": microusd_to_usd(llm_cost),
            "cost_usd_per_1k_requests": round(microusd_to_usd(llm_cost) * 1000 / n, 4) if n else 0,
        },
        "rows": rows,
    }


def _print(report: dict) -> None:
    cfg = report["config"]
    head = f"red-team eval: {report['n_attack']} attacks, {report['n_benign']} benign"
    if cfg["guardrail_llm"] == "on":
        head += f"  [cascade: heuristics + {cfg['classifier_model']} via {cfg['llm_provider']}]"
    else:
        head += "  [heuristics only, GUARDRAIL_LLM=off]"
    print(head)
    print(f"  catch rate          : {report['catch_rate']:.1%}   missed: {report['missed']}")
    print(
        f"  false positive rate : {report['false_positive_rate']:.1%}   fps: {report['false_positives']}"
    )
    print(
        f"  classifier latency  : p50 {report['latency_ms_p50']} ms, p99 {report['latency_ms_p99']} ms"
    )
    print("  by slice            : ", end="")
    print(
        "  ".join(f"{k} {v['blocked']}/{v['n']}" for k, v in report["by_slice"].items() if v["n"])
    )
    print("  by source           : ", end="")
    print(
        "  ".join(
            f"{k}: caught {v['caught']}/{v['attacks']}, fp {v['fp']}/{v['benign']}"
            for k, v in report["by_source"].items()
        )
    )
    c = report["classifier"]
    if c["calls"]:
        print(
            f"  LLM classifier      : {c['calls']} calls ({c['share_of_requests']:.0%} of requests), "
            f"{c['input_tokens']}+{c['output_tokens']} tokens, ${c['cost_usd']:.5f} "
            f"(${c['cost_usd_per_1k_requests']:.4f} per 1,000 requests at list price)"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--llm", choices=["on", "off"], help="override GUARDRAIL_LLM for this run")
    ap.add_argument(
        "--pace",
        type=float,
        default=None,
        help="seconds to sleep after each classifier call (default 6 with --llm on: free-tier RPM)",
    )
    args = ap.parse_args()
    if args.llm:
        os.environ["GUARDRAIL_LLM"] = args.llm
    pace = args.pace if args.pace is not None else (6.0 if args.llm == "on" else 0.0)

    report = run(pace_s=pace)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"last_{report['config']['method']}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    else:
        _print(report)
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
