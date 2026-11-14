"""Data classes shared across the pipeline (they hold data only; no logic lives here).

ScopeEntry     a declaration that an evidence file is authoritative for one system at one moment
               ("this snapshot covers the account service"). It is what allows the engine to
               treat a missing row as proof of absence.
EvidenceFile   one file listed in evidence_files.csv, with its extracted text.
NormRecord     one observation about one resource, in a format-independent shape. Every
               evidence format (SQL, JSON, log, handover text, PDF, screenshot) is
               converted into NormRecords, so later stages never touch raw formats.
ActivityEvent  one row of system_activity.csv. These are *claims reported by workers*, not
               proof of state; they are kept separate from evidence records on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

@dataclass
class ScopeEntry:
    """Declaration that an evidence file is authoritative for one system at one moment.
    A record missing from a file counts as proof of absence only for systems in scope."""
    file_id: str
    system_id: str
    captured_at: datetime
    collection: str | None = None


@dataclass
class EvidenceFile:
    """An evidence file plus the bookkeeping gathered while loading it."""
    file_id: str
    path: Path
    origin: str                  # producing system, from evidence_files.csv
    captured_at: datetime        # when the evidence was captured (UTC)
    kind: str                    # sql | json | log | txt | pdf | image
    status: str = "ok"           # ok | empty | ocr_unavailable | pdf_unavailable | <parse error>
    text: str = ""               # raw text (for pdf/image: the extracted text)
    stats: dict[str, int] = field(default_factory=dict)   # parser counters used by validation
    scopes: list[ScopeEntry] = field(default_factory=list)


@dataclass
class NormRecord:
    """A normalised observation. Field semantics:

    captured_at  capture time of the file or snapshot section containing the record; used
                 to choose which snapshot describes the world at audit time.
    observed_at  time the record itself was observed/logged, when the source provides one.
    topology     PRIMARY | REPLICA | INVENTORY | SNAPSHOT | EXPORT | LOG | HANDOVER |
                 DOCUMENT | SCREENSHOT | JSON - distinguishes authoritative stores from
                 copies, which is needed to detect replication drift.
    source       the concrete origin inside the file (SQL table, log service, ...).
    system_id    owning system of the resource (see Normalizer.system_for).
    resource_id  None for document-level records (PDF/screenshot), which instead list the
                 ids they mention in doc_resource_ids.
    state        canonical state fields (http_status, rows, enabled, ...); see normalize.py.
    raw_lines    source line numbers (logs/handover) used to verify coverage.
    flags        data-quality markers such as 'system_hint_mismatch'.
    blob         full text of the record, used for identity matching.
    """
    record_id: int
    file_id: str
    file_kind: str
    captured_at: datetime
    observed_at: datetime | None
    topology: str
    source: str
    system_id: str | None
    resource_id: str | None
    category: str | None
    emails: tuple[str, ...]
    user_ids: tuple[str, ...]
    phones: tuple[str, ...]
    hashes: tuple[str, ...]
    state: dict[str, Any]
    locator: str
    raw_lines: tuple[int, ...]
    doc_resource_ids: tuple[str, ...]
    flags: tuple[str, ...]
    blob: str


@dataclass(frozen=True)
class ActivityEvent:
    """One worker/system event from system_activity.csv."""
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    result: str                  # reported_result: SUCCESS / FAILED / PARTIAL ... (a claim)
    details: str
    emails: tuple[str, ...]
    user_ids: tuple[str, ...]