"""Small I/O helpers shared by every module.

Role in the pipeline
--------------------
  * read_csv                    load any file of the dataset folder as a list of dict rows
  * try_parse_ts / parse_ts / fmt_ts
                                one consistent way to read and print timestamps; every time is
                                converted to timezone-aware UTC so comparisons against audit_at
                                are always valid
  * configure_console           keep printing from crashing on non-ASCII text (names, multilingual
                                requests) under legacy Windows code pages
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from typing import Any

from .config import DATASET_DIR


def configure_console() -> None:
    """Make stdout/stderr tolerant of non-ASCII text (names, multilingual requests) so that
    printing never crashes on consoles with a legacy code page (e.g. Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_csv(name: str) -> list[dict]:
    """Read dataset/<name> into a list of row dicts (UTF-8, BOM tolerant)."""
    with open(DATASET_DIR / name, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def try_parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or return None if the value
    is missing or malformed. Used for optional fields where bad data must not abort a run."""
    if not isinstance(value, str):
        return None
    # 'Z' is rewritten as an explicit UTC offset because fromisoformat on older Python versions rejects it.
    text = value.strip().replace("Z", "+00:00")
    # Anything shorter than YYYY-MM-DD cannot be a date.
    if len(text) < 10:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_ts(value: str) -> datetime:
    """Strict variant of try_parse_ts for fields that must be valid (e.g. audit_at)."""
    parsed = try_parse_ts(value)
    if parsed is None:
        raise ValueError(f"invalid timestamp: {value!r}")
    return parsed


def fmt_ts(value: datetime | None) -> str:
    """Format a datetime as 'YYYY-MM-DDTHH:MM:SSZ' (empty string for None)."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""