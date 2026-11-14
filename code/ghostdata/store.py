"""Evidence store: loads every evidence file and the activity log, normalises them, and
exposes indexes for the later stages.

load_store() is the single entry point used by the pipeline. It is deterministic and takes a
few seconds; nothing is cached between runs except OCR text.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import DATASET_DIR
from .extractors import extract_pdf_text, ocr_image
from .io_utils import parse_ts, read_csv
from .models import ActivityEvent, EvidenceFile, NormRecord, ScopeEntry
from .normalize import EMAIL_RE, USER_RE, Normalizer
from .parsers import parse_file

KIND_BY_EXT = {".sql": "sql", ".json": "json", ".log": "log", ".txt": "txt", ".pdf": "pdf", ".png": "image"}


@dataclass
class EvidenceStore:
    """All normalised evidence plus lookup indexes.

    by_resource  resource_id -> records (document-level records are indexed under every id
                 they mention, so a PDF is found when looking up a resource it lists)
    by_file      file_id -> records
    """
    files: dict[str, EvidenceFile]
    records: list[NormRecord]
    activity: list[ActivityEvent]
    system_map: dict[str, str]
    by_resource: dict[str, list[NormRecord]] = field(default_factory=dict)
    by_file: dict[str, list[NormRecord]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Build the lookup indexes."""
        for record in self.records:
            self.by_file.setdefault(record.file_id, []).append(record)
            ids = [record.resource_id] if record.resource_id else list(record.doc_resource_ids)
            for resource_id in ids:
                self.by_resource.setdefault(resource_id, []).append(record)
    @property
    def scopes(self) -> list[ScopeEntry]:
        """All scope declarations across files (which systems each file is authoritative for)."""
        return [s for ev in self.files.values() for s in ev.scopes]

def load_activity() -> list[ActivityEvent]:
    """Load system_activity.csv. Identifiers mentioned in each event's details are extracted
    because catalog events (LINK_RESOURCE_IDENTITY) tie resources to users that way."""
    events = []
    for row in read_csv("system_activity.csv"):
        details = row.get("details") or ""
        events.append(ActivityEvent(
            activity_id=row["activity_id"], event_time=parse_ts(row["event_time"]),
            event_type=row["event_type"], source_system=row["source_system"],
            target_system=row["target_system"], resource_id=row["resource_id"],
            result=row["reported_result"], details=details,
            emails=tuple(sorted({e.lower() for e in EMAIL_RE.findall(details)})),
            user_ids=tuple(sorted(set(USER_RE.findall(details))))))
    return events


def build_system_map(events: list[ActivityEvent]) -> dict[str, str]:
    """Map resource_id -> owning system using the activity log's target systems (majority vote).
    Targets naming a replica or failover copy are ignored: they describe where a sync went,
    not who owns the resource."""
    # Each activity row votes for the system that handled the resource; the most frequent target wins.
    votes: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        target = (event.target_system or "").lower()
        if not event.resource_id or not target or "replica" in target or "failover" in target:
            continue
        votes[event.resource_id][event.target_system] += 1
    return {rid: counter.most_common(1)[0][0] for rid, counter in votes.items()}


def _load_text(ev: EvidenceFile) -> None:
    """Fill ev.text from disk (text formats) or from PDF extraction / OCR, setting ev.status
    when the required external tool is unavailable."""
    if ev.kind in ("sql", "json", "log", "txt"):
        ev.text = ev.path.read_text(encoding="utf-8", errors="replace")
    elif ev.kind == "pdf":
        text = extract_pdf_text(ev.path)
        ev.text, ev.status = (text, "ok") if text is not None else ("", "pdf_unavailable")
    elif ev.kind == "image":
        text = ocr_image(ev.path, ev.file_id)
        ev.text, ev.status = (text, "ok") if text is not None else ("", "ocr_unavailable")


def load_store(use_ocr: bool = True) -> EvidenceStore:
    """Load and normalise the whole evidence set. With use_ocr=False screenshots are left
    unread (status 'ocr_unavailable')."""
    activity = load_activity()
    system_map = build_system_map(activity)
    normalizer = Normalizer(system_map)
    files: dict[str, EvidenceFile] = {}
    records: list[NormRecord] = []
    for row in read_csv("evidence_files.csv"):
        path = DATASET_DIR / row["file_path"]
        ev = EvidenceFile(row["file_id"], path, row["origin"], parse_ts(row["captured_at"]),
                          KIND_BY_EXT[path.suffix.lower()])
        # Screenshots can be skipped (--no-ocr) for quick runs; they are then reported as unreadable.
        if ev.kind == "image" and not use_ocr:
            ev.status = "ocr_unavailable"
        else:
            _load_text(ev)
        if ev.text:
            parsed = parse_file(ev, normalizer)
            records.extend(parsed)
            if not parsed and ev.status == "ok":
                ev.status = "empty"      # e.g. a genuinely empty snapshot: valid evidence of absence
        files[ev.file_id] = ev
    # Number the records only after every file is parsed so record_id is unique and stable.
    for number, record in enumerate(records, 1):
        record.record_id = number
    return EvidenceStore(files, records, activity, system_map)