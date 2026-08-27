"""Provider-independent research layer.

    LEAD -> PROPERTY RESEARCH -> OWNER -> DISTRESS -> EQUITY -> (comps, ARV,
    repairs, MAO, offer — all Wave 1, unchanged)

Everything here answers "what do we actually know, and how do we know it?"
Nothing here underwrites: the deal math stays in :mod:`wholesale_engine.analysis`.
"""

from __future__ import annotations

from .distress import (
    DISTRESS_LABELS,
    DISTRESS_SIGNALS,
    URGENT_SIGNALS,
    DistressProfile,
    profile_from_lead,
    profile_from_public_records,
)
from .equity import EquityAssessment, EquityStatus, assess_equity
from .facts import Confidence, Fact, best, lowest_confidence
from .models import PropertyResearch
from .owner_research import OwnerRecord, OwnerResearchService, looks_like_entity
from .property_research import PropertyResearchService

__all__ = [
    "Confidence",
    "DISTRESS_LABELS",
    "DISTRESS_SIGNALS",
    "DistressProfile",
    "EquityAssessment",
    "EquityStatus",
    "Fact",
    "OwnerRecord",
    "OwnerResearchService",
    "PropertyResearch",
    "PropertyResearchService",
    "URGENT_SIGNALS",
    "assess_equity",
    "best",
    "looks_like_entity",
    "lowest_confidence",
    "profile_from_lead",
    "profile_from_public_records",
]
