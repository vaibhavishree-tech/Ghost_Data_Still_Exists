"""Step ordering and serialisation.

Role in the pipeline
--------------------
Turns the unordered (operation, system, resource, evidence) tuples produced by the pipeline
into the exact `steps_json` structure required by the task: duplicates removed, steps sorted
by phase -> operation order (the order of the task's operation table) -> system_id ->
resource_id (ascending string compare), then numbered from 1.
"""
from __future__ import annotations

# (phase, operation, state) in the order given by the problem statement's operation table.
OPERATION_TABLE = [
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
]
OP_PHASE = {op: phase for phase, op, _ in OPERATION_TABLE}
OP_STATE = {op: state for _, op, state in OPERATION_TABLE}
OP_RANK = {op: rank for rank, (_, op, _) in enumerate(OPERATION_TABLE)}   # table order encodes phase order too


def build_steps(raw: list[tuple[str, str, str, str]]) -> list[dict]:
    """Deduplicate, order and number steps.

    `raw` items are (operation, system_id, resource_id, evidence_id).  Two items with the same
    (operation, system, resource) collapse into one; a non-empty evidence id wins.
    """
    unique: dict[tuple[str, str, str], str] = {}
    for operation, system_id, resource_id, evidence_id in raw:
        # Identity of a step: the same operation on the same resource of the same system is one step.
        key = (operation, system_id, resource_id)
        if key not in unique or (not unique[key] and evidence_id):
            unique[key] = evidence_id
    # Required order: position in the operation table (which already encodes the phase), then system_id, then resource_id.
    ordered = sorted(unique.items(), key=lambda kv: (OP_RANK[kv[0][0]], kv[0][1], kv[0][2]))
    steps = []
    for number, ((operation, system_id, resource_id), evidence_id) in enumerate(ordered, 1):
        steps.append({
            "step_no": number,
            "phase": OP_PHASE[operation],
            "system_id": system_id,
            "resource_id": resource_id,
            "operation": operation,
            "state": OP_STATE[operation],
            "evidence_id": evidence_id,
        })
    return steps