"""The research service: a lead in, a normalized :class:`PropertyResearch` out.

    LEAD -> PROPERTY RESEARCH -> OWNER RESEARCH -> DISTRESS -> EQUITY

Provider-independent by construction. It reads whatever the lead already
carries, asks the provider for anything it supports, and leaves the rest
unknown. Swapping a CSV provider for a paid API changes how much comes back —
it changes nothing about the shape of the result or the code that consumes it.

Cost discipline: this service issues at most one ``get_property`` and one
``get_distress_data`` call per lead, and only when the provider declares the
capability. It never calls for comps — comps are the expensive stage and are
sequenced separately by :mod:`wholesale_engine.hunt`.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from ..lead_hunter.models import Lead
from ..models.enums import Condition, Occupancy, PropertyType
from ..providers.base import Capability, PropertyDataProvider
from ..providers.metrics import ProviderMetrics
from .distress import DistressProfile, profile_from_lead, profile_from_public_records
from .equity import assess_equity
from .facts import (
    SOURCE_LEAD_LIST,
    SOURCE_PROVIDER,
    Confidence,
    Fact,
    best,
)
from .models import (
    FORECLOSURE_ACTIVE,
    FORECLOSURE_NONE,
    FORECLOSURE_PRE,
    TAX_STATUS_CURRENT,
    TAX_STATUS_DELINQUENT,
    PropertyResearch,
)
from .owner_research import OwnerResearchService


class PropertyResearchService:
    """Assembles everything knowable about a property from every source.

    :param provider: optional data provider. Without one the service still
        works — it just returns more unknowns, which is the correct answer.
    :param metrics: shared call counter, so research shows up in the run's
        cost report alongside search and comps.
    """

    def __init__(
        self,
        provider: Optional[PropertyDataProvider] = None,
        metrics: Optional[ProviderMetrics] = None,
    ) -> None:
        self.provider = provider
        self.metrics = metrics or (provider.metrics if provider else ProviderMetrics())
        self.owner_service = OwnerResearchService(provider)

    # ------------------------------------------------------------------

    def research(self, lead: Lead, as_of: Optional[date] = None) -> PropertyResearch:
        """Full research pass over one lead."""
        result = self._from_lead(lead)
        result.researched_on = as_of or date.today()

        detail = self._fetch_detail(lead)
        if detail is not None:
            self._apply_detail(result, detail)

        public = self._fetch_distress(lead)
        if public is not None:
            result.distress = result.distress.merge(
                profile_from_public_records(public)
            )
            self._apply_public_money(result, public)
            result.sources_used.append("county_records")

        result.owner = self.owner_service.research(lead)
        self._reconcile_absentee(result)
        self._derive_tax_status(result)
        self._derive_foreclosure_status(result)
        self._derive_equity(result)
        self._derive_high_equity_signal(result)
        result.source_confidence = self._overall_confidence(result)
        return result

    def research_all(
        self, leads: List[Lead], as_of: Optional[date] = None
    ) -> Dict[str, PropertyResearch]:
        """Research a batch, keyed by lead id. Order is preserved."""
        return {
            (lead.lead_id or lead.display_id()): self.research(lead, as_of)
            for lead in leads
        }

    # ------------------------------------------------------------------
    # Stage 1: what the lead already tells us
    # ------------------------------------------------------------------

    def _from_lead(self, lead: Lead) -> PropertyResearch:
        source = lead.source or SOURCE_LEAD_LIST
        confidence = Confidence.MEDIUM

        def fact(value: Any, note: str = "") -> Fact:
            return Fact.reported(value, source, confidence, note)

        result = PropertyResearch(
            property_id=lead.property_id or lead.lead_id,
            lead_id=lead.lead_id,
            address=lead.address,
            city=lead.city,
            state=lead.state,
            county=lead.county,
            zip_code=lead.zip_code,
            property_type=lead.property_type,
            occupancy=lead.occupancy,
            condition=lead.condition,
            beds=fact(lead.beds),
            baths=fact(lead.baths),
            sqft=fact(lead.sqft),
            year_built=fact(lead.year_built),
            estimated_value=fact(lead.estimated_value),
            current_price=fact(lead.asking_price),
            estimated_repairs=fact(lead.estimated_repairs),
            days_on_market=fact(lead.days_on_market),
            source=source,
            sources_used=[source],
        )
        result.distress = profile_from_lead(lead, source)
        return result

    # ------------------------------------------------------------------
    # Stage 2: the provider, when it supports the call
    # ------------------------------------------------------------------

    def _fetch_detail(self, lead: Lead) -> Optional[Lead]:
        if self.provider is None or not self.provider.supports(Capability.PROPERTY):
            return None
        response = self.provider.get_property(lead)
        self.metrics.detail_calls += 1
        if not response.supported or not response.ok:
            if response.supported and response.reason:
                lead.needs_verification.append(response.reason)
            return None
        return response.data

    def _fetch_distress(self, lead: Lead) -> Optional[Dict[str, Any]]:
        if self.provider is None or not self.provider.supports(Capability.DISTRESS):
            return None
        response = self.provider.get_distress_data(lead)
        self.metrics.distress_calls += 1
        if response.ok and isinstance(response.data, dict):
            return response.data
        return None

    def _apply_detail(self, result: PropertyResearch, detail: Lead) -> None:
        """Merge a provider detail record. Better-sourced facts win."""
        source = getattr(detail, "source", None) or SOURCE_PROVIDER

        def incoming(value: Any) -> Fact:
            return Fact.reported(value, source, Confidence.HIGH)

        for attr, value in (
            ("beds", detail.beds),
            ("baths", detail.baths),
            ("sqft", detail.sqft),
            ("year_built", detail.year_built),
            ("estimated_value", detail.estimated_value),
            ("estimated_repairs", detail.estimated_repairs),
            ("days_on_market", detail.days_on_market),
        ):
            setattr(result, attr, best(incoming(value), getattr(result, attr)))

        for attr, unknown_member in (
            ("property_type", PropertyType.UNKNOWN),
            ("occupancy", Occupancy.UNKNOWN),
            ("condition", Condition.UNKNOWN),
        ):
            current = getattr(result, attr)
            found = getattr(detail, attr, unknown_member)
            if current is unknown_member and found is not unknown_member:
                setattr(result, attr, found)

        result.distress = result.distress.merge(profile_from_lead(detail, source))
        if source not in result.sources_used:
            result.sources_used.append(source)

    def _apply_public_money(self, result: PropertyResearch, data: Dict[str, Any]) -> None:
        """Take the money facts a public-record response carries.

        Only real numbers are taken. A missing mortgage balance stays missing —
        it never becomes zero, because "no mortgage recorded here" and "no
        mortgage" are different claims and this engine cannot tell them apart.
        """
        for key, attr in (
            ("mortgage_balance", "mortgage_balance"),
            ("liens", "liens"),
            ("tax_amount", "tax_amount"),
            ("last_sale_price", "last_sale_price"),
            ("assessed_value", None),
        ):
            if attr is None:
                continue
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(
                    result, attr, Fact.reported(float(value), "county_records", Confidence.HIGH)
                )
        sale_date = data.get("last_sale_date")
        if isinstance(sale_date, date):
            result.last_sale_date = Fact.reported(
                sale_date, "county_records", Confidence.HIGH
            )
        mailing = data.get("owner_mailing_address")
        if isinstance(mailing, str) and mailing.strip():
            result.notes.append("Owner mailing address available from public records.")

    # ------------------------------------------------------------------
    # Stage 3: derivations
    # ------------------------------------------------------------------

    def _reconcile_absentee(self, result: PropertyResearch) -> None:
        """Keep the absentee signal and the owner record in agreement."""
        from_owner = result.owner.absentee_owner
        from_distress = result.distress.get("absentee_owner")
        resolved = best(from_owner, from_distress)
        if resolved.is_known:
            result.owner.absentee_owner = resolved
            result.distress.signals["absentee_owner"] = resolved

    def _derive_tax_status(self, result: PropertyResearch) -> None:
        """CURRENT / DELINQUENT only when a source actually said so."""
        if result.tax_status.is_known:
            return
        delinquent = result.distress.get("tax_delinquent")
        if not delinquent.is_known:
            result.tax_status = Fact.unknown(
                "no tax record checked — this engine has no assessor access"
            )
            return
        result.tax_status = Fact(
            value=TAX_STATUS_DELINQUENT if delinquent.value else TAX_STATUS_CURRENT,
            source=delinquent.source,
            confidence=delinquent.confidence,
            note="from the reported tax-delinquency signal",
        )

    def _derive_foreclosure_status(self, result: PropertyResearch) -> None:
        """Roll the two foreclosure signals into one status.

        Active foreclosure outranks pre-foreclosure. Both unknown means
        unknown — never NONE REPORTED, which would assert something nobody
        checked.
        """
        active = result.distress.get("foreclosure")
        pre = result.distress.get("pre_foreclosure")
        if active.is_true:
            result.foreclosure_status = Fact(
                FORECLOSURE_ACTIVE, active.source, active.confidence
            )
        elif pre.is_true:
            result.foreclosure_status = Fact(FORECLOSURE_PRE, pre.source, pre.confidence)
        elif active.value is False and pre.value is False:
            result.foreclosure_status = Fact(
                FORECLOSURE_NONE,
                active.source,
                min(active.confidence, pre.confidence, key=lambda c: c.rank),
                "both foreclosure signals reported as not applicable",
            )
        else:
            result.foreclosure_status = Fact.unknown(
                "no foreclosure filing checked — this engine has no court-record access"
            )

    def _derive_equity(self, result: PropertyResearch) -> None:
        result.equity = assess_equity(
            estimated_value=result.estimated_value.value,
            mortgage_balance=result.mortgage_balance.value,
            liens=result.liens.value,
            reported_equity=None,
            asking_price=result.current_price.value,
            value_confidence=result.estimated_value.confidence,
            mortgage_confidence=result.mortgage_balance.confidence,
        )

    def _derive_high_equity_signal(self, result: PropertyResearch) -> None:
        """Set the high-equity signal only from a calculated position.

        A derived spread is not equity, so it must not light up the
        high-equity signal that the lead score pays points for.
        """
        existing = result.distress.get("high_equity")
        if existing.is_known and existing.confidence.rank >= Confidence.MEDIUM.rank:
            return
        if not result.equity.is_verified_enough_to_lean_on:
            return
        result.distress.signals["high_equity"] = Fact.derived(
            result.equity.is_high_equity,
            f"calculated equity is {result.equity.equity_percentage * 100:.0f}% of value",
            result.equity.equity_confidence,
        )

    def _overall_confidence(self, result: PropertyResearch) -> Confidence:
        """One reading for how solid this research is.

        Driven by the two facts everything else rests on — value and price —
        and knocked down when the record is mostly empty.
        """
        anchors = [result.estimated_value, result.current_price]
        known = [f for f in anchors if f.is_known]
        if not known:
            return Confidence.UNKNOWN
        base = min(known, key=lambda f: f.confidence.rank).confidence
        if result.completeness < 0.35 and base.rank > Confidence.LOW.rank:
            return Confidence(
                {3: "MEDIUM", 2: "LOW"}.get(base.rank, base.value)
            )
        return base
