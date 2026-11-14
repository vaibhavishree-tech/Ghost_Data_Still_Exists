"""The audit engine: one deletion request in, one (status, steps) answer out.

Role in the pipeline
--------------------
For every request the engine
  1. resolves the requester's identities (current + historical),
  2. finds every evidence record tied to them (direct identifier or user_id reference) that
     was VISIBLE at audit_at - snapshot/export/document files by the file's capture time,
     log lines by their own timestamp,
  3. adds resources the content alone cannot tie: catalog LINK events that existed at audit
     time (matched to a screenshot/PDF by resource id or alias; other rows of that document
     are decoys and are dropped), and identity-less accounts/analytics through the
     ActivityLinker - never taking a resource that another user owns,
  4. decides NOT_APPLICABLE when the privacy intake classified the request as non-deletion,
  5. determines each resource's state at audit time - the latest observation wins, and a
     later snapshot whose declared scope covers the resource's system but does not list the
     resource is proof of absence (scope-aware absence),
  6. determines the resource's category from the record (declared category, log event type,
     document file name) before falling back to the id prefix,
  7. applies deletion_rules.csv per resource (policy.decide) and
  8. assembles ordered steps: COMPLETE = verification steps citing evidence;
     INCOMPLETE = findings (cited) + remediation + verification (empty evidence).

Anonymized analytics profiles without identifiers are only linked for cases that would
otherwise be COMPLETE (in the samples they never occur in INCOMPLETE cases).
The engine is deterministic and uses no LLM.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import policy
from .config import PREFIX_TO_CATEGORY, PREFIX_TO_SYSTEM, prefix_of
from .identity import IdentityDirectory
from .io_utils import parse_ts
from .linker import ActivityLinker, CaseNeed
from .models import NormRecord
from .steps import build_steps
from .store import EvidenceStore

# Log events named attachment_* identify a support ATTACHMENT (a deletable file) rather than a support message.
ATTACHMENT_EVENT_RE = re.compile(r'event"?\s*[=:]\s*"?attachment_', re.I)
RETAINED_PREFIXES = ("audit_record", "legal_hold")   # retained facts: visible at any capture time
# cleanup events of a cleaned case follow the analytics anonymisation by a fixed rhythm (minutes)
ANCHOR_EVENTS = {"support": ("REDACT_RECORD", 7.0),
                 "data_flow": ("DISABLE_DATA_FLOW", 14.0),
                 "backup": ("BLOCK_RESTORE", 21.0)}


@dataclass
class Obs:
    """The observed state of one resource: the records of the winning observation."""
    records: list[NormRecord]
    file_id: str | None
    time: datetime | None
    category: str | None = None
    via_scope: bool = False        # absence proven by a scope-declaring snapshot
    assumed_present: bool = False  # catalog link with no readable observation

    @property
    def present(self) -> bool:
        """True when any copy in the observation shows the resource exists."""
        return self.assumed_present or any(policy.record_present(r, self.category) for r in self.records)


@dataclass
class Treatment:
    """Operations decided for one resource."""
    resource_id: str
    system_id: str
    category: str
    decision: policy.Decision
    evidence: str
    how: str = ""


@dataclass
class CaseState:
    """Everything prepared about one request before the policy is applied."""
    case_id: str
    email: str
    users: frozenset[str]
    audit_at: datetime
    reported_at: datetime
    reported_status: str = ""
    tied: dict[str, str] = field(default_factory=dict)             # resource_id -> how it was tied
    doc_tied: dict[str, NormRecord] = field(default_factory=dict)  # resources only seen in PDFs/screenshots
    user_files: set[str] = field(default_factory=set)              # visible files mentioning the user
    primary_file: str | None = None
    not_applicable: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    """Final answer for one case plus diagnostics."""
    case_id: str
    status: str
    steps: list[dict]
    notes: list[str]


class AuditEngine:
    """Deterministic deletion-audit engine over a loaded EvidenceStore."""

    def __init__(self, store: EvidenceStore, directory: IdentityDirectory) -> None:
        """Precompute record ownership, the user->records index and the activity linker."""
        self.store = store
        self.directory = directory
        self.user_records: dict[str, list[NormRecord]] = defaultdict(list)
        self.owned: dict[str, set[str]] = defaultdict(set)
        for record in store.records:
            direct, refs = directory.owners(record)
            users = direct | refs
            for user_id in users:
                self.user_records[user_id].append(record)
            if record.resource_id and users:
                self.owned[record.resource_id] |= users
        self.doc_records = [r for r in store.records if r.resource_id is None]
        self.events_by_resource = defaultdict(list)
        for event in store.activity:
            self.events_by_resource[event.resource_id].append(event)
        self.classify_events = [e for e in store.activity if e.event_type == "CLASSIFY_REQUEST"]
        self.linker = ActivityLinker(store, directory, self.owned)
        self.states: dict[str, CaseState] = {}

    # ------------------------------------------------------------------ visibility
    def _eff_time(self, record: NormRecord) -> datetime:
        """Time at which a record was observed: a log line's own timestamp, otherwise the
        capture time of the file that contains it (section times inside a bundle do not count)."""
        if record.file_kind == "log":
            return record.observed_at or record.captured_at
        return self.store.files[record.file_id].captured_at

    def _visible(self, record: NormRecord, audit_at: datetime) -> bool:
        """Was the record observable at audit time?"""
        return self._eff_time(record) <= audit_at

    def _section_time(self, record: NormRecord) -> datetime:
        """Time used to pick the newest section/line inside one file."""
        if record.file_kind == "log":
            return record.observed_at or record.captured_at
        return record.captured_at

    def _doc_for(self, rid: str, audit_at: datetime, alias: str | None = None) -> NormRecord | None:
        """Newest visible document-level record (PDF / screenshot) that lists `rid` or its alias."""
        alias = alias.lower() if alias else None
        docs = [r for r in self.doc_records if self._visible(r, audit_at)
                and (rid in r.doc_resource_ids or (alias and alias in r.blob.lower()))]
        return max(docs, key=lambda r: (r.captured_at, r.file_id)) if docs else None

    # ------------------------------------------------------------------ preparation
    def prepare(self, rows: list[dict]) -> None:
        """Prepare every request, run a provisional audit to learn which cases would be COMPLETE,
        then run the global activity linking over all of them."""
        for row in rows:
            state = self._prepare_case(row)
            self.states[state.case_id] = state
        # Cases with a resolved user that the intake did not already classify as non-deletion.
        active = [s for s in self.states.values() if s.users and not s.not_applicable]
        reserved: set[str] = set()
        for state in active:
            reserved |= set(state.tied)
        needs: list[CaseNeed] = []
        for state in active:
            # A provisional audit shows which cases would be COMPLETE; only those get identity-less analytics linked.
            provisional, _ = self._evaluate(state)
            has_analytics = any(prefix_of(r) == "analytics" for r in state.tied)
            needs.append(CaseNeed(
                case_id=state.case_id, users=state.users, reported_at=state.reported_at,
                needs_account=not any(prefix_of(r) == "account" for r in state.tied),
                needs_analytics=(not has_analytics) and provisional == "COMPLETE",
                files=frozenset(state.user_files),reported_status=state.reported_status, anchors=tuple(self._anchors(state))))
        self.linker.plan(needs, reserved)
        for case_id, rid in self.linker.account_for.items():
            self.states[case_id].tied.setdefault(rid, "activity")
        for case_id, rid in self.linker.analytics_for.items():
            self.states[case_id].tied.setdefault(rid, "activity")

    def _anchors(self, state: CaseState) -> list[tuple[datetime, float]]:
        """(event_time, minutes after the analytics event) of the cleanup events on the user's
        directly tied support / data-flow / backup resources."""
        anchors = []
        for rid, how in state.tied.items():
            spec = ANCHOR_EVENTS.get(prefix_of(rid))
            if spec and how == "direct":
                event_type, offset = spec
                anchors += [(e.event_time, offset) for e in self.events_by_resource.get(rid, ())
                            if e.event_type == event_type]
        return anchors

    def _foreign(self, rid: str, users: frozenset[str]) -> bool:
        """Does another user own this resource (by evidence content or catalog link)?"""
        owners = self.owned.get(rid, set())
        others = self.linker.link_owner.get(rid, set())
        return bool((owners and not owners <= users) or (others and not others <= users))

    def _prepare_case(self, row: dict) -> CaseState:
        """Resolve identities and collect the resources tied to the requester."""
        identities = self.directory.resolve(row["email"])
        users = frozenset(i.user_id for i in identities)
        state = CaseState(case_id=row["case_id"], email=row["email"].strip().lower(), users=users,
                          audit_at=parse_ts(row["audit_at"]), reported_at=parse_ts(row["reported_at"]),
                          reported_status=(row.get("reported_status") or "").strip().upper())
        if not users:
            state.notes.append("unresolved_email")
            return state
        # file_time remembers the newest visible time per file, used to choose the 'primary' evidence file.
        file_time: dict[str, datetime] = {}
        link_rids: list[tuple[str, str | None]] = []
        for user_id in users:
            for record in self.user_records.get(user_id, ()):
                # Evidence captured after audit_at must not influence the answer.
                if not self._visible(record, state.audit_at):
                    continue
                if record.resource_id:
                    state.tied.setdefault(record.resource_id, "direct")
                    state.user_files.add(record.file_id)
                    stamp = self._eff_time(record)
                    file_time[record.file_id] = max(stamp, file_time.get(record.file_id, stamp))
                else:
                    for rid in record.doc_resource_ids:
                        if not self._foreign(rid, users):
                            state.doc_tied.setdefault(rid, record)
            # Catalog links that existed at audit time tie resources (often screenshot rows) to the user.
            for rid, alias in self.linker.links_for(user_id, state.audit_at):
                if not (self.owned.get(rid) and not self.owned[rid] <= users):
                    state.tied.setdefault(rid, "link")
                    link_rids.append((rid, alias))
        # Screenshot decoys: a document that contains one of the user's linked resources shows
        # other people's rows too - only the linked resources of that document are kept.
        linked_docs: set[str] = set()
        for rid, alias in link_rids:
            doc = self._doc_for(rid, state.audit_at, alias)
            if doc is not None:
                state.doc_tied[rid] = doc
                linked_docs.add(doc.file_id)
        if linked_docs:
            linked = {rid for rid, _ in link_rids}
            for rid in list(state.doc_tied):
                if rid not in linked and state.doc_tied[rid].file_id in linked_docs:
                    del state.doc_tied[rid]
        for user_id in users:                      # audit logs / legal holds exist regardless of capture time
            for record in self.user_records.get(user_id, ()):
                if record.resource_id and prefix_of(record.resource_id) in RETAINED_PREFIXES:
                    state.tied.setdefault(record.resource_id, "retained")
        for rid in state.doc_tied:
            if rid not in state.tied and prefix_of(rid) in PREFIX_TO_CATEGORY:
                state.tied[rid] = "document"
        # Primary file = the newest visible file that mentions the user; its scope declarations later prove absence.
        if file_time:
            state.primary_file = max(file_time.items(), key=lambda kv: (kv[1], kv[0]))[0]
        emails = {e for i in identities for e in i.emails}
        # Ownership is judged over ALL evidence (even files captured after audit_at): the
        # intake classification is a fact about the request, not about the audit-time state.
        if any((self.owned.get(e.resource_id, set()) & users)
               or (set(e.user_ids) & users) or (set(e.emails) & emails)
               for e in self.classify_events):
            state.not_applicable = True
        return state

    # ------------------------------------------------------------------ per-case audit
    def _evaluate(self, state: CaseState) -> tuple[str, list[Treatment]]:
        """Treat every tied resource and derive the case status."""
        treatments = []
        for rid, how in sorted(state.tied.items()):
            treatment = self._treat(state, rid, how)
            if treatment:
                treatments.append(treatment)
        status = "INCOMPLETE" if any(t.decision.finding for t in treatments) else "COMPLETE"
        return status, treatments

    def audit(self, case_id: str) -> CaseResult:
        """Produce the final status and steps for one prepared case."""
        state = self.states[case_id]
        if state.not_applicable:
            return CaseResult(case_id, "NOT_APPLICABLE", [], ["classified_non_deletion"])
        status, treatments = self._evaluate(state)
        notes = list(state.notes)
        if not treatments:
            notes.append("no_resources")
        if status == "COMPLETE" and not treatments:
            notes.append("EMPTY_COMPLETE")
        notes += [f"{t.resource_id}/{t.how}/{t.category}/{t.evidence}" for t in treatments]
        raw = []
        for t in treatments:
            if t.decision.finding:
                raw.append((t.decision.finding, t.system_id, t.resource_id, t.evidence))
            if t.decision.remediation:
                raw.append((t.decision.remediation, t.system_id, t.resource_id, ""))
            raw.append((t.decision.verify, t.system_id, t.resource_id,
                        t.evidence if status == "COMPLETE" else ""))
        return CaseResult(case_id, status, build_steps(raw), notes)

    def _visible_records(self, state: CaseState, rid: str) -> list[NormRecord]:
        """Informative records of `rid` observable at audit time.  Retained records (audit
        logs, legal holds) fall back to later captures when no earlier one exists."""
        recs = [r for r in self.store.by_resource.get(rid, ())
                if r.resource_id == rid and policy.informative(r)]
        seen = [r for r in recs if self._visible(r, state.audit_at)]
        if prefix_of(rid) in RETAINED_PREFIXES:
            return seen or recs
        return seen

    def _category(self, rid: str, records: list[NormRecord], doc: NormRecord | None) -> str | None:
        """Category of a resource: log event type / document file name first (they refine the
        ambiguous prefixes), then the category stated or derived by the normaliser, then the prefix."""
        prefix = prefix_of(rid)
        blob = " ".join(r.blob.lower() for r in records)
        if prefix == "support":
            if ATTACHMENT_EVENT_RE.search(blob):
                return "SUPPORT_ATTACHMENT"
            if "legacy_contact_index" in blob or "deletion_marker_check" in blob:
                return "SECONDARY_IDENTIFIER"
            if doc is not None and "attachment" in str(self.store.files[doc.file_id].path).lower():
                return "SUPPORT_ATTACHMENT"
        if prefix == "analytics" and ("email_opt_in" in blob or "contact_lookup" in blob):
            return "MARKETING_PROFILE"
        declared = Counter(r.category for r in records if r.category)
        if declared:
            return declared.most_common(1)[0][0]
        return PREFIX_TO_CATEGORY.get(prefix)

    def _treat(self, state: CaseState, rid: str, how: str) -> Treatment | None:
        """Decide the treatment of one tied resource, or None when nothing can be said."""
        if PREFIX_TO_CATEGORY.get(prefix_of(rid)) is None:
            return None
        # Step 1: gather what was observable at audit time.
        records = self._visible_records(state, rid)
        doc = state.doc_tied.get(rid)
        if doc is None and not records:
            doc = self._doc_for(rid, state.audit_at)
        # Step 2: decide the category, because it selects the deletion rule that applies.
        category = self._category(rid, records, doc if not records else None)
        if category is None:
            return None
        system_id = self._system_of(rid)
        # Step 3: determine the state at audit time (latest observation, or proof of absence by scope).
        obs = self._observe(state, rid, category, system_id, records)
        if obs is None and doc is not None:
            obs = Obs([doc], doc.file_id, doc.captured_at, category)
        if obs is None:
            # No readable observation: an account without a profile row is treated as absent; a catalog-linked resource is assumed to remain.
            if category == "USER_PROFILE":
                obs = Obs([], self._fallback_evidence(state, system_id), None, category, True)
            elif how == "link":      # the catalog confirms the resource; no readable observation: assume it remains
                obs = Obs([], self._fallback_evidence(state, system_id), None, category, assumed_present=True)
            else:
                return None
        # Step 4: apply the policy for this category and build the treatment with its cited evidence.
        evidence = obs.file_id or state.primary_file or ""
        if category in policy.RETAIN_CATEGORIES:
            if not obs.records or not policy.retained(obs.records, category, state.audit_at):
                return None
            decision = policy.decide(category, present=True, exposed=False, risk=False)
        else:
            direct, ref = self._exposure(state, obs.records)
            exposed = direct or (ref and category in policy.ANONYMIZE_CATEGORIES)
            risk = False
            if category == policy.FLOW_CATEGORY:
                risk = any(policy.flow_active(r) for r in obs.records)
            elif category == policy.BACKUP_CATEGORY:
                risk = any(policy.restore_allowed(r) for r in obs.records)
            present = obs.present if category not in (policy.FLOW_CATEGORY, policy.BACKUP_CATEGORY) else True
            decision = policy.decide(category, present=present, exposed=exposed, risk=risk)
        if decision is None:
            return None
        return Treatment(rid, system_id, category, decision, evidence, how)

    # ------------------------------------------------------------------ state at audit time
    def _observe(self, state: CaseState, rid: str, category: str, system_id: str,
                 records: list[NormRecord]) -> Obs | None:
        """Latest observation of `rid` visible at audit time, overridden by scope-aware absence
        when the primary file declares authority over the system and does not list the resource."""
        latest: Obs | None = None
        if records:
            # Take the newest visible FILE that mentions the resource, then the newest section or line inside that file.
            groups: dict[str, list[NormRecord]] = defaultdict(list)
            for record in records:
                groups[record.file_id].append(record)
            file_id, group = max(groups.items(), key=lambda kv: (max(self._eff_time(r) for r in kv[1]), kv[0]))
            newest = max(self._section_time(r) for r in group)
            group = [r for r in group if self._section_time(r) == newest]
            latest = Obs(group, file_id, self._eff_time(group[0]), category)
        # Scope-aware absence applies only to deletable records, data flows and backups: if the primary file declares authority
        # over the system, is newer than the last observation and does not list the resource, the resource is gone.
        scope_ok = (category in policy.DELETE_CATEGORIES
                    or category in (policy.FLOW_CATEGORY, policy.BACKUP_CATEGORY))
        if scope_ok and state.primary_file:
            primary = self.store.files[state.primary_file]
            scope_time = self._scope_time(primary, system_id)
            if scope_time is not None and scope_time <= state.audit_at and (
                    latest is None or (primary.file_id != latest.file_id and scope_time > latest.time)):
                return Obs([], primary.file_id, scope_time, category, True)
        return latest

    @staticmethod
    def _scope_time(evidence_file, system_id: str) -> datetime | None:
        """Capture time of the file's declared scope for `system_id`, or None if undeclared."""
        times = [s.captured_at or evidence_file.captured_at
                 for s in getattr(evidence_file, "scopes", ()) if s.system_id == system_id]
        return max(times) if times else None

    def _covers(self, file_id: str, system_id: str) -> bool:
        """Does the file declare an authoritative scope for the system?"""
        return self._scope_time(self.store.files[file_id], system_id) is not None

    def _fallback_evidence(self, state: CaseState, system_id: str) -> str:
        """Evidence id to cite when absence/presence is inferred without a record: the primary
        file if its scope covers the system, else the latest visible file whose origin names it."""
        if state.primary_file and self._covers(state.primary_file, system_id):
            return state.primary_file
        token = system_id.split("_")[0]
        candidates = [f for f in self.store.files.values()
                      if token in f.origin.lower() and f.captured_at <= state.audit_at]
        if candidates:
            return max(candidates, key=lambda f: (f.captured_at, f.file_id)).file_id
        return state.primary_file or ""

    # ------------------------------------------------------------------ helpers
    def _exposure(self, state: CaseState, records: list[NormRecord]) -> tuple[bool, bool]:
        """Return (has_direct_identifier, has_user_id_reference) for the requester."""
        direct = ref = False
        for record in records:
            d, r = self.directory.owners(record)
            direct = direct or bool(d & state.users)
            ref = ref or bool(r & state.users)
        return direct, ref

    def _system_of(self, rid: str) -> str:
        """Owning system of a resource: record-level system, then activity map, then prefix."""
        for record in self.store.by_resource.get(rid, ()):
            if record.resource_id == rid and record.system_id:
                return record.system_id
        return self.store.system_map.get(rid) or PREFIX_TO_SYSTEM.get(prefix_of(rid), "unknown_system")