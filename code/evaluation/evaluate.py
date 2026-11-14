"""Evaluation harness.

Role in the submission
----------------------
Compares predicted answers with the provided sample answers and reports the metrics required
by the task: case-level status accuracy, step-level precision / recall / F1, field-level
accuracy, and sequence accuracy.  It also writes a per-case diff so each failure can be
traced.  Self-contained (csv + json only): `python code/evaluation/evaluate.py pred.csv expected.csv`.

Metric definitions
------------------
status accuracy     fraction of cases whose deletion_status equals the expected one
step P / R / F1     exact step tuples (phase, system_id, resource_id, operation, state,
                    evidence_id), step_no ignored, counted as multisets across all cases
step (no evidence)  the same without evidence_id, to separate "found the right thing" from
                    "cited the right file"
field accuracy      for each field, fraction of expected steps whose predicted step at the same
                    position has the same value (missing steps count as wrong)
sequence accuracy   exact match of the whole steps array / mean LCS ratio of the step order
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter

FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load_rows(path) -> dict[str, tuple[str, list[dict]]]:
    """Read an output-format CSV into {case_id: (status, steps)}."""
    rows = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[row["case_id"]] = (row["deletion_status"], json.loads(row["steps_json"] or "[]"))
    return rows


def key(step: dict, with_evidence: bool = True) -> tuple:
    """Comparable tuple of a step (step_no excluded)."""
    base = (step["phase"], step["system_id"], step["resource_id"], step["operation"], step["state"])
    return base + (step["evidence_id"],) if with_evidence else base


def lcs(a: list, b: list) -> int:
    """Length of the longest common subsequence of two lists."""
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            table[i + 1][j + 1] = table[i][j] + 1 if x == y else max(table[i][j + 1], table[i + 1][j])
    return table[-1][-1]


def evaluate(pred: dict, expected: dict) -> tuple[dict, list[str]]:
    """Compute all metrics over the cases present in `expected`; return (metrics, diff lines)."""
    status_ok = exact = 0
    tp = fp = fn = tp_ne = fp_ne = fn_ne = 0
    field_ok = Counter()
    field_total = Counter()
    lcs_ratios = []
    diffs: list[str] = []
    for case_id, (exp_status, exp_steps) in expected.items():
        pred_status, pred_steps = pred.get(case_id, ("MISSING", []))
        status_ok += pred_status == exp_status
        exact += pred_steps == exp_steps and pred_status == exp_status
        # Steps are compared as multisets of tuples, so their order does not affect precision/recall (order is scored separately below).
        e_keys, p_keys = Counter(map(key, exp_steps)), Counter(map(key, pred_steps))
        common = e_keys & p_keys
        tp += sum(common.values())
        fn += sum((e_keys - p_keys).values())
        fp += sum((p_keys - e_keys).values())
        e_ne = Counter(key(s, False) for s in exp_steps)
        p_ne = Counter(key(s, False) for s in pred_steps)
        common_ne = e_ne & p_ne
        tp_ne += sum(common_ne.values())
        fn_ne += sum((e_ne - p_ne).values())
        fp_ne += sum((p_ne - e_ne).values())
        # Field accuracy is position aligned: the i-th expected step is compared with the i-th predicted step.
        for index, step in enumerate(exp_steps):
            for name in FIELDS:
                field_total[name] += 1
                if index < len(pred_steps) and pred_steps[index].get(name) == step[name]:
                    field_ok[name] += 1
        ek, pk = [key(s) for s in exp_steps], [key(s) for s in pred_steps]
        # LCS ratio: how much of the step ORDER is shared (1.0 = identical sequence).
        lcs_ratios.append(lcs(ek, pk) / max(len(ek), len(pk), 1))
        if pred_status != exp_status or (e_keys != p_keys) or ek != pk:
            diffs.append(f"### {case_id}: expected {exp_status}, got {pred_status}")
            for k in sorted((e_keys - p_keys).elements()):
                diffs.append(f"- MISSING {k}")
            for k in sorted((p_keys - e_keys).elements()):
                diffs.append(f"- EXTRA   {k}")
            if e_keys == p_keys and ek != pk:
                diffs.append("- same steps, different ORDER")
            diffs.append("")
    n = max(len(expected), 1)

    def prf(t: int, f_p: int, f_n: int) -> tuple[float, float, float]:
        """Precision, recall, F1 from counts."""
        p = t / (t + f_p) if t + f_p else 0.0
        r = t / (t + f_n) if t + f_n else 0.0
        return p, r, (2 * p * r / (p + r) if p + r else 0.0)

    p, r, f1 = prf(tp, fp, fn)
    p2, r2, f2 = prf(tp_ne, fp_ne, fn_ne)
    metrics = {
        "cases": len(expected),
        "status_accuracy": status_ok / n,
        "step_precision": p, "step_recall": r, "step_f1": f1,
        "step_precision_no_evidence": p2, "step_recall_no_evidence": r2, "step_f1_no_evidence": f2,
        "field_accuracy": {name: field_ok[name] / field_total[name] if field_total[name] else 0.0
                           for name in FIELDS},
        "sequence_exact_match": exact / n,
        "sequence_lcs_ratio": sum(lcs_ratios) / n,
    }
    return metrics, diffs


def render_markdown(metrics: dict, diffs: list[str]) -> str:
    """Human-readable report (also reused when assembling evaluation_report.md)."""
    lines = ["# Evaluation on sample cases", "",
             f"- Cases: {metrics['cases']}",
             f"- Status accuracy: {metrics['status_accuracy']:.3f}",
             f"- Step precision / recall / F1: {metrics['step_precision']:.3f} / "
             f"{metrics['step_recall']:.3f} / {metrics['step_f1']:.3f}",
             f"- Step P / R / F1 ignoring evidence_id: {metrics['step_precision_no_evidence']:.3f} / "
             f"{metrics['step_recall_no_evidence']:.3f} / {metrics['step_f1_no_evidence']:.3f}",
             f"- Sequence exact match: {metrics['sequence_exact_match']:.3f}",
             f"- Sequence LCS ratio: {metrics['sequence_lcs_ratio']:.3f}", "",
             "## Field-level accuracy (position aligned)"]
    for name, value in metrics["field_accuracy"].items():
        lines.append(f"- {name}: {value:.3f}")
    lines += ["", "## Per-case differences", ""] + (diffs or ["None."])
    return "\n".join(lines)


def compare_files(pred_path, expected_path, report_path=None) -> dict:
    """Load two output CSVs, evaluate on the expected cases and optionally write the report."""
    metrics, diffs = evaluate(load_rows(pred_path), load_rows(expected_path))
    text = render_markdown(metrics, diffs)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return metrics


if __name__ == "__main__":
    print(json.dumps(compare_files(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None), indent=2))