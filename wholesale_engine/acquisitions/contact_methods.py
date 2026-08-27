"""Multiple phones and emails per owner, with provenance on each one.

Wave 5 stored one phone and one email per contact. Real skip traces return
three numbers and two addresses, of varying quality, from different dates —
and the whole point is knowing which one to try first.

Each :class:`ContactMethod` carries its own confidence, source and verified
date. Two rules govern how they change:

* **duplicates merge, they do not stack** — the same number from two sources
  is one record whose provenance improves
* **verified information is never silently overwritten** — a lower-confidence
  update to a verified method is recorded as a competing record, and the
  change is written to the log rather than applied in place
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from ..research.facts import Confidence
from .models import PhoneType, format_phone, normalize_email, normalize_phone


class MethodKind(str, Enum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ADDRESS = "ADDRESS"

    def __str__(self) -> str:
        return self.value


class MethodStatus(str, Enum):
    """What we know about whether this route works."""

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    #: Reached someone who is not the owner.
    WRONG = "WRONG"
    #: Owner asked not to be contacted. Never dial again.
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    #: Disconnected, bounced, undeliverable.
    INVALID = "INVALID"

    def __str__(self) -> str:
        return self.value

    @property
    def is_usable(self) -> bool:
        return self in (MethodStatus.UNVERIFIED, MethodStatus.VERIFIED)


#: Statuses that must never be dialled, texted or emailed again.
SUPPRESSED_STATUSES = (MethodStatus.DO_NOT_CONTACT, MethodStatus.INVALID, MethodStatus.WRONG)


@dataclass
class ContactMethod:
    """One way to reach an owner, with where it came from and how good it is."""

    method_id: Optional[int] = None
    property_id: str = ""
    contact_id: Optional[int] = None
    kind: MethodKind = MethodKind.PHONE
    #: Normalized: digits for a phone, lower-case for an email.
    value: str = ""
    phone_type: PhoneType = PhoneType.UNKNOWN
    confidence: Confidence = Confidence.UNKNOWN
    status: MethodStatus = MethodStatus.UNVERIFIED
    source: str = ""
    source_date: Optional[date] = None
    last_verified: Optional[date] = None
    is_test_data: bool = False
    attempts: int = 0
    last_outcome: Optional[str] = None
    notes: str = ""

    @classmethod
    def phone(
        cls,
        raw: Optional[str],
        source: str,
        confidence: Confidence = Confidence.UNKNOWN,
        phone_type: PhoneType = PhoneType.UNKNOWN,
        **kwargs: Any,
    ) -> Optional["ContactMethod"]:
        """Build a phone method, or ``None`` when the number is unusable.

        Returning ``None`` rather than an empty record is the point: there is
        no way to end up with a phone method that has no phone number.
        """
        digits = normalize_phone(raw)
        if not digits:
            return None
        return cls(
            kind=MethodKind.PHONE, value=digits, phone_type=phone_type,
            confidence=confidence, source=source, **kwargs
        )

    @classmethod
    def email(
        cls,
        raw: Optional[str],
        source: str,
        confidence: Confidence = Confidence.UNKNOWN,
        **kwargs: Any,
    ) -> Optional["ContactMethod"]:
        address = normalize_email(raw)
        if not address:
            return None
        return cls(
            kind=MethodKind.EMAIL, value=address, confidence=confidence,
            source=source, **kwargs
        )

    @classmethod
    def address(
        cls, raw: Optional[str], source: str, confidence: Confidence = Confidence.UNKNOWN,
        **kwargs: Any,
    ) -> Optional["ContactMethod"]:
        text = (raw or "").strip()
        if not text:
            return None
        return cls(
            kind=MethodKind.ADDRESS, value=text, confidence=confidence,
            source=source, **kwargs
        )

    # ------------------------------------------------------------------

    @property
    def is_verified(self) -> bool:
        return self.status is MethodStatus.VERIFIED

    @property
    def is_suppressed(self) -> bool:
        return self.status in SUPPRESSED_STATUSES

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable and bool(self.value)

    @property
    def dedupe_key(self) -> tuple:
        """Same property, same kind, same value = the same method."""
        return (self.property_id, self.kind.value, self.value.lower())

    def display(self) -> str:
        if self.kind is MethodKind.PHONE:
            return format_phone(self.value) or self.value
        return self.value

    def label(self) -> str:
        if self.is_test_data:
            return "TEST DATA"
        parts = [str(self.confidence)]
        if self.kind is MethodKind.PHONE and self.phone_type is not PhoneType.UNKNOWN:
            parts.insert(0, str(self.phone_type))
        if self.status is not MethodStatus.UNVERIFIED:
            parts.append(str(self.status))
        return " / ".join(parts)

    def rank(self) -> tuple:
        """Best first: usable, verified, confident, mobile, recently checked."""
        return (
            int(self.is_usable),
            int(self.is_verified),
            self.confidence.rank,
            1 if self.phone_type is PhoneType.MOBILE else 0,
            self.last_verified.toordinal() if self.last_verified else 0,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "method_id": self.method_id,
            "property_id": self.property_id,
            "contact_id": self.contact_id,
            "kind": str(self.kind),
            "value": self.display(),
            "phone_type": str(self.phone_type),
            "confidence": str(self.confidence),
            "status": str(self.status),
            "source": self.source,
            "source_date": self.source_date.isoformat() if self.source_date else None,
            "last_verified": self.last_verified.isoformat() if self.last_verified else None,
            "is_test_data": self.is_test_data,
            "attempts": self.attempts,
            "last_outcome": self.last_outcome,
            "notes": self.notes,
        }


@dataclass
class MergeOutcome:
    """What happened when an incoming method met an existing one."""

    action: str  # "added", "improved", "kept", "conflict"
    method: ContactMethod
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.action in ("added", "improved")


def merge_method(
    existing: Optional[ContactMethod], incoming: ContactMethod
) -> MergeOutcome:
    """Fold ``incoming`` into ``existing``, protecting verified information.

    * nothing on file -> **added**
    * incoming is better sourced, or the existing one is unverified ->
      **improved** (confidence, type and dates move up; never down)
    * existing is VERIFIED and incoming is weaker -> **conflict**: the
      existing record stands, and the disagreement is recorded in its notes
      rather than being applied
    * suppressed (DO_NOT_CONTACT / INVALID / WRONG) always wins, whatever the
      confidence of the incoming record
    """
    if existing is None:
        return MergeOutcome("added", incoming, f"new {incoming.kind.value.lower()}")

    if existing.is_suppressed:
        return MergeOutcome(
            "kept", existing,
            f"existing record is {existing.status} and will not be replaced",
        )

    if incoming.status in SUPPRESSED_STATUSES:
        existing.status = incoming.status
        existing.notes = f"{existing.notes}\n{incoming.notes}".strip()
        return MergeOutcome("improved", existing, f"marked {incoming.status}")

    if existing.is_verified and incoming.confidence.rank < Confidence.HIGH.rank:
        note = (
            f"{incoming.source or 'an unnamed source'} reported this "
            f"{incoming.kind.value.lower()} at {incoming.confidence} on "
            f"{(incoming.source_date or date.today()).isoformat()}; the verified "
            "record was kept."
        )
        existing.notes = f"{existing.notes}\n{note}".strip() if existing.notes else note
        return MergeOutcome("conflict", existing, note)

    improved = False
    if incoming.confidence.rank > existing.confidence.rank:
        existing.confidence = incoming.confidence
        improved = True
    if (
        existing.phone_type is PhoneType.UNKNOWN
        and incoming.phone_type is not PhoneType.UNKNOWN
    ):
        existing.phone_type = incoming.phone_type
        improved = True
    if incoming.is_verified and not existing.is_verified:
        existing.status = MethodStatus.VERIFIED
        existing.last_verified = incoming.last_verified or date.today()
        improved = True
    if incoming.source and incoming.source not in existing.source:
        existing.source = (
            f"{existing.source}, {incoming.source}" if existing.source else incoming.source
        )
        improved = True
    if incoming.source_date and (
        existing.source_date is None or incoming.source_date > existing.source_date
    ):
        existing.source_date = incoming.source_date
        improved = True
    # A real record supersedes a placeholder from the mock provider.
    if existing.is_test_data and not incoming.is_test_data:
        existing.is_test_data = False
        improved = True

    return MergeOutcome(
        "improved" if improved else "kept",
        existing,
        "provenance updated" if improved else "already on file, nothing better",
    )


def deduplicate(methods: Sequence[ContactMethod]) -> List[ContactMethod]:
    """Collapse duplicate values, keeping the best provenance for each."""
    merged: Dict[tuple, ContactMethod] = {}
    for method in methods:
        key = method.dedupe_key
        outcome = merge_method(merged.get(key), method)
        merged[key] = outcome.method
    return sorted(merged.values(), key=lambda m: m.rank(), reverse=True)


def best_method(
    methods: Sequence[ContactMethod], kind: MethodKind = MethodKind.PHONE
) -> Optional[ContactMethod]:
    """The one to try first, or ``None`` when nothing usable exists."""
    usable = [m for m in methods if m.kind is kind and m.is_usable]
    return max(usable, key=lambda m: m.rank()) if usable else None
