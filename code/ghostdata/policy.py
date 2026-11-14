"""Record state and deletion-rule policy.

Role in the pipeline
--------------------
Two responsibilities, both pure functions of a normalised record:

  1. STATE  - was a record actually present / active / restorable at the time it was
              captured?  Evidence is trusted over worker "reported_result" claims.
  2. POLICY - how does deletion_rules.csv translate (category, state, exposure) into the
              finding / remediation / verification operations?

Category groups follow deletion_rules.csv:
  DELETE     USER_PROFILE, SECONDARY_IDENTIFIER, SEARCH_INDEX_ENTRY, CACHE_ENTRY,
             USER_EXPORT, MARKETING_PROFILE, SUPPORT_ATTACHMENT
  REDACT     SUPPORT_MESSAGE, EMAIL_DELIVERY_LOG, ACCOUNT_TOMBSTONE
  ANONYMIZE  ANALYTICS_PROFILE, PRODUCT_TELEMETRY
  RETAIN     SECURITY_AUDIT_LOG (within retention), LEGAL_HOLD_RECORD (active hold)
  DATA_FLOW / BACKUP_ARCHIVE are handled as "disable" / "block restore" risks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .io_utils import try_parse_ts
from .models import NormRecord

DELETE_CATEGORIES = {"USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
                     "USER_EXPORT", "MARKETING_PROFILE", "SUPPORT_ATTACHMENT"}
REDACT_CATEGORIES = {"SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE"}
ANONYMIZE_CATEGORIES = {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}
RETAIN_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}
FLOW_CATEGORY = "DATA_FLOW"
BACKUP_CATEGORY = "BACKUP_ARCHIVE"

# Words found in state fields (result / state / status ...) and how they are read:
# ABSENT = the record does not exist; PRESENT = it exists; RELEASED = a legal hold is no longer in force;
# ACTIVE / INACTIVE = a data flow can / cannot copy data.
ABSENT_TOKENS = {"absent", "deleted", "not_found", "notfound", "missing", "none", "miss", "removed",
                 "purged", "gone", "not found", "no_match", "null", "false"}
PRESENT_TOKENS = {"found", "active", "present", "identifiable", "exists", "hit", "retained",
                  "enabled", "true", "ok"}
RELEASED_TOKENS = {"released", "expired", "inactive", "closed", "lifted", "revoked"}
ACTIVE_TOKENS = {"active", "enabled", "running", "on", "true", "syncing"}
INACTIVE_TOKENS = {"disabled", "paused", "inactive", "stopped", "off", "false", "blocked"}
# State fields that count as a real observation of a resource; a log line with none of them says nothing.
SIGNAL_KEYS = ("http_status", "rows", "result", "state", "account_status", "status", "enabled",
               "restore_allowed", "download_available", "archive_present", "marker_found",
               "profile_present", "last_delivery_status")
# Restore and flow flags that only appear inside the free text of a log line.
LOG_SIGNAL_RE = re.compile(r"\b(eligible|deletion_block|restore_allowed|enabled)\s*[=:]", re.I)


@dataclass(frozen=True)
class Decision:
    """The three operations (any may be None) a policy decision produces for one resource."""
    finding: str | None
    remediation: str | None
    verify: str


def _token(value) -> str:
    """Lower-cased, stripped string form of a state value."""
    return str(value).strip().lower()


def informative(record: NormRecord) -> bool:
    """Log lines only describe a resource's state when they carry a state signal (a known
    state field or an eligible / deletion_block / enabled flag); a bare mention is not an
    observation.  Other kinds always count."""
    if record.file_kind == "log":
        return (any(key in record.state for key in SIGNAL_KEYS) or bool(LOG_SIGNAL_RE.search(record.blob))
                or bool(record.emails or record.hashes or record.phones or record.user_ids))
    return True


def flag(record: NormRecord, key: str) -> bool | None:
    """Boolean field `key` from the record state, falling back to the record text."""
    value = record.state.get(key)
    if isinstance(value, bool):
        return value
    if value is not None:
        token = _token(value)
        if token in ("true", "yes", "1"):
            return True
        if token in ("false", "no", "0"):
            return False
    match = re.search(rf'{key}"?\s*[:=]\s*"?(true|false|yes|no)', record.blob, re.I)
    if match:
        return match.group(1).lower() in ("true", "yes")
    return None


def record_present(record: NormRecord, category: str | None = None) -> bool:
    """Does the record show that the resource exists?  404 / zero rows / a miss means absent;
    structured snapshot rows with no explicit signal count as present.  For an account
    tombstone `account_status: DELETED` describes the ACCOUNT, not the tombstone, so it is
    ignored.  `marker_found=false` means no deletion marker exists, i.e. the record is still there."""
    state = record.state
    # The evidence is checked from the most direct signal to the weakest; the first one present decides.
    if "profile_present" in state:
        return bool(state["profile_present"])
    # A 404 or other error status means the lookup found nothing.
    if "http_status" in state:
        return int(state["http_status"]) < 400
    # Zero rows returned means no record.
    if "rows" in state:
        return int(state["rows"]) > 0
    # Exports and archives are present while a download or archive is still available.
    availability = [bool(state[k]) for k in ("download_available", "archive_present") if k in state]
    if availability:
        return any(availability)
    # marker_found=false means no deletion marker was written, so the record is still there (inverted).
    if "marker_found" in state:
        return not bool(state["marker_found"])
    # For a tombstone, account_status describes the deleted ACCOUNT, not the tombstone, so it is ignored.
    keys = ("result", "state", "status") if category == "ACCOUNT_TOMBSTONE" else (
        "result", "state", "account_status", "status")
    # Last resort: read textual states such as 'NOT_FOUND' or 'ACTIVE'.
    for key in keys:
        if key in state:
            token = _token(state[key])
            if token in ABSENT_TOKENS:
                return False
            if token in PRESENT_TOKENS:
                return True
    # No explicit signal: a structured snapshot row with no negative marker counts as present.
    return True


def flow_active(record: NormRecord) -> bool:
    """Is the data flow able to copy or recreate data?"""
    enabled = flag(record, "enabled")
    if enabled is not None:
        return enabled
    for key in ("state", "status", "result"):
        if key in record.state:
            token = _token(record.state[key])
            if token in ACTIVE_TOKENS:
                return True
            if token in INACTIVE_TOKENS:
                return False
    return False


def restore_allowed(record: NormRecord) -> bool:
    """Can the backup still restore data?  Explicit restore_allowed wins; otherwise a
    candidate that is eligible and not blocked by a deletion block can be restored."""
    allowed = flag(record, "restore_allowed")
    if allowed is not None:
        return allowed
    eligible = flag(record, "eligible")
    blocked = flag(record, "deletion_block")
    if eligible is not None or blocked is not None:
        return (True if eligible is None else eligible) and not bool(blocked)
    for key in ("state", "status", "result"):
        if key in record.state:
            token = _token(record.state[key])
            if token in ("allowed", "active", "enabled", "true"):
                return True
            if token in INACTIVE_TOKENS:
                return False
    return False


def retained(records: list[NormRecord], category: str, audit_at: datetime) -> bool:
    """Is a SECURITY_AUDIT_LOG / LEGAL_HOLD_RECORD still inside its retention / hold?"""
    for record in records:
        # A legal hold stays in force as long as some observation is not marked 'released'.
        if category == "LEGAL_HOLD_RECORD":
            token = _token(record.state.get("status", record.state.get("state", "")))
            if token in RELEASED_TOKENS:
                continue
            return True
        # Audit logs are retained while retention_until is still in the future at audit time (or when no end date is known).
        until = record.state.get("retention_until")
        if until is None:
            return True
        if isinstance(until, str):
            until = try_parse_ts(until)
        if until is None:
            return True
        try:
            if until > audit_at:
                return True
        except TypeError:
            return True
    return False


def decide(category: str, *, present: bool, exposed: bool, risk: bool) -> Decision | None:
    """Translate (category, state) into operations according to deletion_rules.csv.

    present  the record exists in the observed state
    exposed  it still holds an identifier it must not (direct id, or user_id for anonymize)
    risk     for DATA_FLOW: flow is active; for BACKUP_ARCHIVE: restore is allowed
    """
    # DELETE rules: if the record still exists, report and delete it; otherwise only verify that it is gone.
    if category in DELETE_CATEGORIES:
        if present:
            return Decision("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
        return Decision(None, None, "VERIFY_RECORD_ABSENT")
    # REDACT rules: the record stays; it is a violation only while a direct identifier is still exposed.
    if category in REDACT_CATEGORIES:
        if not present:
            return None
        if exposed:
            return Decision("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
        return Decision(None, None, "VERIFY_RECORD_REDACTED")
    # ANONYMIZE rules: the record stays but must no longer be linkable to the user (a user_id reference also counts as exposure).
    if category in ANONYMIZE_CATEGORIES:
        if not present:
            return None
        if exposed:
            return Decision("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
        return Decision(None, None, "VERIFY_RECORD_ANONYMIZED")
    # Data flows: an active flow can recreate deleted data, so it must be disabled.
    if category == FLOW_CATEGORY:
        if risk:
            return Decision("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")
        return Decision(None, None, "VERIFY_DATA_FLOW_DISABLED")
    # Backups are kept, but their restore path must be blocked while restoring is still allowed.
    if category == BACKUP_CATEGORY:
        if risk:
            return Decision("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
        return Decision(None, None, "VERIFY_RESTORE_BLOCKED")
    # RETAIN rules: audit logs and legal holds must stay; the only check is that they are still present.
    if category in RETAIN_CATEGORIES:
        return Decision(None, None, "VERIFY_RECORD_PRESENT")
    return None