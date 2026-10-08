"""How far the LLM judge agrees with a human on calibration.jsonl.

    python -m evals.summarization.agreement            # agreement report (+ how many rows still need scores)
    python -m evals.summarization.agreement --blind    # print source + summary of unscored rows, no judge scores

Each row of calibration.jsonl is one summary from a real judged run: `case` (the golden.jsonl id),
`run` (results timestamp), the models and prompt, `summary`, the `judge` scores, and `human` scores that start
as null. Rows are chosen to span good and bad judge scores, so agreement is not measured only on easy cases.
The five current rows come from the 30-case Groq run (`qwen/qwen3.8-27b` summaries, `openai/gpt-oss-120b`
judge) and were re-judged after the rubric fix; their human scores are still pending, so this module reports
no agreement number yet, only how many rows are left.

Known limits: five rows from one human say whether the judge is roughly right, not how often it is wrong; and
agreement on Qwen summaries says nothing direct about how the judge treats the production model's summaries.

Human scoring procedure (by hand, never generated):
  1. Run with --blind. It prints the source document and the summary, and hides the judge's scores.
  2. Read the whole source, then the summary. Score faithfulness and coverage 1-5 with the rubric in
     run.JUDGE_SYSTEM, and note every claim the source does not support. Decide before looking at `judge`.
  3. Write the scores and notes into the row's `human` object. Only then compare with the judge.
  4. Run without flags: exact agreement, within-1 agreement and mean absolute error per dimension.
A null human score means "not scored yet"; such rows are left out of the numbers and reported as pending.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIMENSIONS = ("faithfulness", "coverage")


def load_rows(path: Path = HERE / "calibration.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pending(rows: list[dict]) -> int:
    """Rows where at least one dimension has no human score yet."""
    return sum(any(r["human"].get(d) is None for d in DIMENSIONS) for r in rows)


def agreement(rows: list[dict]) -> dict[str, dict]:
    """Per dimension: n scored pairs, exact and within-1 agreement (share), mean absolute error."""
    out: dict[str, dict] = {}
    for d in DIMENSIONS:
        pairs = [(r["judge"][d], r["human"][d]) for r in rows if r["human"].get(d) is not None]
        diffs = [abs(j - h) for j, h in pairs]
        n = len(diffs)
        out[d] = {
            "n": n,
            "exact": round(sum(x == 0 for x in diffs) / n, 3) if n else None,
            "within_1": round(sum(x <= 1 for x in diffs) / n, 3) if n else None,
            "mae": round(sum(diffs) / n, 3) if n else None,
        }
    return out


def print_blind(rows: list[dict], golden: dict[str, dict]) -> None:
    for r in rows:
        if not any(r["human"].get(d) is None for d in DIMENSIONS):
            continue
        case = golden[r["case"]]
        print(f"=== {r['id']} ({r['case']}: {case['title']}) ===")
        print(f"--- source ---\n{case['text']}\n--- summary ---\n{r['summary']}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=HERE / "calibration.jsonl")
    ap.add_argument("--blind", action="store_true", help="print unscored rows without judge scores")
    args = ap.parse_args(argv)
    rows = load_rows(args.file)
    if args.blind:
        golden = {
            c["id"]: c
            for c in (
                json.loads(line)
                for line in (HERE / "golden.jsonl").read_text().splitlines()
                if line.strip()
            )
        }
        print_blind(rows, golden)
        return 0
    print(f"judge vs human agreement ({args.file.name}, {len(rows)} rows)")
    for d, a in agreement(rows).items():
        if a["n"]:
            print(
                f"  {d:<12} n={a['n']}  exact {a['exact']:.0%}  within-1 {a['within_1']:.0%}  "
                f"MAE {a['mae']:.2f}"
            )
        else:
            print(f"  {d:<12} n=0  (no human scores yet)")
    left = pending(rows)
    if left:
        print(f"pending: {left} cases need human scores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
