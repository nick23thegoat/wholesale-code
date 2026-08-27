"""TEMPLATE for a future property-data API source. **Not connected to anything.**

This file deliberately contains no network code, no vendor names, no API keys
and no endpoints. It exists so that Wave 3 can add a real provider by filling
in four methods, without touching normalization, scoring, filtering, or the
Wave 1 analyzer.

To implement one later:

1. Copy this file to ``sources/<provider>_source.py``.
2. Implement ``search_leads``; add ``get_property`` / ``get_owner`` /
   ``get_comps`` if the provider supports them.
3. Map the provider's payload into :class:`~wholesale_engine.lead_hunter.models.Lead`
   (and :class:`~wholesale_engine.models.property.Comp` for comps). **Map only
   fields the provider actually returns** — leave everything else blank.
4. Register it in ``sources/__init__.py``.

Rules that apply to any implementation of this interface:

* Use the provider's documented, licensed API. No scraping around a paywall,
  a login, a CAPTCHA, an anti-bot system or a robots.txt exclusion.
* Honour the published rate limits, and back off on 429/5xx.
* Never fill a gap with a guess. An absent field stays absent — that is what
  drives the MISSING DATA and NEEDS VERIFICATION output.
* A vendor's ARV is a claim, not a fact. It enters the system as
  ``estimated_value`` and only becomes VERIFIED/SUPPORTED if comps support it.
* Contact data (phones, emails) does not belong here. See
  :mod:`wholesale_engine.lead_hunter.skip_trace`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...data.sources import NotConfiguredError, SearchCriteria
from ...models.property import Comp
from ..models import Lead
from .base import BaseLeadSource


class ApiLeadSourceTemplate(BaseLeadSource):
    """Skeleton for a licensed property-data API. Every method raises.

    Args:
        api_key: credential for the future provider. Never hard-code one; read
            it from the environment or a secrets manager at call time.
        rate_limit_per_minute: the provider's documented limit, to be honoured
            by the implementation rather than discovered by being blocked.
    """

    name = "api-source-template"
    is_local = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        rate_limit_per_minute: int = 60,
        base_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.rate_limit_per_minute = rate_limit_per_minute
        self.base_url = base_url

    # -- required --------------------------------------------------------

    def search_leads(self, criteria: Optional[SearchCriteria] = None) -> List[Lead]:
        """Would call the provider's property-search endpoint.

        Implementation sketch::

            payload = self._get("/search", params=self._to_params(criteria))
            return [self._to_lead(item) for item in payload["results"]]
        """
        raise NotConfiguredError(
            "No property-data API is connected. Wave 2 runs on CSV data you supply; "
            "API integration is Wave 3 work."
        )

    # -- optional --------------------------------------------------------

    def get_property(self, property_id: str) -> Lead:
        raise NotConfiguredError("No property-data API is connected.")

    def get_owner(self, property_id: str) -> dict:
        raise NotConfiguredError(
            "No public-record API is connected. This engine will not invent ownership, "
            "liens, mortgages or foreclosure status."
        )

    def get_comps(self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6) -> List[Comp]:
        raise NotConfiguredError("No comp API is connected.")

    # -- mapping helpers the implementation will need ---------------------

    def _to_lead(self, payload: Dict[str, Any]) -> Lead:
        """Map a provider record onto :class:`Lead`.

        Keep this the *only* place that knows the vendor's field names, and
        map exclusively fields the vendor actually returned.
        """
        raise NotConfiguredError("Mapping is defined when a provider is chosen.")

    def _to_params(self, criteria: Optional[SearchCriteria]) -> Dict[str, Any]:
        """Translate :class:`SearchCriteria` into the provider's query params."""
        raise NotConfiguredError("Mapping is defined when a provider is chosen.")
