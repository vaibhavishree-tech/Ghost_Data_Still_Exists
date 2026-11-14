"""Project-wide configuration: filesystem layout, external tool commands and the
vocabulary used to recognise resource identifiers.

Role in the pipeline
--------------------
Every other module takes its constants from here, so this file is the single place that
decides
  * where things live on disk (repository root, dataset folder, work folders),
  * how the optional external programs are called (tesseract for screenshots, pdftotext for PDFs),
  * how a resource id such as "account_5684" or "audit_record_12" maps to a prefix, an owning
    system and a default record category.

All paths are derived from this file's location so the pipeline runs from any working
directory. Environment variables allow overriding tool locations without code changes
(TESSERACT_CMD, PDFTOTEXT_CMD) and the dataset location (GHOST_DATASET_DIR). The additional
evaluation cases use GHOST_DATASET_DIR to run the real engine on tiny synthetic datasets.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# <repo>/code/ghostdata/config.py -> parents[2] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = Path(os.environ.get("GHOST_DATASET_DIR", str(REPO_ROOT / "dataset")))
# Generated files (reports, diagnostics, OCR text) go under work/; it is not part of the submission.
WORK_DIR = REPO_ROOT / "work"
PHASE1_DIR = WORK_DIR / "phase1"
OCR_CACHE_DIR = WORK_DIR / "ocr_cache"
# Earlier exploratory runs stored OCR text here; it is read (never written) to avoid re-running OCR.
LEGACY_OCR_CACHE_DIR = WORK_DIR / "phase0" / "ocr_cache"

# External programs. Both are optional: when missing, the affected evidence files are
# marked "ocr_unavailable" / "pdf_unavailable" instead of aborting the run.
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "tesseract")
PDFTOTEXT_CMD = os.environ.get("PDFTOTEXT_CMD", "pdftotext")

# Resource identifiers are "<prefix>_<number>". The prefix identifies the kind of record and,
# by default, the owning system. The activity log (see store.build_system_map) takes
# precedence over this table whenever it names a target system for the resource.
PREFIX_TO_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    # Fallbacks below are inferred from evidence origins and activity-log system names;
    # they are only used when the activity log has no entry for the specific resource.
    "legal_hold": "security_service",
    "email_delivery": "email_delivery_service",
    "cache": "cache_service",
    "object": "object_storage",
    "marketing": "marketing_platform",
}

# Default record category per prefix. Prefixes shared by several categories
# (support, analytics) are refined in normalize.Normalizer.category_for.
PREFIX_TO_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_MESSAGE",
    "export": "USER_EXPORT",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "cache": "CACHE_ENTRY",
    "object": "SUPPORT_ATTACHMENT",
    "marketing": "MARKETING_PROFILE",
}
# Prefixes whose owning system is fixed by the data model. For these the id prefix is
# authoritative; the activity log is not, because it contains events logged against
# unrelated systems (e.g. data flows recorded under the search service).
CORE_PREFIXES = {"account", "search", "analytics", "support", "export","data_flow", "backup", "audit_record", "tombstone"}

# Longest prefixes first so "audit_record" is never shadowed by a shorter alternative.
_PREFIXES = sorted(PREFIX_TO_SYSTEM, key=len, reverse=True)
# Matches any resource id (for example 'account_5684') inside free text such as logs or OCR output.
RESOURCE_RE = re.compile(r"\b(?:" + "|".join(_PREFIXES) + r")_\d+\b")


def prefix_of(resource_id: str) -> str:
    """Return the prefix of a resource id ('audit_record_12' -> 'audit_record')."""
    return resource_id.rsplit("_", 1)[0]