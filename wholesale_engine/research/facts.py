"""A value plus where it came from and how much to trust it.

The engine's central rule is that an unknown must stay visibly unknown. A bare
``None`` cannot say whether nobody looked, somebody looked and found nothing,
or a source reported it and the source is unreliable. :class:`Fact` says which.

    >>> Fact.reported(True, "county_records", Confidence.HIGH)
    >>> Fact.unknown("no public-record source configured")

Every research field carries one, so a report can always answer "how do you
know that?" — and so nothing in this package can quietly manufacture a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Iterable, List, Optional, TypeVar

T = TypeVar("T")


class Confidence(Enum):
    """How much weight a fact can carry.

    ``HIGH`` is reserved for a primary source — a county record, a recorded
    deed, a closed sale. A lead-list CSV is a claim by whoever built the list,
    which is ``MEDIUM`` at best and often ``LOW``.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value

    @property
    def rank(self) -> int:
        return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}[self.value]

    @property
    def weight(self) -> float:
        """0.0-1.0, for scoring."""
        return {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.35, "UNKNOWN": 0.0}[self.value]

    @classmethod
    def parse(cls, raw: Any) -> "Confidence":
        if isinstance(raw, cls):
            return raw
        text = str(raw or "").strip().upper()
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


#: Source labels used by the bundled providers. A real vendor adds its own.
SOURCE_UNKNOWN = "unknown"
SOURCE_LEAD_LIST = "lead_list"
SOURCE_USER = "user_supplied"
SOURCE_DERIVED = "derived"
SOURCE_COUNTY = "county_records"
SOURCE_PROVIDER = "property_data_provider"


@dataclass(frozen=True)
class Fact(Generic[T]):
    """One researched value with its provenance.

    ``value is None`` always means unknown. There is no other reading, and no
    code path sets a non-None value without also naming a source.
    """

    value: Optional[T] = None
    source: str = SOURCE_UNKNOWN
    confidence: Confidence = Confidence.UNKNOWN
    note: str = ""

    # -- constructors ---------------------------------------------------

    @classmethod
    def unknown(cls, note: str = "") -> "Fact[T]":
        """Nothing is known. The only way to build a valueless fact."""
        return cls(value=None, source=SOURCE_UNKNOWN, confidence=Confidence.UNKNOWN, note=note)

    @classmethod
    def reported(
        cls,
        value: Optional[T],
        source: str,
        confidence: Confidence = Confidence.MEDIUM,
        note: str = "",
    ) -> "Fact[T]":
        """A value from a named source. ``None`` collapses back to unknown."""
        if value is None:
            return cls.unknown(note or f"{source} did not report this")
        return cls(value=value, source=source, confidence=confidence, note=note)

    @classmethod
    def derived(cls, value: Optional[T], note: str, confidence: Confidence = Confidence.LOW) -> "Fact[T]":
        """A value the engine computed rather than received.

        Derived facts are capped at the confidence of their weakest input by
        the caller, and are never presented as verified.
        """
        if value is None:
            return cls.unknown(note)
        return cls(value=value, source=SOURCE_DERIVED, confidence=confidence, note=note)

    # -- reading --------------------------------------------------------

    @property
    def is_known(self) -> bool:
        return self.value is not None

    @property
    def is_true(self) -> bool:
        """Only for boolean facts: reported and affirmative."""
        return self.value is True

    def or_else(self, default: T) -> T:
        return default if self.value is None else self.value

    def describe(self) -> str:
        if not self.is_known:
            return f"unknown{f' ({self.note})' if self.note else ''}"
        return f"{self.value} [{self.source}, {self.confidence}]"

    def __str__(self) -> str:
        return self.describe()


def best(*facts: "Fact[T]") -> "Fact[T]":
    """The most trustworthy known fact, or unknown when none is known.

    Ties keep the first argument, so callers order by preference: a county
    record before a lead list, a lead list before anything derived.
    """
    known = [f for f in facts if f.is_known]
    if not known:
        return next((f for f in facts if f.note), Fact.unknown())
    return max(known, key=lambda f: f.confidence.rank)


def lowest_confidence(*facts: "Fact") -> Confidence:
    """The weakest confidence among known facts — a derived value's ceiling."""
    known = [f.confidence for f in facts if f.is_known]
    if not known:
        return Confidence.UNKNOWN
    return min(known, key=lambda c: c.rank)


def known_count(facts: Iterable["Fact"]) -> int:
    return sum(1 for f in facts if f.is_known)


def missing_names(mapping: dict) -> List[str]:
    """Field names whose fact is unknown, in declaration order."""
    return [name for name, fact in mapping.items() if not fact.is_known]
