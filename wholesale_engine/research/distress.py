"""Normalized distress signals, each with its own source and confidence.

Wave 2 carried distress as tri-state booleans on the :class:`Lead`. That was
enough to score with, but not enough to act on: "vacant, because a lead list
said so" and "vacant, because the county posted a notice" are the same boolean
and very different facts.

A :class:`DistressProfile` keeps them apart. Every signal is a
:class:`~wholesale_engine.research.facts.Fact`, so the report can show::

    pre_foreclosure = True   source = county_records   confidence = HIGH

Nothing here manufactures a signal. A signal nobody reported stays unknown,
and unknown never scores and never rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from ..config import LEAD_SIGNALS
from .facts import Confidence, Fact, SOURCE_LEAD_LIST

#: Every signal the engine recognises. The Wave 2 ten, plus two the research
#: layer can observe directly from condition and notes.
DISTRESS_SIGNALS: tuple = tuple(LEAD_SIGNALS) + ("deferred_maintenance",)

#: Readable labels, in report order.
DISTRESS_LABELS: Dict[str, str] = {
    "absentee_owner": "absentee owner",
    "vacant": "vacant",
    "high_equity": "high equity",
    "pre_foreclosure": "pre-foreclosure",
    "foreclosure": "foreclosure",
    "tax_delinquent": "tax delinquent",
    "probate": "probate",
    "inherited": "inherited",
    "code_violation": "code violation",
    "tired_landlord": "tired landlord",
    "deferred_maintenance": "deferred maintenance",
}

#: Signals that put a clock on the seller. These are the ones worth chasing.
URGENT_SIGNALS: tuple = (
    "foreclosure",
    "pre_foreclosure",
    "tax_delinquent",
    "probate",
    "code_violation",
)

#: Phrases in a free-text note that evidence deferred maintenance. Matching one
#: records a LOW-confidence signal attributed to the note — never a fact.
_MAINTENANCE_PHRASES = (
    "deferred maintenance",
    "needs work",
    "handyman",
    "as-is",
    "as is",
    "fixer",
    "roof leak",
    "end of life",
    "gutted",
    "fire damage",
    "water damage",
)


@dataclass
class DistressProfile:
    """Every distress signal for one property, with provenance."""

    signals: Dict[str, Fact] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in DISTRESS_SIGNALS:
            self.signals.setdefault(name, Fact.unknown())

    # -- reading --------------------------------------------------------

    def get(self, name: str) -> Fact:
        return self.signals.get(name, Fact.unknown())

    def is_set(self, name: str) -> bool:
        return self.get(name).is_true

    @property
    def confirmed(self) -> List[str]:
        """Signals reported as True, in report order."""
        return [n for n in DISTRESS_SIGNALS if self.is_set(n)]

    @property
    def ruled_out(self) -> List[str]:
        return [n for n in DISTRESS_SIGNALS if self.get(n).value is False]

    @property
    def unknown(self) -> List[str]:
        return [n for n in DISTRESS_SIGNALS if not self.get(n).is_known]

    @property
    def count(self) -> int:
        return len(self.confirmed)

    @property
    def urgent_count(self) -> int:
        return sum(1 for n in self.confirmed if n in URGENT_SIGNALS)

    @property
    def best_confidence(self) -> Confidence:
        """The strongest confidence among confirmed signals."""
        confirmed = [self.get(n).confidence for n in self.confirmed]
        return max(confirmed, key=lambda c: c.rank) if confirmed else Confidence.UNKNOWN

    def labelled(self) -> List[str]:
        """Confirmed signals as ``"probate [county_records, HIGH]"`` strings."""
        out = []
        for name in self.confirmed:
            fact = self.get(name)
            out.append(f"{DISTRESS_LABELS.get(name, name)} [{fact.source}, {fact.confidence}]")
        return out

    def as_bools(self) -> Dict[str, Optional[bool]]:
        """Tri-state view, for the Wave 2 scorer and the change tracker."""
        return {name: self.get(name).value for name in DISTRESS_SIGNALS}

    def merge(self, other: "DistressProfile") -> "DistressProfile":
        """Combine two profiles, keeping the better-sourced fact per signal.

        Used when a research pass adds public-record data on top of a lead
        list: the county wins, the list fills the gaps.
        """
        from .facts import best

        merged = DistressProfile()
        for name in DISTRESS_SIGNALS:
            merged.signals[name] = best(other.get(name), self.get(name))
        return merged


def profile_from_lead(lead, source: str = SOURCE_LEAD_LIST) -> DistressProfile:
    """Build a profile from a Wave 2 :class:`Lead`.

    A lead list is somebody's claim, so everything it says is MEDIUM at best.
    Two signals are inferred rather than read, and both are marked as such:

    * ``vacant`` from an occupancy of VACANT
    * ``deferred_maintenance`` from a heavy/teardown condition, or from
      phrasing in the free-text notes
    """
    profile = DistressProfile()
    for name in LEAD_SIGNALS:
        value = getattr(lead, name, None)
        if value is not None:
            profile.signals[name] = Fact.reported(value, source, Confidence.MEDIUM)

    # Occupancy is a stronger statement of vacancy than a checkbox.
    occupancy = getattr(lead, "occupancy", None)
    if occupancy is not None and getattr(occupancy, "value", "") == "vacant":
        profile.signals["vacant"] = Fact.reported(
            True, source, Confidence.MEDIUM, "occupancy reported as VACANT"
        )

    condition = getattr(lead, "condition", None)
    condition_value = getattr(condition, "value", "")
    if condition_value in ("heavy", "teardown"):
        profile.signals["deferred_maintenance"] = Fact.reported(
            True, source, Confidence.MEDIUM, f"condition reported as {condition_value.upper()}"
        )
    elif condition_value in ("turnkey", "cosmetic"):
        profile.signals["deferred_maintenance"] = Fact.reported(
            False, source, Confidence.LOW, f"condition reported as {condition_value.upper()}"
        )

    if not profile.get("deferred_maintenance").is_known:
        notes = (getattr(lead, "notes", "") or "").lower()
        hit = next((p for p in _MAINTENANCE_PHRASES if p in notes), None)
        if hit:
            profile.signals["deferred_maintenance"] = Fact.reported(
                True, "listing_notes", Confidence.LOW, f'notes mention "{hit}"'
            )
    return profile


def profile_from_public_records(
    data: Dict[str, object], source: str = "county_records"
) -> DistressProfile:
    """Build a profile from a public-record response.

    Only genuine booleans are taken. A missing key, a null, or anything
    non-boolean leaves the signal unknown — a record that does not mention
    foreclosure is not a record saying there is none.
    """
    profile = DistressProfile()
    for name in DISTRESS_SIGNALS:
        value = data.get(name)
        if isinstance(value, bool):
            profile.signals[name] = Fact.reported(value, source, Confidence.HIGH)
    return profile
