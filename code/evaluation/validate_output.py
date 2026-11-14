"""Format validator for the submission file.

Role in the submission
----------------------
Checks output.csv against every structural rule of the task before it is submitted:
exactly one row per request, valid JSON, consecutive step numbers, phase/state consistent
with the operation, the required step order, no duplicate steps, [] only for NOT_APPLICABLE,
verification-only steps with evidence on COMPLETE rows, and evidence only on findings of
INCOMPLETE rows.  Usage: python code/evaluation/validate_output.py [output.csv] [requests.csv]
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

OPERATIONS = [
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"), ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"), ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"), ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"), ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"), ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"), ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"), ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
]
RANK = {op: i for i, (_, op, _) in enumerate(OPERATIONS)}
EXPECT = {op: (phase, state) for phase, op, state in OPERATIONS}
FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate(output_path: Path, requests_path: Path) -> list[str]:
    """Return a list of human-readable problems (empty list = file is valid)."""
    problems: list[str] = []
    with open(requests_path, newline="", encoding="utf-8") as handle:
        expected_ids = [r["case_id"] for r in csv.DictReader(handle)]
    with open(output_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    ids = [r["case_id"] for r in rows]
    if len(rows) != len(expected_ids):
        problems.append(f"row count {len(rows)} != {len(expected_ids)}")
    if set(ids) != set(expected_ids) or len(set(ids)) != len(ids):
        problems.append("case ids missing, extra or duplicated")
    # Case ids are unpadded (case_9, case_10), so row order is checked numerically.
    numeric = [int(re.sub(r"\D", "", i) or 0) for i in ids]
    if numeric != sorted(numeric):
        problems.append("rows are not in numeric case order")
    for row in rows:
        case, status = row["case_id"], row["deletion_status"]
        try:
            steps = json.loads(row["steps_json"])
        except json.JSONDecodeError:
            problems.append(f"{case}: invalid JSON")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            problems.append(f"{case}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            problems.append(f"{case}: NOT_APPLICABLE must be []")
        if status != "NOT_APPLICABLE" and not steps:
            problems.append(f"{case}: {status} with empty steps")
        if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
            problems.append(f"{case}: step_no not consecutive from 1")
        keys, seen = [], set()
        for s in steps:
            if set(s) != FIELDS:
                problems.append(f"{case}: wrong fields {sorted(s)}")
                continue
            if EXPECT.get(s["operation"]) != (s["phase"], s["state"]):
                problems.append(f"{case}: step {s['step_no']} phase/state do not match {s['operation']}")
            keys.append((RANK[s["operation"]], s["system_id"], s["resource_id"]))
            ident = (s["operation"], s["system_id"], s["resource_id"])
            if ident in seen:
                problems.append(f"{case}: duplicate step {ident}")
            seen.add(ident)
            # COMPLETE rows may contain only evidence-backed verification steps.
            if status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
                problems.append(f"{case}: COMPLETE step {s['step_no']} must be verification with evidence")
            # INCOMPLETE rows: findings cite evidence; remediation and verification are future work with empty evidence.
            if status == "INCOMPLETE" and ((s["phase"] == "FINDING") != bool(s["evidence_id"])):
                problems.append(f"{case}: INCOMPLETE step {s['step_no']} evidence rule broken")
        if keys != sorted(keys):
            problems.append(f"{case}: steps not in required order")
        if status == "INCOMPLETE":
            findings = {(s["system_id"], s["resource_id"]) for s in steps if s["phase"] == "FINDING"}
            remedied = {(s["system_id"], s["resource_id"]) for s in steps if s["phase"] == "REMEDIATION"}
            if not findings:
                problems.append(f"{case}: INCOMPLETE without findings")
            # Every reported problem needs a remediation step on the same resource, and vice versa.
            if findings != remedied:
                problems.append(f"{case}: findings and remediation differ")
    return problems


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "output.csv"
    req = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "dataset" / "deletion_requests.csv"
    found = validate(out, req)
    print("output.csv is valid" if not found else f"{len(found)} problem(s):")
    for line in found[:50]:
        print(" -", line)
    sys.exit(1 if found else 0)