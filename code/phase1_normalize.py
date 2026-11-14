"""Phase 1 entry point: normalise all evidence and validate the result.

Steps
  1. Load every evidence file and the activity log into an EvidenceStore.
  2. Reconcile record counts with the raw files (ghostdata.validate).
  3. Write the normalised tables and a Markdown report to work/phase1/:
       records.csv       one row per NormRecord (without the raw text blob)
       activity.csv      the normalised activity log
       file_checks.csv   per-file reconciliation results
       phase1_report.md  human-readable summary

Usage (from the repository root):
    python code/phase1_normalize.py            # include OCR of screenshots
    python code/phase1_normalize.py --no-ocr   # skip screenshots
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostdata.config import PHASE1_DIR  # noqa: E402
from ghostdata.io_utils import configure_console, fmt_ts  # noqa: E402
from ghostdata.store import EvidenceStore, load_store  # noqa: E402
from ghostdata.validate import build_report, run_checks  # noqa: E402

RECORD_COLUMNS = ["record_id", "file_id", "file_kind", "captured_at", "observed_at", "topology", "source",
                  "system_id", "resource_id", "category", "emails", "user_ids", "phones", "hashes",
                  "state_json", "locator", "doc_resource_ids", "flags"]


def write_records(store: EvidenceStore, path: Path) -> None:
    """Write the normalised record table; multi-valued fields are joined with '|'."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(RECORD_COLUMNS)
        for r in store.records:
            writer.writerow([
                r.record_id, r.file_id, r.file_kind, fmt_ts(r.captured_at), fmt_ts(r.observed_at),
                r.topology, r.source, r.system_id or "", r.resource_id or "", r.category or "",
                "|".join(r.emails), "|".join(r.user_ids), "|".join(r.phones), "|".join(r.hashes),
                json.dumps(r.state, ensure_ascii=False, default=str, sort_keys=True),
                r.locator, "|".join(r.doc_resource_ids), "|".join(r.flags)])


def write_activity(store: EvidenceStore, path: Path) -> None:
    """Write the normalised activity log."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["activity_id", "event_time", "event_type", "source_system", "target_system",
                         "resource_id", "result", "emails", "user_ids", "details"])
        for e in store.activity:
            writer.writerow([e.activity_id, fmt_ts(e.event_time), e.event_type, e.source_system,
                             e.target_system, e.resource_id, e.result, "|".join(e.emails),
                             "|".join(e.user_ids), e.details])


def main() -> None:
    """Run the full Phase 1 procedure and print a short console summary."""
    configure_console()
    parser = argparse.ArgumentParser(description="Phase 1: evidence normalisation")
    parser.add_argument("--no-ocr", action="store_true", help="do not OCR screenshots")
    args = parser.parse_args()

    started = time.time()
    PHASE1_DIR.mkdir(parents=True, exist_ok=True)
    # Parse everything, then reconcile each file's record count against an independent count of its raw text.
    store = load_store(use_ocr=not args.no_ocr)
    checks = run_checks(store)

    write_records(store, PHASE1_DIR / "records.csv")
    write_activity(store, PHASE1_DIR / "activity.csv")
    with open(PHASE1_DIR / "file_checks.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file_id", "kind", "origin", "status", "records", "expected", "ok", "detail"])
        for c in checks:
            writer.writerow([c.file_id, c.kind, c.origin, c.status, c.records, c.expected, c.ok, c.detail])
    report = build_report(store, checks)
    (PHASE1_DIR / "phase1_report.md").write_text(report, encoding="utf-8")

    failed = [c for c in checks if not c.ok]
    print(f"files={len(checks)} records={len(store.records)} activity_events={len(store.activity)} "
          f"failed_checks={len(failed)} ({time.time() - started:.1f}s)")
    for c in failed:
        print(f"  FAIL {c.file_id} [{c.kind}] {c.detail}")
    print(f"[written] {PHASE1_DIR}")


if __name__ == "__main__":
    main()