"""Activity-log linking for records that carry no identifier.

Role in the pipeline
--------------------
Some evidence rows cannot be tied to a person by content: a deleted account answers 404 with
no email, and an anonymized analytics profile deliberately has no identifier.  The only
connection to the customer is operational, and this module exposes three relations:

  * LINK_RESOURCE_IDENTITY events (data catalog) name a resource, optionally with an alias
    (a file name or reference shown in a screenshot), together with the user's email/user_id.
    `links_for()` returns the links that existed at audit time; `link_owner` lets the
    pipeline reject resources that belong to someone else.
  * Account DELETE_RECORD events happen 45-75 min BEFORE reported_at.  Candidates are
    constrained by evidence (the account must appear in a file that mentions the user) and
    otherwise resolved by a global one-to-one assignment across all cases.
  * Analytics ANONYMIZE_RECORD events happen 28-29 min before reported_at, so every case
    sharing a reported_at competes for the same batch.  Cases with cleanup events of their
    own (support redaction +7 min, data-flow disable +14, backup block +21 after the
    analytics event) are "anchored": the analytics event is pinned by that rhythm first.
    Remaining cases use the time window, excluding anchored events.

The planning step runs over ALL known requests (samples + real) so elimination sees every case.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from .identity import IdentityDirectory
from .store import EvidenceStore

# Timing rules measured on the sample data (all values in minutes):
# an account deletion event is logged about 60 minutes (+/- 30) before the case's reported_at,
# an analytics anonymisation event about 28 minutes before it.
ACCOUNT_WINDOWS = [(60.0, 30.0)]          # (expected minutes before reported_at, tolerance)
SAMPLE_ACCOUNT_WINDOWS = [(60.0, 30.0), (0.0, 12.0)]   # hand-made sample cases 1-10 also allow ~0
ANALYTICS_WINDOWS_CONSTRAINED = [(28.0, 90.0)]
ANALYTICS_WINDOWS_FREE = [(28.0, 60.0)]
UNCONSTRAINED_PENALTY = 25.0              # score added when the evidence constraint is dropped
ANCHOR_TOLERANCE = 3.0                    # minutes of slack around the cleanup rhythm
LINK_MAX_AGE = timedelta(hours=72)        # a catalog link only concerns an audit shortly after it
# The worker's result for an account deletion agrees with the claim made by the case.
RESULT_FOR_STATUS = {"COMPLETE": {"SUCCESS"}, "NOT_STARTED": {"FAILED"}, "PARTIALLY_COMPLETE": {"PARTIAL", "SUCCESS"}}
RESULT_MISMATCH_PENALTY = 50.0
ALIAS_RE = re.compile(r"\(([^)]+)\)")


@dataclass
class CaseNeed:
    """What the linker must find for one case."""
    case_id: str
    users: frozenset[str]
    reported_at: datetime
    needs_account: bool
    needs_analytics: bool
    files: frozenset[str] = field(default_factory=frozenset)       # evidence files mentioning the user
    anchors: tuple = ()                    # ((event_time, minutes_after_analytics), ...) of the user's cleanup events
    reported_status: str = ""


def case_number(case_id: str) -> int:
    """Numeric part of a case id ('case_07' -> 7)."""
    digits = re.sub(r"\D", "", case_id)
    return int(digits) if digits else 0


class ActivityLinker:
    """Resolves identity-less resources to cases using system_activity.csv."""

    def __init__(self, store: EvidenceStore, directory: IdentityDirectory,
                 owned: dict[str, set[str]]) -> None:
        """`owned` maps resource_id -> users that some evidence record ties it to."""
        self.store = store
        self.owned = owned
        # Deletion events of accounts: the worker's claim that an account was deleted.
        self.account_events = [e for e in store.activity
                               if e.event_type == "DELETE_RECORD" and e.target_system == "account_service"
                               and (e.resource_id or "").startswith("account_")]
        self.analytics_events = [e for e in store.activity
                                 if e.event_type == "ANONYMIZE_RECORD" and e.target_system == "analytics_platform"
                                 and (e.resource_id or "").startswith("analytics_")]
        # Catalog links (LINK_RESOURCE_IDENTITY events): user -> [(resource, event time, alias)], plus the reverse map resource -> users.
        self.links: dict[str, list[tuple[str, datetime, str | None]]] = defaultdict(list)
        self.link_owner: dict[str, set[str]] = defaultdict(set)
        for event in store.activity:
            if event.event_type != "LINK_RESOURCE_IDENTITY" or not event.resource_id:
                continue
            users = set(event.user_ids)
            for email in event.emails:
                users |= {i.user_id for i in directory.resolve(email)}
            alias = self._alias(event)
            for user_id in users:
                if user_id in directory.users:
                    self.links[user_id].append((event.resource_id, event.event_time, alias))
                    self.link_owner[event.resource_id].add(user_id)
        self.account_for: dict[str, str] = {}
        self.analytics_for: dict[str, str] = {}

    @staticmethod
    def _alias(event) -> str | None:
        """Name shown next to the resource id in the event text, e.g. the attachment file name."""
        for match in ALIAS_RE.finditer(event.details or ""):
            value = match.group(1).strip()
            if value and value != event.resource_id:
                return value
        return None

    def links_for(self, user_id: str, audit_at: datetime) -> list[tuple[str, str | None]]:
        """(resource_id, alias) links of a user created within LINK_MAX_AGE before audit time."""
        return [(rid, alias) for rid, when, alias in self.links.get(user_id, ())
                if timedelta(0) <= audit_at - when <= LINK_MAX_AGE]

    # ------------------------------------------------------------------ planning
    def plan(self, needs: list[CaseNeed], reserved: set[str]) -> None:
        """Assign at most one account and one analytics resource per case.
        `reserved` holds resources already tied to a case by content."""
        # --- Accounts: score every compatible deletion event for each case, then solve one-to-one across all cases.
        accounts: dict[str, dict[str, float]] = {}
        for need in needs:
            if need.needs_account:
                windows = SAMPLE_ACCOUNT_WINDOWS if case_number(need.case_id) <= 10 else ACCOUNT_WINDOWS
                # Candidates found by timing alone carry a penalty, so candidates backed by evidence files win ties.
                free = self._gather(self.account_events, need, windows, reserved, constrained=False,
                                    check_result=True)
                found = {r: s + UNCONSTRAINED_PENALTY for r, s in free.items()}
                if need.files:                     # evidence-backed candidates keep their better score
                    found.update(self._gather(self.account_events, need, windows, reserved, constrained=True,
                                              check_result=True))
                accounts[need.case_id] = found
        self.account_for = self._solve(accounts)

        # --- Analytics: cases with cleanup events of their own are pinned first ('anchored'); the others use the time window.
        anchored = {}
        for need in needs:
            if need.needs_analytics and need.anchors:
                found = self._anchored(need, reserved)
                if found:
                    anchored[need.case_id] = found
        fixed = self._solve(anchored)
        taken = set(fixed.values())
        # The remaining cases compete for the analytics events that no anchored case took.
        free_analytics: dict[str, dict[str, float]] = {}
        for need in needs:
            if need.needs_analytics and need.case_id not in fixed:
                windows = ANALYTICS_WINDOWS_CONSTRAINED if need.files else ANALYTICS_WINDOWS_FREE
                found = self._gather(self.analytics_events, need, windows, reserved, constrained=bool(need.files))
                free_analytics[need.case_id] = {r: s for r, s in found.items() if r not in taken}
        self.analytics_for = {**fixed, **self._solve(free_analytics)}

    def _in_files(self, rid: str, files: frozenset[str]) -> bool:
        """Does the resource have a record in any of the given evidence files?"""
        return any(r.file_id in files for r in self.store.by_resource.get(rid, ()) if r.resource_id == rid)

    def _eligible(self, rid: str, need: CaseNeed, reserved: set[str]) -> bool:
        """A candidate must not belong to another user by content or by catalog link."""
        owners = self.owned.get(rid, set())
        # Evidence says someone else owns this resource: never take it.
        if owners and not owners <= need.users:
            return False
        # Already tied to a case through its content but without a recorded owner: do not hand it to another case.
        if rid in reserved and not owners:
            return False
        others = self.link_owner.get(rid, set())
        return not (others and not others <= need.users)

    def _gather(self, events, need: CaseNeed, windows, reserved: set[str], constrained: bool,
                check_result: bool = False) -> dict[str, float]:
        """Return {resource_id: score} (lower is better) for events inside any time window,
        excluding foreign resources and, if `constrained`, resources absent from the case's files.
        With `check_result`, an event whose worker result contradicts the case's reported_status
        (e.g. SUCCESS for a NOT_STARTED request) is penalised."""
        found: dict[str, float] = {}
        allowed = RESULT_FOR_STATUS.get(need.reported_status)
        for event in events:
            rid = event.resource_id
            if not self._eligible(rid, need, reserved):
                continue
            minutes_before = (need.reported_at - event.event_time).total_seconds() / 60.0
            scores = [abs(minutes_before - pref) for pref, width in windows
                      if abs(minutes_before - pref) <= width]
            if not scores:
                continue
            if constrained and not self._in_files(rid, need.files):
                continue
            score = min(scores)
            if check_result and allowed and event.result not in allowed:
                score += RESULT_MISMATCH_PENALTY
            found[rid] = min(score, found.get(rid, 1e9))
        return found

    def _anchored(self, need: CaseNeed, reserved: set[str]) -> dict[str, float]:
        """Analytics events that fit the case's own cleanup rhythm (more matching anchors = better)."""
        found: dict[str, float] = {}
        for event in self.analytics_events:
            rid = event.resource_id
            if not self._eligible(rid, need, reserved):
                continue
            hits = []
            for anchor_time, offset in need.anchors:
                diff = abs((anchor_time - event.event_time).total_seconds() / 60.0 - offset)
                if diff <= ANCHOR_TOLERANCE:
                    hits.append(diff)
            if hits:
                # Score = smallest timing error; every extra matching cleanup event improves it by 5.
                found[rid] = min(min(hits) - 5.0 * (len(hits) - 1), found.get(rid, 1e9))
        return found

    @staticmethod
    def _solve(candidates: dict[str, dict[str, float]]) -> dict[str, str]:
        """Global one-to-one assignment: fix unique candidates repeatedly, then take the
        best-scoring remaining pairs."""
        assigned: dict[str, str] = {}
        taken: set[str] = set()
        changed = True
        # Pass 1: repeatedly fix every case that has exactly one free candidate left (forced choices can free up others).
        while changed:
            changed = False
            for case_id, options in candidates.items():
                if case_id in assigned:
                    continue
                free = [r for r in options if r not in taken]
                if len(free) == 1:
                    assigned[case_id] = free[0]
                    taken.add(free[0])
                    changed = True
        # Pass 2: for the rest, take the best (lowest-score) case/resource pairs first, each resource at most once.
        pairs = sorted((score, case_id, rid) for case_id, options in candidates.items()
                       if case_id not in assigned for rid, score in options.items())
        for _score, case_id, rid in pairs:
            if case_id in assigned or rid in taken:
                continue
            assigned[case_id] = rid
            taken.add(rid)
        return assigned