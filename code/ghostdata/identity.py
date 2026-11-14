"""Identity resolution.

Role in the pipeline
--------------------
Builds a directory of every identifier a customer has ever had (current values from
user_identities.csv plus old/new values from user_audit_log.csv) and answers two questions:

  1. resolve(email)  - which user(s) does a deletion request's email belong to, including
                       requests made with an *old* address?
  2. owners(record)  - which users does an evidence record belong to?  A record is tied to a
                       user either by a DIRECT identifier (email, email hash, phone) or by a
                       stable user_id REFERENCE.  The distinction matters for policy: a
                       redacted/anonymized record may keep a user_id reference but must not
                       keep a direct identifier.

Matching is exact (lower-cased email, lower-cased hash, digits-only phone), so look-alike
addresses such as g.silva76jr@... never match g.silva76@...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .io_utils import read_csv
from .models import NormRecord

MIN_PHONE_DIGITS = 7   # shorter digit strings are too ambiguous to identify anybody


def digits_only(value: str | None) -> str:
    """Return only the digits of a phone-like string ('+971 50 482 7316' -> '971504827316')."""
    return re.sub(r"\D", "", value or "")


@dataclass
class Identity:
    """Everything known about one user_id, current and historical."""
    user_id: str
    name: str = ""
    current_email: str = ""
    emails: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    audit_ids: list[str] = field(default_factory=list)


class IdentityDirectory:
    """Lookup structure over all users' identifiers."""

    def __init__(self) -> None:
        """Create empty indexes; IdentityDirectory.load() fills them from the two identity files."""
        self.users: dict[str, Identity] = {}
        self.by_email: dict[str, set[str]] = {}
        self.by_hash: dict[str, set[str]] = {}
        self.by_phone: dict[str, set[str]] = {}
        self._owner_cache: dict[int, tuple[frozenset[str], frozenset[str]]] = {}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls) -> "IdentityDirectory":
        """Read user_identities.csv and user_audit_log.csv and build the directory."""
        directory = cls()
        for row in read_csv("user_identities.csv"):
            user_id = row["user_id"].strip()
            identity = directory.users.setdefault(user_id, Identity(user_id))
            identity.name = (row.get("name") or "").strip()
            identity.current_email = (row.get("email") or "").strip().lower()
            directory._add(identity, "email", row.get("email"))
            directory._add(identity, "email_hash", row.get("email_hash"))
            directory._add(identity, "phone", row.get("phone"))
        for row in read_csv("user_audit_log.csv"):
            user_id = row["user_id"].strip()
            identity = directory.users.setdefault(user_id, Identity(user_id))
            field_name = (row.get("field_name") or "").strip().lower()
            for value in (row.get("old_value"), row.get("new_value")):
                directory._add(identity, field_name, value)
            identity.audit_ids.append(row["audit_id"])
        directory._index()
        return directory

    @staticmethod
    def _add(identity: Identity, field_name: str, value: str | None) -> None:
        """Normalise one identifier value and store it in the matching set of `identity`."""
        value = (value or "").strip()
        if not value:
            return
        if field_name == "email":
            identity.emails.add(value.lower())
        elif field_name == "email_hash":
            identity.hashes.add(value.lower())
        elif field_name == "phone":
            digits = digits_only(value)
            if len(digits) >= MIN_PHONE_DIGITS:
                identity.phones.add(digits)

    def _index(self) -> None:
        """Build identifier -> user_id reverse maps used for record ownership."""
        for identity in self.users.values():
            for email in identity.emails:
                self.by_email.setdefault(email, set()).add(identity.user_id)
            for value in identity.hashes:
                self.by_hash.setdefault(value, set()).add(identity.user_id)
            for phone in identity.phones:
                self.by_phone.setdefault(phone, set()).add(identity.user_id)

    # ------------------------------------------------------------------ queries
    def resolve(self, email: str) -> list[Identity]:
        """Return the users a request email belongs to.  Users whose *current* email equals
        the request come first; otherwise users that held it historically."""
        key = (email or "").strip().lower()
        # Prefer users whose CURRENT email is the request email; fall back to users who held that address in the past.
        current = [u for u in self.users.values() if u.current_email == key]
        if current:
            return current
        return [self.users[uid] for uid in sorted(self.by_email.get(key, ()))]

    def owners(self, record: NormRecord) -> tuple[frozenset[str], frozenset[str]]:
        """Return (direct_owners, reference_owners) of a record.

        direct_owners     users matched through email / email hash / phone found in the record
        reference_owners  users whose user_id is referenced by the record
        """
        # Ownership of a record never changes during a run, so the answer is cached per record.
        cached = self._owner_cache.get(record.record_id)
        if cached is not None:
            return cached
        direct: set[str] = set()
        for email in record.emails:
            direct |= self.by_email.get(email.lower(), set())
        for value in record.hashes:
            direct |= self.by_hash.get(value.lower(), set())
        # Phones are compared as digit strings, so '+971 50 482 7316' and '971504827316' match.
        for phone in record.phones:
            digits = digits_only(phone)
            if len(digits) >= MIN_PHONE_DIGITS:
                direct |= self.by_phone.get(digits, set())
        # A user_id mentioned in the record is a REFERENCE, not a direct identifier (see the module docstring).
        refs = {uid for uid in record.user_ids if uid in self.users}
        result = (frozenset(direct), frozenset(refs))
        self._owner_cache[record.record_id] = result
        return result