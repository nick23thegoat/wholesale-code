"""The lead source interface.

Every way of getting leads — a CSV export, a property-data API, a county
record feed, a CRM — implements :class:`BaseLeadSource`. The pipeline only
ever sees this interface, so adding a source in Wave 3 requires no change to
normalization, scoring, filtering, or the Wave 1 analyzer.

The four capability methods mirror what a real property-data vendor exposes:

===================  ====================================================
``search_leads()``   find candidate properties matching criteria
``get_property()``   fetch full detail for one property
``get_owner()``      ownership record (NOT contact info — see skip_trace)
``get_comps()``      comparable sales for a subject property
===================  ====================================================

Only ``search_leads()`` is required. The others raise
:class:`~wholesale_engine.data.sources.NotConfiguredError` by default, so a
CSV source is not forced to pretend it can do things it cannot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ...data.sources import NotConfiguredError, SearchCriteria
from ...models.property import Comp
from ..models import Lead


class BaseLeadSource(ABC):
    """Abstract source of leads."""

    #: Short identifier recorded on every lead this source produces.
    name: str = "unnamed-source"

    #: Set False on sources that reach the network, so callers can tell
    #: local files apart from remote calls when rate limiting matters.
    is_local: bool = True

    @abstractmethod
    def search_leads(self, criteria: Optional[SearchCriteria] = None) -> List[Lead]:
        """Return candidate leads. Unknown fields must be left blank."""

    # -- optional capabilities ------------------------------------------

    def get_property(self, property_id: str) -> Lead:
        """Full detail for one property."""
        raise NotConfiguredError(
            f"{self.name} cannot look up individual properties. Wave 2 works from "
            "the data in your CSV."
        )

    def get_owner(self, property_id: str) -> dict:
        """Ownership record from a legitimate public-record source.

        This is ownership only — never phone numbers or emails. Contact data
        belongs behind :mod:`wholesale_engine.lead_hunter.skip_trace`, which
        is a seam, not an implementation.
        """
        raise NotConfiguredError(
            f"{self.name} has no ownership data. This engine does not have access "
            "to county records and will not invent an owner."
        )

    def get_comps(self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6) -> List[Comp]:
        """Comparable sales for a subject property.

        Implementations return raw :class:`Comp` objects only. Grading and ARV
        derivation stay in :mod:`wholesale_engine.analysis.comps` so vendor
        data is held to exactly the same reliability bar as hand-entered comps.
        """
        raise NotConfiguredError(
            f"{self.name} has no comp feed. Supply comps by CSV, or the ARV stays "
            "labelled NEEDS ARV VERIFICATION."
        )

    def describe(self) -> str:
        return f"{self.name} ({'local' if self.is_local else 'remote'})"
