"""Command-line entry point.

Role in the submission
----------------------
    python code/main.py              audit all requests in dataset/deletion_requests.csv and
                                     write <repo>/output.csv (518 rows + header)
    python code/main.py --sample     audit dataset/sample_deletion_requests.csv, compare with
                                     dataset/sample_output.csv and write work/phase2/ reports

Both modes load the sample AND real requests so that activity linking (which assigns
identity-less resources to cases one-to-one) sees every case.  The engine is deterministic
and calls no external model, so the run costs 0 tokens.

Options:  --order natural|lexicographic   row order of output.csv (default natural = file order)
          --no-ocr                         skip screenshot OCR
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Make 'ghostdata' and 'evaluation' importable no matter where the script is started from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostdata.config import REPO_ROOT, WORK_DIR                      # noqa: E402
from ghostdata.identity import IdentityDirectory                       # noqa: E402
from ghostdata.io_utils import configure_console, read_csv             # noqa: E402
from ghostdata.pipeline import AuditEngine, CaseResult                 # noqa: E402
from ghostdata.store import load_store                                 # noqa: E402
from evaluation.evaluate import compare_files                          # noqa: E402

PHASE2_DIR = Path(WORK_DIR) / "phase2"


def write_rows(results: list[CaseResult], path: Path, order: str) -> None:
    """Write results in the submission format (case_id, deletion_status, steps_json)."""
    if order == "lexicographic":
        results = sorted(results, key=lambda r: r.case_id)
    else:
        results = sorted(results, key=lambda r: int(re.sub(r"\D", "", r.case_id) or 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for result in results:
            writer.writerow([result.case_id, result.status, json.dumps(result.steps, separators=(",", ":"))])


def write_diagnostics(engine: AuditEngine, results: list[CaseResult], path: Path) -> None:
    """Per-case diagnostics (users, primary file, tied resources) for debugging."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "status", "users", "primary_file", "tied_by_activity",
                         "n_tied", "n_steps", "notes"])
        for result in results:
            state = engine.states[result.case_id]
            activity = [f"{r}" for r, how in state.tied.items() if how in ("activity", "link", "document")]
            writer.writerow([result.case_id, result.status, "|".join(sorted(state.users)),
                             state.primary_file or "", "|".join(activity), len(state.tied),
                             len(result.steps), ";".join(result.notes + state.notes)])


def main() -> None:
    """Run the audit for samples or for the real requests."""
    parser = argparse.ArgumentParser(description="Ghost Data deletion-audit agent")
    parser.add_argument("--sample", action="store_true", help="run and evaluate the sample cases")
    parser.add_argument("--order", choices=("natural", "lexicographic"), default="natural")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()
    configure_console()

    # 1. Load and normalise all evidence; 2. build the identity directory; 3. prepare ALL requests
    # (samples and real ones) so that activity linking sees every case.
    store = load_store(use_ocr=not args.no_ocr)
    directory = IdentityDirectory.load()
    sample_rows = read_csv("sample_deletion_requests.csv")
    real_rows = read_csv("deletion_requests.csv")
    engine = AuditEngine(store, directory)
    engine.prepare(sample_rows + real_rows)

    # 4. Audit only the requested set, after the global preparation.
    rows = sample_rows if args.sample else real_rows
    results = [engine.audit(row["case_id"]) for row in rows]
    counts = Counter(r.status for r in results)
    print(f"cases={len(results)} statuses={dict(counts)}")
    print(f"linker: accounts={len(engine.linker.account_for)} analytics={len(engine.linker.analytics_for)} "
          f"not_applicable={sum(1 for s in engine.states.values() if s.not_applicable)}")

    write_diagnostics(engine, results, PHASE2_DIR / ("sample_diagnostics.csv" if args.sample else "diagnostics.csv"))
    if args.sample:
        predictions = PHASE2_DIR / "sample_predictions.csv"
        write_rows(results, predictions, args.order)
        metrics = compare_files(predictions, Path(WORK_DIR).parent / "dataset" / "sample_output.csv",
                                PHASE2_DIR / "sample_eval.md")
        print(json.dumps(metrics, indent=2))
        print(f"report: {PHASE2_DIR / 'sample_eval.md'}")
    else:
        if len(results) != 518:
            print(f"WARNING: expected 518 rows, got {len(results)}")
        write_rows(results, Path(REPO_ROOT) / "output.csv", args.order)
        from evaluation.validate_output import validate
        problems = validate(Path(REPO_ROOT) / "output.csv", Path(REPO_ROOT) / "dataset" / "deletion_requests.csv")
        print("format check:", "OK" if not problems else f"{len(problems)} problem(s): {problems[:5]}")
        print(f"wrote {Path(REPO_ROOT) / 'output.csv'}")


if __name__ == "__main__":
    main()