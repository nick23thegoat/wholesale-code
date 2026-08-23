"""The property-data provider interface.

A provider is anything that can answer questions about real property: a paid
data API, a county record feed, or a CSV file exported from either. The funnel
in :mod:`wholesale_engine.hunt` only ever sees this interface.

Five capabilities, all optional except the first:

======================  =============================================
``search_properties``   find candidates matching :class:`HuntCriteria`
``get_property``        full detail for one property
``get_owner``           ownership of record (NOT contact information)
``get_distress_data``   liens, tax status, foreclosure filings
``get_comps``           comparable sales for a subject property
======================  =============================================

**Unsupported is a clear answer, not a failure.** A provider declares what it
supports via :attr:`capabilities`; asking for anything else returns a
:class:`ProviderResponse` with ``supported=False`` and a reason. Nothing in
this package ever fabricates a result to fill a gap — a missing owner stays
missing, and the lead carries it as a gap to go and fill.

Every result is converted to the existing :class:`~wholesale_engine.lead_hunter.models.Lead`
and analyzed by the existing Wave 1 analyzer. There is no second deal-analysis
system and there must never be one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from ..lead_hunter.models import Lead
from ..models.property import Comp
from .criteria import HuntCriteria
from .metrics import ProviderMetrics

T = TypeVar("T")


class Capability(str, Enum):
    """What a provider can be asked for."""

    SEARCH = "search_properties"
    PROPERTY = "get_property"
    OWNER = "get_owner"
    DISTRESS = "get_distress_data"
    COMPS = "get_comps"

    def __str__(self) -> str:
        return self.value


class ProviderError(RuntimeError):
    """A provider call failed for a reason the caller may want to retry."""


class ProviderNotConfigured(ProviderError):
    """Credentials or an endpoint are missing.

    Raised at construction, never at call time, so an unconfigured provider can
    never be mistaken for one that returned no results.
    """


@dataclass
class ProviderResponse(Generic[T]):
    """One provider answer, with the reason attached when there isn't one.

    Three distinct states, deliberately not collapsed into ``None``:

    * ``supported=False`` — this provider cannot answer this kind of question
    * ``supported=True, data=None`` — it can, and the answer is "nothing found"
    * ``supported=True, data=<value>`` — a real answer
    """

    data: Optional[T] = None
    supported: bool = True
    reason: str = ""
    source: str = ""
    calls: int = 0

    @property
    def ok(self) -> bool:
        return self.supported and self.data is not None

    @classmethod
    def unsupported(cls, provider: str, capability: Capability) -> "ProviderResponse[T]":
        return cls(
            data=None,
            supported=False,
            reason=(
                f"{provider} does not support {capability}. This is not an error and "
                "nothing has been invented to fill the gap — the field stays blank "
                "and is reported as missing data."
            ),
            source=provider,
        )

    @classmethod
    def empty(cls, provider: str, reason: str = "no results") -> "ProviderResponse[T]":
        return cls(data=None, supported=True, reason=reason, source=provider)


@dataclass
class ProviderInfo:
    """Everything a user needs to know before choosing a provider."""

    name: str
    description: str
    is_local: bool = True
    requires_credentials: bool = False
    capabilities: tuple = ()
    documentation_note: str = ""
    configured: bool = True
    missing_settings: List[str] = field(default_factory=list)


class PropertyDataProvider(ABC):
    """Base class for every source of property data."""

    #: Short name used by ``--source``.
    name: str = "unnamed"
    #: One-line description shown by ``--list-sources``.
    description: str = ""
    #: False for anything that reaches the network.
    is_local: bool = True
    #: Set on providers that cannot be constructed without credentials.
    requires_credentials: bool = False
    #: What this provider can actually answer.
    capabilities: tuple = (Capability.SEARCH,)
    #: Where the endpoint and auth contract came from. Left blank until a real
    #: vendor's published API documentation has been read.
    documentation_note: str = ""

    def __init__(self, metrics: Optional[ProviderMetrics] = None) -> None:
        self.metrics = metrics or ProviderMetrics(provider_name=self.name)
        if not self.metrics.provider_name:
            self.metrics.provider_name = self.name

    # ------------------------------------------------------------------
    # Capability declaration
    # ------------------------------------------------------------------

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            description=self.description,
            is_local=self.is_local,
            requires_credentials=self.requires_credentials,
            capabilities=tuple(str(c) for c in self.capabilities),
            documentation_note=self.documentation_note,
        )

    def _unsupported(self, capability: Capability) -> ProviderResponse:
        self.metrics.record_unsupported(str(capability))
        return ProviderResponse.unsupported(self.name, capability)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @abstractmethod
    def search_properties(self, criteria: HuntCriteria) -> ProviderResponse[List[Lead]]:
        """Find candidate properties. The only required capability.

        Implementations must return normalized :class:`Lead` objects with every
        unknown field left blank. Narrow server-side wherever the vendor's API
        allows it — that is the cheapest filtering there is.
        """

    def get_property(self, lead: Lead) -> ProviderResponse[Lead]:
        """Full detail for one property. Billable: called only after filtering."""
        return self._unsupported(Capability.PROPERTY)

    def get_owner(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """Ownership of record.

        Ownership only — never a phone number or an email address. Contact
        data is skip tracing, which lives behind its own interface and is not
        connected to anything.
        """
        return self._unsupported(Capability.OWNER)

    def get_distress_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """Liens, tax delinquency, foreclosure filings, code violations.

        Public-record facts only, from a source that publishes them. An
        unanswered question stays unanswered.
        """
        return self._unsupported(Capability.DISTRESS)

    def get_comps(
        self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6
    ) -> ProviderResponse[List[Comp]]:
        """Comparable sales.

        The most expensive call in the funnel, so it runs last and only on
        candidates that survived everything else. Returned comps are graded by
        the existing :mod:`wholesale_engine.analysis.comps` — vendor data is
        held to exactly the same reliability bar as hand-entered comps.
        """
        return self._unsupported(Capability.COMPS)

    # ------------------------------------------------------------------

    def describe(self) -> str:
        where = "local" if self.is_local else "remote"
        caps = ", ".join(str(c) for c in self.capabilities)
        return f"{self.name} ({where}) — {caps}"
