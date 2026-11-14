"""Validation of the normalised record table against the raw evidence.

Row counts are checked with counters that do NOT share code with the parsers:
  sql   number of INSERT statements  == parsed rows + rows in tables without resource_id
  json  number of '"resource_id": "<value>"' occurrences == records
  log   every line containing a resource id belongs to some record
  txt   every line containing a resource id belongs to some record
  pdf / image   non-empty text yields exactly one document record

The report also summarises the record table (topology, system, category, identifier and
state coverage) so classification heuristics can be audited before later stages rely on them.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .config import RESOURCE_RE
from .models import EvidenceFile, NormRecord
from .store import EvidenceStore

_INSERT_RE = re.compile(r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\b", re.IGNORECASE | re.MULTILINE)
_JSON_RID_RE = re.compile(r'"resource_id"\s*:\s*"[^"]+"')


@dataclass
class FileCheck:
    """Result of reconciling one evidence file with its parsed records."""
    file_id: str
    kind: str
    origin: str
    status: str
    records: int
    expected: int
    ok: bool
    detail: str


def check_file(ev: EvidenceFile, records: list[NormRecord]) -> FileCheck:
    """Reconcile one file's parsed records with an independent count taken from its raw text."""
    if ev.kind == "sql":
        expected = len(_INSERT_RE.findall(ev.text))
        skipped = ev.stats.get("sql_rows_skipped", 0)
        ok = expected == len(records) + skipped
        detail = f"INSERT statements={expected}; parsed rows={len(records)}; skipped rows={skipped}"
    elif ev.kind == "json":
        expected = len(_JSON_RID_RE.findall(ev.text))
        ok = expected == len(records)
        detail = f"resource_id occurrences={expected}; records={len(records)}"
    elif ev.kind in ("log", "txt"):
        lines = ev.text.splitlines()
        id_lines = {n for n, line in enumerate(lines, 1) if RESOURCE_RE.search(line)}
        covered: set[int] = set()
        for record in records:
            covered.update(record.raw_lines)
        uncovered = sorted(id_lines - covered)
        expected = len(id_lines)
        ok = not uncovered
        detail = f"lines with resource ids={expected}; covered={expected - len(uncovered)}"
        if uncovered:
            samples = "; ".join(f"L{n}: {lines[n - 1].strip()[:90]}" for n in uncovered[:3])
            detail += f"; UNCOVERED {len(uncovered)} e.g. {samples}"
    else:
        expected = 1 if ev.text.strip() else 0
        ok = expected == len(records)
        ids = records[0].doc_resource_ids if records else ()
        detail = f"chars={len(ev.text)}; ids mentioned={', '.join(ids) or 'none'}"
    # A file passes only if the counts reconcile AND it was read successfully (or is legitimately empty).
    ok = ok and ev.status in ("ok", "empty")
    return FileCheck(ev.file_id, ev.kind, ev.origin, ev.status, len(records), expected, ok, detail)


def run_checks(store: EvidenceStore) -> list[FileCheck]:
    """Run check_file for every evidence file, in file-id order."""
    return [check_file(ev, store.by_file.get(fid, [])) for fid, ev in sorted(store.files.items())]


def _table(headers: list[str], rows: list[list]) -> str:
    """Render a Markdown table."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def build_report(store: EvidenceStore, checks: list[FileCheck]) -> str:
    """Compose the Markdown validation report: per-file reconciliation, mismatches, and
    distributions of the normalised record table."""
    out = ["# Phase 1 - evidence normalisation report\n"]
    failed = [c for c in checks if not c.ok]
    out.append(f"Files: {len(checks)}; records: {len(store.records)}; "
               f"files failing validation: {len(failed)}\n")

    out.append("## Per-file reconciliation\n")
    out.append(_table(["file", "kind", "origin", "status", "records", "check", "detail"],
                      [[c.file_id, c.kind, c.origin, c.status, c.records, "OK" if c.ok else "FAIL", c.detail]
                       for c in checks]))

    out.append("\n## Records by file kind and topology\n")
    counts = Counter((r.file_kind, r.topology) for r in store.records)
    out.append(_table(["file kind", "topology", "records"], [[k, t, n] for (k, t), n in sorted(counts.items())]))

    out.append("\n## Records by system_id\n")
    out.append(_table(["system_id", "records"],
                      [[s or "(none)", n] for s, n in Counter(r.system_id for r in store.records).most_common()]))

    out.append("\n## Records by category\n")
    out.append(_table(["category", "records"],
                      [[c or "(none)", n] for c, n in Counter(r.category for r in store.records).most_common()]))

    out.append("\n## Data-quality flags\n")
    flags = Counter(f for r in store.records for f in r.flags)
    out.append(_table(["flag", "records"], [[f, n] for f, n in flags.most_common()]) if flags else "none")
    examples = [r for r in store.records if r.flags][:12]
    if examples:
        out.append("\nExamples:\n")
        out.append(_table(["file", "resource", "system", "source", "flags"],
                          [[r.file_id, r.resource_id, r.system_id, r.source, ",".join(r.flags)] for r in examples]))

    out.append("\n## Identifier coverage by file kind (records carrying each identifier)\n")
    cover: dict[str, Counter] = defaultdict(Counter)
    for r in store.records:
        cover[r.file_kind]["records"] += 1
        for name in ("emails", "user_ids", "phones", "hashes"):
            if getattr(r, name):
                cover[r.file_kind][name] += 1
    out.append(_table(["file kind", "records", "email", "user_id", "phone", "hash"],
                      [[k, c["records"], c["emails"], c["user_ids"], c["phones"], c["hashes"]]
                       for k, c in sorted(cover.items())]))

    out.append("\n## State fields present, by category\n")
    keys: dict[str, Counter] = defaultdict(Counter)
    for r in store.records:
        for key in r.state:
            keys[r.category or "(none)"][key] += 1
    out.append(_table(["category", "state fields (count)"],
                      [[cat, ", ".join(f"{k}:{n}" for k, n in c.most_common())] for cat, c in sorted(keys.items())]))

    out.append("\n## Snapshot scope (systems each file is authoritative for)\n")
    scope_by_file: dict[str, set[str]] = defaultdict(set)
    for entry in store.scopes:
        scope_by_file[entry.file_id].add(entry.system_id)
    out.append(_table(["file", "records", "systems in scope"],
                      [[fid, len(store.by_file.get(fid, [])), ", ".join(sorted(systems))]
                       for fid, systems in sorted(scope_by_file.items())]))
    
    out.append("\n## Activity log\n")
    out.append(_table(["event type", "events"],
                      [[t, n] for t, n in Counter(e.event_type for e in store.activity).most_common()]))
    out.append(f"\nResources with an activity-derived system: {len(store.system_map)}")
    return "\n".join(out) + "\n"