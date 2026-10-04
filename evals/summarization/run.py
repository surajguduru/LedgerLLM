"""Summarization golden-set eval.

    python -m evals.summarization.run --provider mock --gate      # programmatic checks only (CI, free)
    python -m evals.summarization.run --provider gemini --gate    # + LLM-as-judge (needs LLM_API_KEY; free tier)

OWNER: Sai. Grow golden.jsonl to ~30 hand-written documents (store the TEXT, not just URLs — URLs drift),
finish the judge prompt, calibrate it against a few human-labelled examples, and log runs with the prompt hash.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

from app.feature.fetch import FetchedPage
from app.feature.prompts import load_prompt
from app.feature.summarize import build_user_prompt, output_token_cap, run_summary

HERE = Path(__file__).parent
MAX_WORDS = 120

JUDGE_SYSTEM = (
    "You are a strict evaluator of summaries. Score the SUMMARY against the SOURCE on two 1-5 scales: "
    "faithfulness (5 = every claim is supported by the source, no invented facts) and coverage "
    "(5 = all key points of the source are present). Reply with JSON only: "
    '{"faithfulness": <int>, "coverage": <int>, "issues": ["<short>", ...]}'
)


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in (HERE / "golden.jsonl").read_text().splitlines()
        if line.strip()
    ]


def key_point_hit_rate(summary: str, key_points: list[str]) -> float:
    s = summary.lower()
    hits = 0
    for kp in key_points:
        words = [w for w in re.findall(r"[a-z]{4,}", kp.lower())]
        if words and sum(w in s for w in words) / len(words) >= 0.5:
            hits += 1
    return hits / max(1, len(key_points))


def judge(provider, source: str, summary: str, model: str) -> dict:
    user = f"<source>\n{source}\n</source>\n<summary>\n{summary}\n</summary>"
    res = provider.complete(model=model, system=JUDGE_SYSTEM, user=user, max_tokens=300)
    try:
        return json.loads(res.text[res.text.find("{") : res.text.rfind("}") + 1])
    except json.JSONDecodeError:
        return {"faithfulness": 0, "coverage": 0, "issues": ["judge returned non-JSON"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--provider",
        default="mock",
        choices=[
            "mock",
            "gemini",
            "groq",
            "openai",
            "openrouter",
            "ollama",
            "openai_compat",
            "anthropic",
        ],
    )
    ap.add_argument("--model", default=None, help="defaults to DEFAULT_MODEL")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()

    os.environ["LLM_PROVIDER"] = args.provider
    from app.config import get_settings
    from app.llm import get_provider

    provider = get_provider()
    args.model = args.model or get_settings().default_model
    prompt = load_prompt()
    th = yaml.safe_load((HERE / "thresholds.yaml").read_text())
    rows = []
    for c in load_cases():
        page = FetchedPage(
            url=None,
            final_url=None,
            title=c["title"],
            text=c["text"],
            content_type="text/plain",
            fetched_ms=0,
        )
        user = build_user_prompt(
            prompt, page, style="bullets", max_words=MAX_WORDS, instructions=None
        )
        r = run_summary(
            provider,
            model=args.model,
            prompt=prompt,
            user_prompt=user,
            max_tokens=output_token_cap(prompt, MAX_WORDS),
        )
        row = {
            "id": c["id"],
            "hit_rate": key_point_hit_rate(r.text, c["key_points"]),
            "length_ratio": len(r.text.split()) / MAX_WORDS,
            "leaks": [m for m in c.get("must_not_include", []) if m.lower() in r.text.lower()],
            "tokens": r.input_tokens + r.output_tokens,
        }
        if args.provider != "mock":
            row["judge"] = judge(provider, c["text"], r.text, args.model)
        rows.append(row)

    n = len(rows)
    avg_hit = sum(r["hit_rate"] for r in rows) / n
    max_len = max(r["length_ratio"] for r in rows)
    leaks = [r["id"] for r in rows if r["leaks"]]
    print(
        f"summarization eval ({args.provider}, prompt {prompt.version}@{prompt.content_hash}, n={n})"
    )
    print(
        f"  key-point hit rate : {avg_hit:.2f}   length ratio max: {max_len:.2f}   leaks: {leaks}"
    )
    ok = avg_hit >= th["min_key_point_hit_rate"] and max_len <= th["max_length_ratio"] and not leaks
    if args.provider != "mock":
        f = sum(r["judge"]["faithfulness"] for r in rows) / n
        cov = sum(r["judge"]["coverage"] for r in rows) / n
        print(f"  judge faithfulness : {f:.2f}   coverage: {cov:.2f}")
        ok = ok and f >= th["min_faithfulness"] and cov >= th["min_coverage"]
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"last_{args.provider}.json").write_text(json.dumps(rows, indent=2))
    if args.gate:
        print(f"gate: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
