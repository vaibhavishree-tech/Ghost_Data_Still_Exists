"""Format-specific parsers. Each parser receives an EvidenceFile with its text already
loaded and returns a list of NormRecords (via Normalizer.build).

  sql   run the dump in an in-memory SQLite database; one record per row
  json  walk the document; one record per object holding a 'resource_id'
  log   group lines sharing a trace id (identity and resource id are often on different
        lines of one request); one record per group
  txt   shift-handover text: one record per pipe-delimited line
  pdf / image   one document-level record that lists the ids mentioned in the text

Parsers never decide what a record means; they only extract what it says.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .config import RESOURCE_RE
from .extractors import extract_doc_resource_ids
from .io_utils import try_parse_ts
from .models import EvidenceFile, NormRecord, ScopeEntry
from .normalize import Normalizer

TRACE_RE = re.compile(r"\b(?:tr|rb)-[A-Za-z0-9\-]+")
KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HANDOVER_COLUMNS = ["ts", "system", "resource_id", "category", "state", "identifier"]


# ---------------------------------------------------------------- SQL
def parse_sql(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Parse a SQL dump. Tables without a resource_id column are skipped and counted in
    ev.stats so validation can reconcile rows. JSON payload columns (record_json) are expanded
    into the record's fields. The table name is kept as `source` because it distinguishes the
    primary from each replica."""
    # The dump is plain SQL: running it in an in-memory SQLite database gives access to every table and row without hand-written SQL parsing.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(ev.text)
    except sqlite3.Error as exc:
        ev.status = f"sql_error: {exc}"
        connection.close()
        return []
    records: list[NormRecord] = []
    skipped = 0
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        if "resource_id" not in columns:
            skipped += len(rows)
            if "origin_system" in columns and "captured_at" in columns:   # snapshot_scope table
                for row in rows:
                    ev.scopes.append(ScopeEntry(ev.file_id, str(row["origin_system"]),try_parse_ts(row["captured_at"]) or ev.captured_at))
            continue
        for number, row in enumerate(rows, 1):
            fields = dict(row)
            # Most tables keep the record as a JSON text column; expand it so its keys become ordinary fields.
            payload = fields.pop("record_json", None)
            if payload:
                try:
                    fields.update(json.loads(payload))
                except json.JSONDecodeError:
                    fields["record_json_unparsed"] = payload
            resource_id = fields.get("resource_id")
            if not resource_id:
                skipped += 1
                continue
            records.append(nz.build(
                ev, str(resource_id), fields, source=table, locator=f"table={table} row={number}",
                blob=json.dumps(fields, ensure_ascii=False, default=str)))
    connection.close()
    ev.stats["sql_rows_skipped"] = skipped
    return records


# ---------------------------------------------------------------- JSON
def parse_json(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Parse a JSON evidence file. Snapshot bundles carry per-section data_store, collection
    and captured_at, which are propagated to the records in that section. Service exports
    carry the system name in the key path (service_exports.<system>...), used as a system hint."""
    try:
        data = json.loads(ev.text)
    except json.JSONDecodeError as exc:
        ev.status = f"json_error: {exc}"
        return []
    records: list[NormRecord] = []

    def walk(node: Any, path: str, ctx: dict) -> None:
        """Recursively visit the JSON tree; ctx carries the data store, collection and capture time of the enclosing snapshot section."""
        if isinstance(node, dict):
            ctx = dict(ctx)
            # A snapshot section header: remember its data store and collection and declare the scope it covers.
            if "data_store" in node and "collection" in node and "resource_id" not in node:
                ctx["data_store"], ctx["collection"] = node["data_store"], node["collection"]
                ev.scopes.append(ScopeEntry(ev.file_id, str(node["data_store"]),try_parse_ts(node.get("captured_at")) or ev.captured_at,str(node["collection"])))
                if node.get("captured_at"):
                    ctx["captured_at"] = node["captured_at"]
            match = re.match(r"service_exports\.([A-Za-z_]+)", path)
            if match:
                ctx["system_hint"] = match.group(1)
            resource_id = node.get("resource_id")
            if isinstance(resource_id, str) and resource_id:
                fields = dict(node)
                for key in ("data_store", "collection"):
                    if key in ctx:
                        fields.setdefault(key, ctx[key])
                records.append(nz.build(
                    ev, resource_id, fields, source=str(ctx.get("data_store") or ctx.get("system_hint") or ev.origin),
                    locator=path or "$", blob=json.dumps(node, ensure_ascii=False, default=str),
                    captured_at=try_parse_ts(ctx.get("captured_at")), system_hint=ctx.get("system_hint")))
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key), ctx)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", ctx)

    # Service exports list the systems they cover as keys; each one is declared as a scope of this file.
    exports = data.get("service_exports") if isinstance(data, dict) else None
    if isinstance(exports, dict):                       # one key per covered system
        for system in exports:
            ev.scopes.append(ScopeEntry(ev.file_id, str(system), ev.captured_at))
    walk(data, "", {})
    return records


# ---------------------------------------------------------------- logs
def _parse_line_fields(line: str) -> dict[str, Any]:
    """Extract fields from one log line. Supported layouts: JSON after the timestamp,
    key=value pairs, '[LEVEL] [service] [trace] event:' application traces, pipe-delimited
    operations streams, and cloud-runtime lines with backslash-escaped quotes."""
    # Remove the leading timestamp, then try each supported log layout in turn.
    rest = TS_RE.sub("", line, count=1).strip()
    if rest.startswith("{"):
        try:
            return dict(json.loads(rest))
        except json.JSONDecodeError:
            pass
    fields: dict[str, Any] = {}
    for key, value in KV_RE.findall(rest.replace('\\"', '"')):
        fields.setdefault(key, value.strip('"'))
    trace = re.match(r"\[(\w+)\]\s+\[(\w+)\]\s+\[([^\]]+)\]\s+(\w+):", rest)
    if trace:
        fields.setdefault("level", trace.group(1))
        fields.setdefault("service", trace.group(2))
        fields.setdefault("trace_id", trace.group(3))
        fields.setdefault("event", trace.group(4))
    if " | " in rest and "event" not in fields:
        parts = [p.strip() for p in rest.split(" | ")]
        if len(parts) >= 5:
            fields.setdefault("level", parts[0])
            fields.setdefault("service", parts[1])
            fields.setdefault("trace_id", parts[2])
            fields.setdefault("event", parts[3])
    if "trace_id" not in fields and "requestId" in fields:
        fields["trace_id"] = fields["requestId"]
    if "trace_id" not in fields:
        found = TRACE_RE.search(line)
        if found:
            fields["trace_id"] = found.group(0)
    return fields


def _log_record(ev: EvidenceFile, nz: Normalizer, entries: list[tuple[int, str, dict]],
                locator: str) -> NormRecord | None:
    """Merge the lines of one trace into a record. The first value seen for each field wins;
    the record's observed_at is the latest line timestamp. Returns None when no line of the
    group carries a resource id."""
    merged: dict[str, Any] = {}
    for _, _, fields in entries:
        for key, value in fields.items():
            merged.setdefault(key, value)
    resource_id = next((str(f["resource_id"]) for _, _, f in entries if f.get("resource_id")), None)
    if not resource_id:
        return None
    stamps = [t for t in (try_parse_ts(TS_RE.match(line).group(0)) if TS_RE.match(line) else None
                          for _, line, _ in entries) if t]
    service = str(merged.get("service") or merged.get("logger") or merged.get("system") or "")
    return nz.build(ev, resource_id, merged, source=service, locator=locator,
                    blob="\n".join(line for _, line, _ in entries),
                    observed_at=max(stamps) if stamps else None, system_hint=service or None,
                    raw_lines=tuple(n for n, _, _ in entries))


def parse_log(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Group log lines by trace id so a 'started' line (identity) and its 'completed' line
    (resource id and result) become one record. Lines without a trace id become single-line
    records. Line numbers are retained so validation can prove every line carrying a resource
    id was consumed."""
    # Lines of one request share a trace id; grouping them lets an identity line and a result line form one record.
    groups: dict[str, list[tuple[int, str, dict]]] = {}
    solo: list[tuple[int, str, dict]] = []
    for number, line in enumerate(ev.text.splitlines(), 1):
        if not line.strip():
            continue
        fields = _parse_line_fields(line)
        trace_id = fields.get("trace_id")
        if trace_id:
            groups.setdefault(str(trace_id), []).append((number, line, fields))
        else:
            solo.append((number, line, fields))
    records: list[NormRecord] = []
    for trace_id, entries in groups.items():
        record = _log_record(ev, nz, entries, f"trace={trace_id} lines={entries[0][0]}-{entries[-1][0]}")
        if record:
            records.append(record)
    for entry in solo:
        record = _log_record(ev, nz, [entry], f"line={entry[0]}")
        if record:
            records.append(record)
    return records


# ---------------------------------------------------------------- handover text
def parse_txt(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Parse the shift-handover text: '<ts> | <system> | <resource> | <CATEGORY> | <STATE> |
    <identifier>'. The category declared here is authoritative and is carried through."""
    records: list[NormRecord] = []
    for number, line in enumerate(ev.text.splitlines(), 1):
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) >= 6 and RESOURCE_RE.fullmatch(parts[2]):
            fields = dict(zip(HANDOVER_COLUMNS, parts))
            records.append(nz.build(ev, parts[2], fields, source=parts[1], locator=f"line={number}",
                                    blob=line, observed_at=try_parse_ts(parts[0]),
                                    system_hint=parts[1], raw_lines=(number,)))
    return records


# ---------------------------------------------------------------- PDF / screenshots
def parse_document(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Produce one document-level record for a PDF or screenshot. The record deliberately has
    no resource_id: a screenshot may list several customers' rows, so attributing the whole
    text to every id it mentions would be wrong. The ids are listed in doc_resource_ids and
    attributed later by a dedicated step."""
    if not ev.text.strip():
        return []
    ids = extract_doc_resource_ids(ev.text, ocr=(ev.kind == "image"))
    fields = {"resource_ids": ids, "chars": len(ev.text)}
    return [nz.build(ev, None, fields, source=ev.origin, locator="whole-document", blob=ev.text,
                     system_hint=ev.origin, doc_resource_ids=tuple(ids))]


_PARSERS = {"sql": parse_sql, "json": parse_json, "log": parse_log, "txt": parse_txt,
            "pdf": parse_document, "image": parse_document}


def parse_file(ev: EvidenceFile, nz: Normalizer) -> list[NormRecord]:
    """Dispatch to the parser for the file's kind."""
    return _PARSERS[ev.kind](ev, nz)