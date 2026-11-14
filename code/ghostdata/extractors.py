"""Text extraction for the non-text evidence formats (PDFs and screenshots).

Role in the pipeline
--------------------
Phase 1 needs text from every evidence file before it can look for identifiers. SQL, JSON,
logs and text files are plain text; the other two formats are handled here:

  * PDFs are read with `pdftotext` when installed, otherwise with the `pypdf` library.
  * Screenshots are read with the `tesseract` OCR program. OCR output is cached on disk
    keyed by file id, so OCR runs at most once per image.

Both tools are optional: every function returns None when its tool is unavailable, and the
caller records that in EvidenceFile.status instead of aborting the run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .config import LEGACY_OCR_CACHE_DIR, OCR_CACHE_DIR, PDFTOTEXT_CMD, RESOURCE_RE, TESSERACT_CMD


def _resolve_tool(command: str) -> str | None:
    """Return an executable path for `command` (explicit path or found on PATH), else None."""
    if Path(command).exists():
        return command
    return shutil.which(command)


def extract_pdf_text(path: Path) -> str | None:
    """Extract text from a PDF. Tries pdftotext first, then pypdf. Returns None if neither works."""
    tool = _resolve_tool(PDFTOTEXT_CMD)
    if tool:
        # '-layout' keeps table columns aligned, which makes handover-style documents easier to read line by line.
        proc = subprocess.run([tool, "-layout", str(path), "-"], capture_output=True)
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace")
    try:
        from pypdf import PdfReader  # optional dependency
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None


def ocr_image(path: Path, file_id: str) -> str | None:
    """OCR a screenshot with tesseract and cache the text.

    The image bytes are piped through stdin instead of passing the path on the command
    line, because tesseract on Windows cannot open files whose path contains non-ASCII
    characters (a common situation with localised user-profile folders).
    """
    # Look in the current cache first, then in the cache left by earlier exploration runs, so OCR is never repeated.
    for cache_dir in (OCR_CACHE_DIR, LEGACY_OCR_CACHE_DIR):
        cached = cache_dir / f"{file_id}.txt"
        if cached.exists():
            return cached.read_text(encoding="utf-8")
    tool = _resolve_tool(TESSERACT_CMD)
    if not tool:
        return None
    # '--psm 6' tells tesseract to expect one uniform block of text, which suits screenshots of tables and admin consoles.
    proc = subprocess.run([tool, "stdin", "stdout", "--psm", "6"],
                          input=path.read_bytes(), capture_output=True)
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (OCR_CACHE_DIR / f"{file_id}.txt").write_text(text, encoding="utf-8")
    return text


def normalise_ocr(text: str) -> str:
    """Repair the most common OCR damage to resource identifiers: lower-case the text and turn
    'user 17', 'data flow 12', 'audit record 3' (spaces/hyphens instead of underscores)
    into their canonical underscore form."""
    # Lower-case first: resource ids are lower-case in the data and OCR often changes letter case.
    t = text.lower()
    t = re.sub(r"user[\s_\-]+(\d+)", r"user_\1", t)
    t = re.sub(r"data[\s_\-]+flow[\s_\-]+(\d+)", r"data_flow_\1", t)
    t = re.sub(r"audit[\s_\-]+record[\s_\-]+(\d+)", r"audit_record_\1", t)
    t = re.sub(r"legal[\s_\-]+hold[\s_\-]+(\d+)", r"legal_hold_\1", t)
    t = re.sub(r"\b(account|search|analytics|support|export|backup|marketing|tombstone|object|cache)"
               r"[\s\-]+(\d+)\b", r"\1_\2", t)
    return t


def extract_doc_resource_ids(text: str, ocr: bool = False) -> list[str]:
    """List the resource ids mentioned in a PDF or screenshot text.

    Ids that are really part of a file name are ignored: 'customer_export_2026-02-10.zip'
    must not yield 'export_2026'. A match is dropped when it is preceded by 'customer'
    or followed by a '-MM-DD' date suffix.
    """
    body = normalise_ocr(text) if ocr else text
    found: set[str] = set()
    # Scan every id-shaped token; the checks below drop the ones that are really part of a file name.
    for match in RESOURCE_RE.finditer(body):
        before = body[max(0, match.start() - 9):match.start()].lower()
        after = body[match.end():match.end() + 6]
        if re.search(r"customer[ _]$", before):
            continue
        if re.match(r"-\d{2}-\d{2}", after):
            continue
        found.add(match.group(0))
    return sorted(found)