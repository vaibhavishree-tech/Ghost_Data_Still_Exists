"""Additional evaluation cases: three small synthetic scenarios run through the real engine.

Run:  python code/evaluation/custom_cases.py

Each scenario is written as a tiny dataset (same file formats as the real one) under
work/custom_cases/dataset, the engine is pointed at it through GHOST_DATASET_DIR, and the
predictions are scored against hand-written expectations with the same metrics as the samples.

  case_901  look-alike email        - identity must be an exact match, never a prefix/similar match
  case_902  replica drift           - a record that survives only on a replica must be reported
  case_903  re-enabled data flow    - the latest state at audit_at wins; later evidence is ignored

The expected answers are my reading of deletion_rules.csv and of the sample answers; they are not
official answers.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
REAL_DATASET = REPO_ROOT / "dataset"
CUSTOM_ROOT = REPO_ROOT / "work" / "custom_cases"
DATASET = CUSTOM_ROOT / "dataset"

# Must be set before ghostdata.config is imported: it reads the variable at import time.
os.environ["GHOST_DATASET_DIR"] = str(DATASET)
sys.path.insert(0, str(CODE_DIR))

USERS = [  # user_id, name, email, phone
    ("user_901", "Jordan Lee", "jordan.lee@gmail.com", "+1 (202) 555-0901"),
    ("user_902", "Jordan Leigh", "jordan.lee1@gmail.com", "+1 (202) 555-0902"),
    ("user_903", "Sam Okafor", "sam.okafor@outlook.com", "+234 803 555 0903"),
    ("user_905", "Priya Nair", "priya.nair@gmail.com", "+91 98765 00905"),
]

REQUESTS = [  # case_id, email, requested_at, reported_status, reported_at, audit_at, text
    ("case_901", "jordan.lee@gmail.com", "2026-09-10T09:00:00Z", "COMPLETE", "2026-09-12T10:00:00Z",
     "2026-09-25T10:00:00Z", "Please delete my account and all my personal data."),
    ("case_902", "sam.okafor@outlook.com", "2026-09-10T09:00:00Z", "COMPLETE", "2026-09-12T10:00:00Z",
     "2026-09-25T10:00:00Z", "Please delete everything you hold about me."),
    ("case_903", "priya.nair@gmail.com", "2026-09-10T09:00:00Z", "COMPLETE", "2026-09-12T10:00:00Z",
     "2026-09-24T12:00:00Z", "Please stop using my data and delete it."),
]

EVIDENCE = [  # file_id, relative path, origin, captured_at
    ("evidence_91", "evidence/records/custom_bundle_lookalike_20260917.json",
     "records_assurance_snapshot_bundle", "2026-09-17T12:00:00Z"),
    ("evidence_92", "evidence/databases/custom_read_replica_20260921.sql",
     "customer_data_read_replica_capture", "2026-09-21T10:00:00Z"),
    ("evidence_93", "evidence/logs/custom_connector_trace_20260926.log",
     "central_observability_export", "2026-09-26T23:59:59Z"),
]


def write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    """Write a CSV file with a header row, creating parent folders when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def sha(email: str) -> str:
    """SHA-256 hex digest of a lower-cased email (the format of user_identities.csv email_hash)."""
    return hashlib.sha256(email.lower().encode()).hexdigest()


def write_text(rel: str, text: str) -> None:
    """Write a text file below the synthetic dataset folder, creating parent folders when needed."""
    path = DATASET / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def bundle_lookalike() -> str:
    """Case 901: two different people whose emails differ by one character."""
    def profile(rid, user, name, email, phone):
        """One account profile as it appears in a snapshot bundle."""
        return {"resource_id": rid, "customer_reference": user, "full_name": name, "email": email,
                "phone": phone, "account_status": "ACTIVE", "signup_channel": "organic_search"}
    return json.dumps({
        "exported_at": "2026-09-17T12:00:00Z", "source": "records_assurance_service",
        "snapshots": [
            {"captured_at": "2026-09-16T12:00:00Z", "data_store": "account_service",
             "collection": "customer_profiles",
             "records": [profile("account_9001", "user_901", "Jordan Lee", "jordan.lee@gmail.com", "+1 (202) 555-0901"),
                         profile("account_9002", "user_902", "Jordan Leigh", "jordan.lee1@gmail.com", "+1 (202) 555-0902")]},
            {"captured_at": "2026-09-16T12:00:00Z", "data_store": "search_service",
             "collection": "customer_search_documents",
             "records": [{"resource_id": "search_9003", "index_name": "customer_directory_v3",
                          "document": {"display_name": "Jordan Leigh", "email": "jordan.lee1@gmail.com",
                                       "customer_reference": "user_902"}}]},
        ]}, indent=2)


def sql_row(table: str, rid: str, system: str, collection: str, record: dict) -> str:
    """Render one INSERT statement in the same layout as the real SQL snapshots (JSON payload in record_json)."""
    blob = json.dumps(record, separators=(",", ":")).replace("'", "''")
    return f"INSERT INTO \"{table}\" VALUES('{rid}','{system}','{collection}','{blob}','2026-09-21T10:00:00Z');"


def sql_replica() -> str:
    """Case 902: the record is gone from the primary but still present on the read replica."""
    def table(name):
        """CREATE TABLE statement matching the real snapshots."""
        return (f"CREATE TABLE {name} (\n            resource_id TEXT PRIMARY KEY,\n"
                "            origin_system TEXT NOT NULL,\n            collection_name TEXT NOT NULL,\n"
                "            record_json TEXT NOT NULL CHECK (json_valid(record_json)),\n"
                "            updated_at TEXT NOT NULL\n        );")
    other = {"resource_id": "search_9199", "index_name": "customer_directory_v3",
             "document": {"display_name": "Unrelated Person", "email": "unrelated.person@gmail.com",
                          "customer_reference": "user_904"}}
    drifted = {"resource_id": "search_9102", "index_name": "customer_directory_v3",
               "document": {"display_name": "Sam Okafor", "email": "sam.okafor@outlook.com",
                            "customer_reference": "user_903"}}
    lines = ["-- Ghost Data Still Exists: point-in-time database snapshot", "-- Dialect: SQLite-compatible SQL",
             "-- Captured at: 2026-09-21T10:00:00Z", "-- Database topology: read_replica",
             "-- Compare the primary and any replica tables for replication drift.", "",
             "BEGIN TRANSACTION;", table("customer_data_primary"),
             sql_row("customer_data_primary", "search_9199", "search_service", "customer_search_documents", other),
             table("customer_data_read_replica"),
             sql_row("customer_data_read_replica", "search_9199", "search_service", "customer_search_documents", other),
             sql_row("customer_data_read_replica", "search_9102", "search_service", "customer_search_documents", drifted),
             "COMMIT;", ""]
    return "\n".join(lines)


def log_connector() -> str:
    """Case 903: disabled, then re-enabled before audit_at, then 'disabled' again AFTER audit_at."""
    def line(ts, level, event, enabled, sched, extra=""):
        """One connector log line in the same layout as the real application traces."""
        return (f'{ts} [{level}] [integration_service] [tr-92010{ts[9:10]}] {event}: instance="integration-1" '
                f'pod="integration-a1" region="eu-west-1" resource_id="data_flow_9201" enabled="{enabled}" '
                f'scheduler_registered="{sched}"{extra} correlation_key="user_905"')
    return "\n".join([
        line("2026-09-20T10:00:00Z", "INFO", "connector_config_loaded", "false", "false"),
        line("2026-09-22T10:00:00Z", "WARN", "connector_heartbeat", "true", "true", ' last_run="success"'),
        line("2026-09-26T10:00:00Z", "INFO", "connector_config_loaded", "false", "false"),
        ""])


def build_dataset() -> None:
    """Create the complete synthetic dataset (identities, requests, evidence manifest and evidence files) from scratch."""
    if DATASET.exists():
        shutil.rmtree(DATASET)
    DATASET.mkdir(parents=True)
    shutil.copy(REAL_DATASET / "deletion_rules.csv", DATASET / "deletion_rules.csv")
    write_csv(DATASET / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
              [(u, n, e, p, sha(e), "2026-05-01T00:00:00Z") for u, n, e, p in USERS])
    write_csv(DATASET / "deletion_requests.csv",
              ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"], REQUESTS)
    write_csv(DATASET / "sample_deletion_requests.csv",
              ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"], [])
    write_csv(DATASET / "system_activity.csv",
              ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
               "reported_result", "details"], [])
    write_csv(DATASET / "user_audit_log.csv",
              ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
    write_csv(DATASET / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], EVIDENCE)
    write_text(EVIDENCE[0][1], bundle_lookalike())
    write_text(EVIDENCE[1][1], sql_replica())
    write_text(EVIDENCE[2][1], log_connector())


def step(no, phase, system, rid, op, state, evidence=""):
    """Build one expected step dictionary in the submission schema."""
    return {"evidence_id": evidence, "operation": op, "phase": phase, "resource_id": rid,
            "state": state, "step_no": no, "system_id": system}


# Hand-written expected answers (status and ordered steps) for each scenario.
EXPECTED = {
    "case_901": ("INCOMPLETE", [
        step(1, "FINDING", "account_service", "account_9001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_91"),
        step(2, "REMEDIATION", "account_service", "account_9001", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "account_service", "account_9001", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    "case_902": ("INCOMPLETE", [
        step(1, "FINDING", "search_service", "search_9102", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_92"),
        step(2, "REMEDIATION", "search_service", "search_9102", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "search_service", "search_9102", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    "case_903": ("INCOMPLETE", [
        step(1, "FINDING", "integration_service", "data_flow_9201", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "evidence_93"),
        step(2, "REMEDIATION", "integration_service", "data_flow_9201", "DISABLE_DATA_FLOW", "DISABLED"),
        step(3, "VERIFICATION", "integration_service", "data_flow_9201", "VERIFY_DATA_FLOW_DISABLED", "DISABLED")]),
}
WATCH = {  # resources that must NOT appear in the answer for that case
    "case_901": ["account_9002", "search_9003"],
    "case_902": [],
    "case_903": [],
}


def write_answers(path: Path, answers: dict[str, tuple[str, list]]) -> None:
    """Write {case_id: (status, steps)} in the submission format (case_id, deletion_status, steps_json)."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for case_id in sorted(answers):
            status, steps = answers[case_id]
            writer.writerow([case_id, status, json.dumps(steps, separators=(",", ":"))])


def main() -> None:
    """Build the synthetic dataset, run the real engine on it, compare with the expectations and print a PASS/FAIL line per case."""
    build_dataset()
    from ghostdata.identity import IdentityDirectory
    from ghostdata.io_utils import configure_console, read_csv
    from ghostdata.pipeline import AuditEngine
    from ghostdata.store import load_store
    from evaluation.evaluate import compare_files

    configure_console()
    # Run the REAL engine on the synthetic dataset; no OCR is needed because it contains no images.
    engine = AuditEngine(load_store(use_ocr=False), IdentityDirectory.load())
    rows = read_csv("deletion_requests.csv")
    engine.prepare(read_csv("sample_deletion_requests.csv") + rows)
    results = {r["case_id"]: engine.audit(r["case_id"]) for r in rows}

    predicted = {cid: (res.status, res.steps) for cid, res in results.items()}
    write_answers(CUSTOM_ROOT / "custom_predictions.csv", predicted)
    write_answers(CUSTOM_ROOT / "custom_expected.csv", EXPECTED)
    metrics = compare_files(CUSTOM_ROOT / "custom_predictions.csv", CUSTOM_ROOT / "custom_expected.csv",
                            CUSTOM_ROOT / "custom_eval.md")

    lines = ["# Additional evaluation cases", ""]
    for cid, (status, steps) in EXPECTED.items():
        got_status, got_steps = predicted[cid]
        key = lambda s: (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"], s["evidence_id"])
        same = [key(s) for s in got_steps] == [key(s) for s in steps]
        leaked = [r for r in WATCH[cid] if any(s["resource_id"] == r for s in got_steps)]
        verdict = "PASS" if got_status == status and same and not leaked else "FAIL"
        lines.append(f"- {cid}: {verdict} (expected {status}, got {got_status}, "
                     f"steps match={same}, wrongly included={leaked or 'none'})")
        if verdict == "FAIL":
            lines.append(f"  - got: {json.dumps(got_steps)}")
    lines += ["", f"Aggregate: status accuracy {metrics['status_accuracy']:.3f}, step F1 {metrics['step_f1']:.3f}, "
                  f"sequence exact match {metrics['sequence_exact_match']:.3f}", ""]
    report = "\n".join(lines)
    (CUSTOM_ROOT / "custom_summary.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()