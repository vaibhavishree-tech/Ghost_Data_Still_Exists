"""Normalisation: turn the loosely structured fields of any evidence record into the single
NormRecord shape.

The Normalizer is responsible for four decisions that must be identical for every
evidence format:
  * which system owns a resource            (system_for)
  * which record category it belongs to     (category_for)
  * whether the copy is a primary or replica (topology_for)
  * the canonical state fields it reports    (_state)
plus extraction of the identifiers (emails, user ids, phones, email hashes) a record carries.

These are descriptive only. Whether a record is *exposed*, *clean*, or must be deleted is
decided later by the state-reconstruction and policy stages.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator

from .config import CORE_PREFIXES, PREFIX_TO_CATEGORY, PREFIX_TO_SYSTEM, prefix_of
from .io_utils import try_parse_ts
from .models import EvidenceFile, NormRecord

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

# Words in a SQL table name or file origin that identify a non-authoritative copy.
REPLICA_WORDS = ("replica", "failover", "reporting", "standby", "regional")

# Canonical state names and the raw field names that can carry them.
STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "http_status": ("http_status", "status_code"),
    "rows": ("rows", "row_count", "result_count"),
    "result": ("result",),
    "state": ("state",),
    "account_status": ("account_status",),
    "status": ("status",),
    "enabled": ("enabled",),
    "restore_allowed": ("restore_allowed",),
    "download_available": ("download_available",),
    "archive_present": ("archive_present",),
    "marker_found": ("marker_found",),
    "last_delivery_status": ("last_delivery_status",),
    "retention_until": ("retention_until", "retained_until"),
    "event_code": ("event_code", "event_name"),
}
BOOL_KEYS = {"enabled", "restore_allowed", "download_available", "archive_present", "marker_found"}
INT_KEYS = {"rows", "http_status"}
OBSERVED_KEYS = ("observed_at", "indexed_at", "timestamp", "ts", "event_time", "created_at", "updated_at")


def _walk_items(obj: Any, depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Yield (key, value) for every key in a nested dict/list structure (depth-limited)."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key), value
            if isinstance(value, (dict, list)):
                yield from _walk_items(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)):
                yield from _walk_items(value, depth + 1)


def _lookup(fields: dict, aliases: tuple[str, ...]) -> Any:
    """Return the first non-null scalar value for any alias, preferring top-level keys over
    nested ones (nested profiles inside service exports are searched second)."""
    # Top-level keys are preferred; nested ones (profiles inside service exports) are searched second.
    for alias in aliases:
        value = fields.get(alias)
        if value is not None and not isinstance(value, (dict, list)):
            return value
    for key, value in _walk_items(fields):
        if key in aliases and value is not None and not isinstance(value, (dict, list)):
            return value
    return None


def _coerce(key: str, value: Any) -> Any:
    """Convert a raw state value to a canonical Python value: null-like strings -> None,
    'true'/'false' -> bool, 0/1 on boolean fields -> bool, counters -> int."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if key in BOOL_KEYS else value
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("", "null", "none", "nan"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if key in BOOL_KEYS and text in ("0", "1"):
        return text == "1"
    if key in INT_KEYS and re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


class Normalizer:
    """Builds NormRecords. One instance is shared by all parsers so that system and
    category decisions are consistent across file formats."""

    def __init__(self, system_map: dict[str, str]):
        """`system_map` maps resource_id -> owning system as reported by the activity log."""
        self.system_map = system_map
        self.known_systems = set(PREFIX_TO_SYSTEM.values()) | set(system_map.values())

    def system_for(self, resource_id: str | None, hint: str | None) -> str | None:
        """Decide the owning system. Precedence: id prefix for core record types >
        activity log (explicit target system for this resource) > prefix fallback for the
        remaining types > the hint supplied by the file (a data_store/service name)."""
        if resource_id:
            prefix = prefix_of(resource_id)
            if prefix in CORE_PREFIXES:
                return PREFIX_TO_SYSTEM[prefix]
            if resource_id in self.system_map:
                return self.system_map[resource_id]
            if prefix in PREFIX_TO_SYSTEM:
                return PREFIX_TO_SYSTEM[prefix]
        if hint and hint in self.known_systems:
            return hint
        return None

    def category_for(self, resource_id: str | None, fields: dict, source: str) -> str | None:
        """Decide the record category. A category stated by the evidence itself (handover
        text) wins. Otherwise it comes from the id prefix, refined for the two prefixes that
        cover several categories (support: message vs attachment; analytics: profile vs
        telemetry). This is a heuristic; the distribution is reported by validation so it
        can be audited against the retention rules."""
        declared = fields.get("category")
        if isinstance(declared, str) and declared.strip():
            return declared.strip().upper()
        if not resource_id:
            return None
        prefix = prefix_of(resource_id)
        context = " ".join(str(x).lower() for x in (source, fields.get("collection"),fields.get("collection_name"), fields.get("data_store")))
        if prefix == "support":
            attachment_keys = ("file_name", "filename", "content_type", "object_key", "attachment_id")
            if "attach" in context or any(k in fields for k in attachment_keys):
                return "SUPPORT_ATTACHMENT"
            return "SUPPORT_MESSAGE"
        if prefix == "analytics" and "telemetry" in context:
            return "PRODUCT_TELEMETRY"
        return PREFIX_TO_CATEGORY.get(prefix)

    def topology_for(self, ev: EvidenceFile, source: str) -> str:
        """Classify where the record came from. For SQL the table name decides (a file can mix
        a primary table and replica tables); the file origin is the fallback. Other formats
        map directly from the file kind and origin."""
        if ev.kind == "sql":
            table = source.lower()
            if "primary" in table:
                return "PRIMARY"
            if any(word in table for word in REPLICA_WORDS):
                return "REPLICA"
            origin = ev.origin.lower()
            if "primary" in origin:
                return "PRIMARY"
            if any(word in origin for word in REPLICA_WORDS):
                return "REPLICA"
            return "INVENTORY"
        if ev.kind == "json":
            origin = ev.origin.lower()
            if "snapshot" in origin:
                return "SNAPSHOT"
            if "export" in origin:
                return "EXPORT"
            return "JSON"
        return {"log": "LOG", "txt": "HANDOVER", "pdf": "DOCUMENT", "image": "SCREENSHOT"}[ev.kind]

    def _state(self, fields: dict) -> dict[str, Any]:
        """Extract the canonical state fields present in a record. A service-export entry with
        a 'profile' key also reports whether that profile is null (record absent)."""
        state: dict[str, Any] = {}
        for canonical, aliases in STATE_ALIASES.items():
            value = _coerce(canonical, _lookup(fields, aliases))
            if value is not None:
                state[canonical] = value
        if "profile" in fields:
            state["profile_present"] = bool(fields["profile"])
        return state

    @staticmethod
    def _phones(fields: dict) -> set[str]:
        """Collect values of any key containing 'phone', at any nesting depth."""
        phones = set()
        for key, value in _walk_items(fields):
            if "phone" in key.lower() and isinstance(value, str) and value.strip().lower() not in ("", "null", "none"):
                phones.add(value.strip())
        return phones

    @staticmethod
    def _observed(fields: dict) -> datetime | None:
        """Return the record's own timestamp if it carries one."""
        for key in OBSERVED_KEYS:
            parsed = try_parse_ts(fields.get(key))
            if parsed:
                return parsed
        return None

    def build(self, ev: EvidenceFile, resource_id: str | None, fields: dict, *, source: str,
              locator: str, blob: str, captured_at: datetime | None = None,
              observed_at: datetime | None = None, system_hint: str | None = None,
              raw_lines: tuple[int, ...] = (), doc_resource_ids: tuple[str, ...] = ()) -> NormRecord:
        """Create a NormRecord from parsed fields. `record_id` is left as 0 and assigned by the
        store once all files are loaded. Identifiers are extracted from the full text (`blob`)
        so values in nested JSON or free-text log messages are included."""
        # A hint is a system name the file itself provides (data_store, origin_system ...); it is used only when the id prefix and the activity log cannot decide.
        hint = system_hint or fields.get("data_store") or fields.get("origin_system") or fields.get("system_id")
        system_id = self.system_for(resource_id, hint if isinstance(hint, str) else None)
        # Quality flags make unexplained records visible in the Phase 1 validation report instead of silently dropping them.
        flags: list[str] = []
        if resource_id and system_id is None:
            flags.append("unknown_system")
        if isinstance(hint, str) and hint in self.known_systems and system_id and hint != system_id:
            flags.append("system_hint_mismatch")
        category = self.category_for(resource_id, fields, source)
        if resource_id and category is None:
            flags.append("unknown_category")
        return NormRecord(
            record_id=0,
            file_id=ev.file_id,
            file_kind=ev.kind,
            captured_at=captured_at or try_parse_ts(fields.get("captured_at")) or ev.captured_at,
            observed_at=observed_at or self._observed(fields),
            topology=self.topology_for(ev, source),
            source=source,
            system_id=system_id,
            resource_id=resource_id,
            category=category,
            # Identifiers are taken from the whole text (blob) so values nested in JSON or inside free-text log messages are found.
            emails=tuple(sorted({e.lower() for e in EMAIL_RE.findall(blob)})),
            user_ids=tuple(sorted(set(USER_RE.findall(blob)))),
            phones=tuple(sorted(self._phones(fields))),
            hashes=tuple(sorted({h.lower() for h in HASH_RE.findall(blob)})),
            state=self._state(fields),
            locator=locator,
            raw_lines=raw_lines,
            doc_resource_ids=doc_resource_ids,
            flags=tuple(flags),
            blob=blob,
        )