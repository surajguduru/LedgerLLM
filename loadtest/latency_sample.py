"""Real-provider latency sample.

The burst test (locustfile.py) deliberately runs on the mock provider: the claim under test there is
our quota logic, not Google's response time (decision D8). That leaves a second number to produce --
how slow is a real request -- and this script produces it.

    # server must be running with the real provider:
    LLM_PROVIDER=gemini LLM_API_KEY=AIza... make dev
    make seed                                   # once, writes .seed_keys.json
    .venv/bin/python loadtest/latency_sample.py

It reports two timings per document size:
  * end-to-end -- wall clock around the HTTP call, i.e. what a client feels
  * model-only -- usage.latency_ms from the response, i.e. time inside the provider call
and their difference, which is our platform overhead (auth, quota, guardrails, ledger, logging).

Requests are paced with --sleep because the Gemini free tier is rate limited per minute; a 429 from
the provider surfaces as a 502 upstream_error and is recorded as a failure rather than retried, so
the sample stays honest.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

# Document sizes in characters. ~4 chars per token, so these bracket the ~3k-token document the
# latency target in docs/DESIGN.md section 1 is stated for.
SIZES = {"small": 4_000, "medium": 12_000, "large": 24_000}

PARAGRAPH = (
    "The quarterly report shows revenue growth across every region, driven by demand for the new "
    "product line and improved retention in the enterprise segment. Operating margin widened by "
    "two points as infrastructure spend fell after the migration to committed-use pricing. "
    "Headcount grew in support and shrank in field sales. Management guided to high single-digit "
    "growth next quarter and flagged currency exposure in two markets as the main risk. "
)


def document(chars: int) -> str:
    """A document of roughly `chars` characters, built by repeating one realistic paragraph.

    Every call appends a unique marker. The response cache (pipeline stage 4.5) is an exact match on
    the request body and returns before the model call at zero cost, so sending the same document
    twice would measure the cache, not the model. The marker keeps every request a genuine miss.
    """
    body = (PARAGRAPH * (chars // len(PARAGRAPH) + 1))[:chars]
    return f"{body} [sample {uuid.uuid4().hex}]"


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation, so every number reported is an observed sample."""
    if not values:
        return float("nan")
    ranked = sorted(values)
    index = min(len(ranked) - 1, max(0, round(q / 100 * len(ranked) + 0.5) - 1))
    return ranked[index]


def api_key(explicit: str | None, alias: str) -> str:
    if explicit:
        return explicit
    keys_file = REPO_ROOT / ".seed_keys.json"
    if not keys_file.exists():
        raise SystemExit("no .seed_keys.json -- run `make seed`, or pass --key")
    keys = json.loads(keys_file.read_text())
    if alias not in keys:
        raise SystemExit(f"alias {alias!r} not in .seed_keys.json (have: {', '.join(keys)})")
    return keys[alias]


def measure(client: httpx.Client, key: str, text: str, max_words: int) -> dict:
    """One request. Returns end-to-end ms, the model's own ms, and what it cost."""
    started = time.perf_counter()
    r = client.post(
        "/v1/summarize",
        json={"text": text, "style": "bullets", "max_words": max_words},
        headers={"X-API-Key": key},
    )
    end_to_end_ms = (time.perf_counter() - started) * 1000
    if r.status_code != 200:
        body = r.json().get("error", {})
        return {"ok": False, "status": r.status_code, "code": body.get("code", "?")}
    usage = r.json()["usage"]
    return {
        "ok": True,
        "end_to_end_ms": end_to_end_ms,
        "model_ms": float(usage["latency_ms"]),
        "overhead_ms": end_to_end_ms - float(usage["latency_ms"]),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "model": usage["model"],
        "cached": usage["cached"],
    }


def summarise(label: str, samples: list[dict]) -> dict:
    ok = [s for s in samples if s["ok"]]
    stats = {"size": label, "n": len(ok), "failed": len(samples) - len(ok)}
    for field in ("end_to_end_ms", "model_ms", "overhead_ms"):
        values = [s[field] for s in ok]
        stats[field] = {
            "p50": round(percentile(values, 50), 1),
            "p95": round(percentile(values, 95), 1),
            "p99": round(percentile(values, 99), 1),
            "max": round(max(values), 1) if values else float("nan"),
        }
    if ok:
        stats["input_tokens_p50"] = int(statistics.median(s["input_tokens"] for s in ok))
        stats["cost_usd_p50"] = round(statistics.median(s["cost_usd"] for s in ok), 6)
        stats["model"] = ok[0]["model"]
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--key", default=None, help="API key (default: read .seed_keys.json)")
    parser.add_argument("--alias", default="pro", help="which seeded tenant to bill (default: pro)")
    parser.add_argument(
        "--per-size", type=int, default=10, help="requests per document size (default: 10 -> 30)"
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=4.5,
        help="seconds between requests; the Gemini free tier allows ~15/min (default: 4.5)",
    )
    parser.add_argument("--max-words", type=int, default=150)
    parser.add_argument("--out", default="loadtest/latency_sample.json")
    args = parser.parse_args()

    key = api_key(args.key, args.alias)
    total = args.per_size * len(SIZES)
    print(
        f"{total} requests to {args.host}, {args.sleep}s apart -> ~{total * args.sleep / 60:.1f} min\n"
    )

    results: list[dict] = []
    with httpx.Client(base_url=args.host, timeout=120.0) as client:
        for label, chars in SIZES.items():
            samples = []
            for i in range(args.per_size):
                # fresh document per request: see document() -- a repeat would be a free cache hit
                sample = measure(client, key, document(chars), args.max_words)
                samples.append(sample)
                if sample["ok"]:
                    print(
                        f"  {label:<6} {i + 1:>2}/{args.per_size}  "
                        f"end-to-end {sample['end_to_end_ms']:>7.0f} ms  "
                        f"model {sample['model_ms']:>7.0f} ms  "
                        f"overhead {sample['overhead_ms']:>6.0f} ms"
                    )
                else:
                    print(
                        f"  {label:<6} {i + 1:>2}/{args.per_size}  "
                        f"FAILED {sample['status']} {sample['code']}"
                    )
                if i < args.per_size - 1:
                    time.sleep(args.sleep)
            results.append(summarise(label, samples))
            print()

    print("| size | n | in-tok p50 | end-to-end p50 | p95 | p99 | model p50 | p99 | overhead p50 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if not r["n"]:
            print(f"| {r['size']} | 0 (all {r['failed']} failed) | - | - | - | - | - | - | - |")
            continue
        print(
            f"| {r['size']} | {r['n']} | {r['input_tokens_p50']} "
            f"| {r['end_to_end_ms']['p50']:.0f} ms | {r['end_to_end_ms']['p95']:.0f} ms "
            f"| {r['end_to_end_ms']['p99']:.0f} ms | {r['model_ms']['p50']:.0f} ms "
            f"| {r['model_ms']['p99']:.0f} ms | {r['overhead_ms']['p50']:.0f} ms |"
        )

    if args.per_size < 100:
        print(
            f"\nNote: with n={args.per_size} per size, p99 is the slowest observed request, not a "
            "true 99th percentile. Reported as-is; the steady-state p99 comes from the Locust run."
        )

    out = REPO_ROOT / args.out
    out.write_text(json.dumps({"host": args.host, "sizes": results}, indent=2) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
