"""Integration seams for V2 and beyond. **Nothing here is implemented.**

Every future data source plugs in as one of the protocols below. They exist
now so the analysis layer never has to change when the sources arrive: the
analyzer only ever sees :class:`PropertyLead` and :class:`Comp` objects, and
does not know or care whether a human typed them or an API returned them.

The stubs deliberately raise :class:`NotImplementedError`. This engine does
not have access to Zillow, the MLS, county records, or any skip-tracing
database, and it must never behave as if it does.

Planned wiring::

    LeadSource ──┐
                 ├─► List[PropertyLead] ──► PropertyEnricher ──► CompProvider
    CSV loader ──┘                                                    │
                                                                      ▼
                                                          analysis.analyze_property
                                                                      │
                                        SkipTraceProvider ◄───────────┤ (contact only,
                                                                      │  after a GO)
                                                                      ▼
                                                        reports + ResultSink (CRM/Sheets)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional, Protocol, runtime_checkable

from ..models.property import Comp, PropertyLead
from ..models.results import AnalysisResult


@dataclass(frozen=True)
class SearchCriteria:
    """Filter set for an automated lead search (V2: daily lead hunting)."""

    city: Optional[str] = None
    state: Optional[str] = None
    states: tuple = ()  # multi-state searches (Wave 2 target markets)
    county: Optional[str] = None
    zip_codes: tuple = ()
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    min_beds: Optional[float] = None
    property_types: tuple = ()
    max_days_on_market: Optional[int] = None
    keywords: tuple = ()  # e.g. "as-is", "cash only", "handyman special"


@runtime_checkable
class LeadSource(Protocol):
    """Anything that can produce leads: a CSV, an API, a scraper, a CRM export.

    The CSV loader in :mod:`wholesale_engine.data.csv_loader` already satisfies
    this shape, which is the point — V2 sources are drop-in siblings of it.
    """

    name: str

    def fetch(self, criteria: SearchCriteria) -> List[PropertyLead]:
        """Return leads matching ``criteria``."""


@runtime_checkable
class PropertyEnricher(Protocol):
    """Fills in property attributes the user did not supply.

    Future implementations: a property-data API for beds/baths/sqft/year built,
    and a county/public-record connector for assessed value, tax status and
    ownership. Until one exists, missing fields stay missing and show up under
    MISSING DATA in the report.
    """

    name: str

    def enrich(self, lead: PropertyLead) -> PropertyLead:
        """Return the lead with additional verified fields populated."""


@runtime_checkable
class CompProvider(Protocol):
    """Supplies comparable sales for a subject property.

    An implementation returns raw :class:`Comp` objects only. All grading and
    ARV derivation stays in :mod:`wholesale_engine.analysis.comps`, so the
    underwriting rules do not fork per data vendor.
    """

    name: str

    def find_comps(
        self,
        lead: PropertyLead,
        radius_miles: float = 1.0,
        months_back: int = 6,
        as_of: Optional[date] = None,
    ) -> List[Comp]:
        """Return candidate comps. Grading happens downstream, not here."""


@runtime_checkable
class SkipTraceProvider(Protocol):
    """Owner-contact lookup. Intentionally not implemented in V1.

    Skip tracing touches personal data and is regulated (TCPA, DNC lists, state
    law). When this is wired up it must run **after** a lead clears the deal
    filter, must record consent and suppression lists, and must never be used
    to fabricate a contact record. The engine will not guess a phone number,
    an owner name, or a mailing address.
    """

    name: str

    def trace(self, lead: PropertyLead) -> dict:
        """Return verified contact data from a licensed provider, or raise."""


@runtime_checkable
class ResultSink(Protocol):
    """Where finished analyses go: Google Sheets, a CRM, a database, email."""

    name: str

    def publish(self, results: Iterable[AnalysisResult]) -> None:
        """Push results to the destination system."""


# ---------------------------------------------------------------------------
# V1 placeholders — present so imports and wiring can be written today.
# ---------------------------------------------------------------------------


class NotConfiguredError(RuntimeError):
    """Raised when a V2 integration is called before it exists."""


class _Unimplemented:
    """Base for the placeholder integrations."""

    name = "unconfigured"
    reason = "not implemented in V1"

    def _fail(self, action: str):
        raise NotConfiguredError(
            f"{self.name}: {action} is {self.reason}. V1 works only from data you supply "
            f"manually or import from CSV."
        )


class UnconfiguredCompProvider(_Unimplemented):
    name = "comp-provider"

    def find_comps(self, lead: PropertyLead, *args, **kwargs) -> List[Comp]:
        self._fail("automated comp retrieval")
        return []


class UnconfiguredSkipTrace(_Unimplemented):
    name = "skip-trace"

    def trace(self, lead: PropertyLead) -> dict:
        self._fail("owner contact lookup")
        return {}


class UnconfiguredLeadSource(_Unimplemented):
    name = "lead-search"

    def fetch(self, criteria: SearchCriteria) -> List[PropertyLead]:
        self._fail("automated lead search")
        return []
