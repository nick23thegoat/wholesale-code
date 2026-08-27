"""CSV-backed provider — the mode that works with no credentials at all.

Wraps the Wave 2 :class:`CsvLeadSource` in the Wave 4 provider interface so
the funnel, the database and the change detector can all be exercised end to
end against files you control, before a dollar is spent on an API.

It honestly reports what a file can and cannot do: a lead list supports search
and (when a comps file is supplied) comps. It has no ownership records and no
public-record distress data, and says so rather than inventing either.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..lead_hunter.models import Lead
from ..lead_hunter.sources.csv_source import CsvLeadSource, attach_comps
from ..models.property import Comp
from .base import Capability, PropertyDataProvider, ProviderResponse
from .criteria import HuntCriteria
from .metrics import ProviderMetrics


class CsvProvider(PropertyDataProvider):
    """Reads leads (and optionally comps) from local CSV files."""

    name = "csv"
    description = "Local CSV lead list. No credentials, no network, no cost."
    is_local = True
    requires_credentials = False
    documentation_note = "Local files only — no vendor API involved."

    def __init__(
        self,
        path: Path,
        comps_path: Optional[Path] = None,
        metrics: Optional[ProviderMetrics] = None,
    ) -> None:
        super().__init__(metrics)
        self.path = Path(path)
        self.comps_path = Path(comps_path) if comps_path else None
        # Add COMPS to whatever this class declares rather than replacing the
        # tuple: a subclass that also supports OWNER or DISTRESS must keep it.
        declared = tuple(type(self).capabilities)
        if self.comps_path and Capability.COMPS not in declared:
            declared += (Capability.COMPS,)
        elif not self.comps_path:
            declared = tuple(c for c in declared if c is not Capability.COMPS)
        self.capabilities = declared
        self._source = CsvLeadSource(self.path)
        self._comps_attached = False
        self.warnings: List[str] = []

    # ------------------------------------------------------------------

    def search_properties(self, criteria: HuntCriteria) -> ProviderResponse[List[Lead]]:
        """Read the file and narrow it by geography, price and type.

        Filtering here mirrors what a real API would do server-side, so the
        funnel behaves identically whichever provider is in front of it.
        """
        self.metrics.search_calls += 1
        try:
            leads = self._source.search_leads(criteria.to_legacy())
        except (OSError, ValueError) as exc:
            self.metrics.record_error(f"{self.path.name}: {exc}")
            return ProviderResponse(data=[], supported=True, reason=str(exc), source=self.name)

        self.warnings.extend(getattr(self._source, "warnings", []))
        self.metrics.properties_searched += len(leads)

        if self.comps_path and not self._comps_attached:
            matched = attach_comps(leads, self.comps_path)
            self._comps_attached = True
            self.metrics.comp_calls += 1  # one bulk read, not one per lead
            if matched == 0:
                self.warnings.append(
                    f"{self.comps_path.name}: no comps matched any lead — check that "
                    "lead_id / property_id / address line up between the two files."
                )

        kept = [lead for lead in leads if self._matches(lead, criteria)]
        if criteria.limit is not None:
            kept = kept[: criteria.limit]
        self.metrics.properties_returned += len(kept)
        self.metrics.properties_filtered += len(leads) - len(kept)
        return ProviderResponse(data=kept, source=self.name, calls=1)

    def _matches(self, lead: Lead, criteria: HuntCriteria) -> bool:
        """Source-side narrowing. Unknown values never reject a lead."""
        if not criteria.matches_geography(
            lead.state, lead.county, lead.city, lead.zip_code
        ):
            return False
        if not criteria.matches_price(lead.asking_price):
            return False
        if not criteria.matches_property_type(str(lead.property_type.value)):
            return False
        return True

    # ------------------------------------------------------------------

    def get_comps(
        self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6
    ) -> ProviderResponse[List[Comp]]:
        """Comps already joined onto the lead by the bulk read above.

        No per-lead call is made or counted: the file was read once.
        """
        if not self.comps_path:
            return self._unsupported(Capability.COMPS)
        if not lead.comps:
            return ProviderResponse.empty(
                self.name, f"no comps in {self.comps_path.name} for this address"
            )
        return ProviderResponse(data=list(lead.comps), source=self.name)
