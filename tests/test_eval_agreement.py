"""Judge-vs-human agreement report (evals/summarization/agreement.py). No network."""

from __future__ import annotations

import json

from evals.summarization import agreement as ag


def _row(judge: tuple[int, int], human: tuple[int | None, int | None]) -> dict:
    return {
        "id": "x",
        "case": "sum-001",
        "summary": "- s",
        "judge": {"faithfulness": judge[0], "coverage": judge[1], "issues": []},
        "human": {"faithfulness": human[0], "coverage": human[1], "notes": ""},
    }


def test_known_pairs_give_known_agreement():
    rows = [
        _row((5, 4), (5, 4)),  # exact, exact
        _row((4, 2), (5, 4)),  # off by 1, off by 2
        _row((2, 3), (5, 3)),  # off by 3, exact
        _row((5, 5), (5, 4)),  # exact, off by 1
    ]
    a = ag.agreement(rows)
    assert a["faithfulness"] == {"n": 4, "exact": 0.5, "within_1": 0.75, "mae": 1.0}
    assert a["coverage"] == {"n": 4, "exact": 0.5, "within_1": 0.75, "mae": 0.75}
    assert ag.pending(rows) == 0


def test_null_human_scores_are_pending_and_left_out():
    rows = [_row((5, 4), (5, 4)), _row((3, 3), (None, None)), _row((4, 4), (4, None))]
    a = ag.agreement(rows)
    assert a["faithfulness"]["n"] == 2 and a["coverage"]["n"] == 1
    assert ag.pending(rows) == 2


def test_report_prints_pending_count(tmp_path, capsys):
    f = tmp_path / "cal.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [_row((5, 4), (None, None))]) + "\n")
    assert ag.main(["--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "faithfulness n=0" in out and "pending: 1 cases need human scores" in out


def test_blind_mode_hides_judge_scores(tmp_path, capsys):
    f = tmp_path / "cal.jsonl"
    row = _row((2, 1), (None, None))
    row["judge"]["issues"] = ["JUDGE-ISSUE-MARKER"]
    f.write_text(json.dumps(row) + "\n")
    ag.main(["--file", str(f), "--blind"])
    out = capsys.readouterr().out
    assert "--- source ---" in out and "- s" in out
    assert "JUDGE-ISSUE-MARKER" not in out and "faithfulness" not in out


def test_committed_calibration_rows_are_well_formed():
    rows = ag.load_rows()
    assert rows and len({r["id"] for r in rows}) == len(rows)
    for r in rows:
        assert set(r["judge"]) >= {"faithfulness", "coverage", "issues"} and r["summary"]
        assert set(r["human"]) == {"faithfulness", "coverage", "notes"}
