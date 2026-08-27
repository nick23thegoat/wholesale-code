"""The normalized research result: everything known about one property.

One object, assembled from every source available, with each field carrying
its own provenance. This is what the dossier renders, what the priority engine
reads, and what a real provider will fill in more of later — without anything
downstream changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from ..models.enums import Condition, Occupancy, PropertyType
from .distress import DistressProfile
from .equity import EquityAssessment
from .facts import Confidence, Fact, missing_names
from .owner_research import OwnerRecord

#: Tax status vocabulary. UNKNOWN is the default and stays that way without
#: a public-record source.
TAX_STATUS_CURRENT = "CURRENT"
TAX_STATUS_DELINQUENT = "DELINQUENT"
TAX_STATUS_UNKNOWN = "UNKNOWN"

#: Foreclosure status vocabulary.
FORECLOSURE_NONE = "NONE REPORTED"
FORECLOSURE_PRE = "PRE-FORECLOSURE"
FORECLOSURE_ACTIVE = "FORECLOSURE"
FORECLOSURE_UNKNOWN = "UNKNOWN"


@dataclass
class PropertyResearch:
    """Normalized research on one property.

    Identity and geography are plain strings because they come from the lead
    and are not in doubt. Everything researched is a
    :class:`~wholesale_engine.research.facts.Fact`, so a report can always say
    where it came from and how much to trust it.
    """

    # --- identity -------------------------------------------------------
    property_id: str = ""
    lead_id: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    county: str = ""
    zip_code: str = ""

    # --- physical -------------------------------------------------------
    property_type: PropertyType = PropertyType.UNKNOWN
    beds: Fact[float] = field(default_factory=Fact.unknown)
    baths: Fact[float] = field(default_factory=Fact.unknown)
    sqft: Fact[int] = field(default_factory=Fact.unknown)
    year_built: Fact[int] = field(default_factory=Fact.unknown)
    lot_size: Fact[float] = field(default_factory=Fact.unknown)
    occupancy: Occupancy = Occupancy.UNKNOWN
    condition: Condition = Condition.UNKNOWN

    # --- money ----------------------------------------------------------
    estimated_value: Fact[float] = field(default_factory=Fact.unknown)
    current_price: Fact[float] = field(default_factory=Fact.unknown)
    last_sale_price: Fact[float] = field(default_factory=Fact.unknown)
    last_sale_date: Fact[date] = field(default_factory=Fact.unknown)
    tax_amount: Fact[float] = field(default_factory=Fact.unknown)
    tax_status: Fact[str] = field(default_factory=Fact.unknown)
    mortgage_balance: Fact[float] = field(default_factory=Fact.unknown)
    liens: Fact[float] = field(default_factory=Fact.unknown)
    estimated_repairs: Fact[float] = field(default_factory=Fact.unknown)

    # --- market ---------------------------------------------------------
    days_on_market: Fact[int] = field(default_factory=Fact.unknown)

    # --- assembled sub-reports ------------------------------------------
    owner: OwnerRecord = field(default_factory=OwnerRecord)
    distress: DistressProfile = field(default_factory=DistressProfile)
    equity: EquityAssessment = field(default_factory=EquityAssessment)

    # --- status rollups (derived from distress, kept as named fields) ----
    foreclosure_status: Fact[str] = field(default_factory=Fact.unknown)

    # --- provenance -----------------------------------------------------
    source: str = ""
    sources_used: List[str] = field(default_factory=list)
    source_confidence: Confidence = Confidence.UNKNOWN
    researched_on: Optional[date] = None
    notes: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors, so callers do not reach through .value
    # ------------------------------------------------------------------

    @property
    def researched_fields(self) -> Dict[str, Fact]:
        """The fact-bearing fields, in report order."""
        return {
            "beds": self.beds,
            "baths": self.baths,
            "sqft": self.sqft,
            "year_built": self.year_built,
            "lot_size": self.lot_size,
            "estimated_value": self.estimated_value,
            "current_price": self.current_price,
            "last_sale_price": self.last_sale_price,
            "last_sale_date": self.last_sale_date,
            "tax_amount": self.tax_amount,
            "tax_status": self.tax_status,
            "mortgage_balance": self.mortgage_balance,
            "liens": self.liens,
            "estimated_repairs": self.estimated_repairs,
            "days_on_market": self.days_on_market,
            "foreclosure_status": self.foreclosure_status,
        }

    @property
    def missing_fields(self) -> List[str]:
        """Everything still unknown, property fields plus owner fields.

        Distress signals are excluded — an unknown signal is normal and is
        reported separately, not as a gap in the research.
        """
        missing = missing_names(self.researched_fields)
        missing += [f"owner.{name}" for name in self.owner.missing_fields]
        if not self.equity.is_known:
            missing.append("equity")
        if self.property_type is PropertyType.UNKNOWN:
            missing.append("property_type")
        if self.occupancy is Occupancy.UNKNOWN:
            missing.append("occupancy")
        if self.condition is Condition.UNKNOWN:
            missing.append("condition")
        return missing

    @property
    def known_field_count(self) -> int:
        return sum(1 for f in self.researched_fields.values() if f.is_known)

    @property
    def completeness(self) -> float:
        """0.0-1.0: how much of the research surface is actually filled in."""
        total = len(self.researched_fields)
        return self.known_field_count / total if total else 0.0

    # --- flattened booleans, for filters and exports --------------------

    @property
    def vacant(self) -> Optional[bool]:
        return self.distress.get("vacant").value

    @property
    def absentee_owner(self) -> Optional[bool]:
        return self.owner.absentee_owner.value

    @property
    def pre_foreclosure(self) -> Optional[bool]:
        return self.distress.get("pre_foreclosure").value

    @property
    def foreclosure(self) -> Optional[bool]:
        return self.distress.get("foreclosure").value

    @property
    def tax_delinquent(self) -> Optional[bool]:
        return self.distress.get("tax_delinquent").value

    @property
    def probate(self) -> Optional[bool]:
        return self.distress.get("probate").value

    @property
    def inherited(self) -> Optional[bool]:
        return self.distress.get("inherited").value

    @property
    def code_violation(self) -> Optional[bool]:
        return self.distress.get("code_violation").value

    @property
    def estimated_equity(self) -> Optional[float]:
        return self.equity.equity_amount

    @property
    def owner_name(self) -> Optional[str]:
        return self.owner.owner_name.value

    @property
    def owner_mailing_address(self) -> Optional[str]:
        return self.owner.owner_mailing_address.value

    def full_address(self) -> str:
        parts = [self.address, self.city, self.state, self.zip_code]
        return ", ".join(p for p in parts if p)

    def display_id(self) -> str:
        return self.property_id or self.lead_id or self.address or "(unidentified)"

    def as_dict(self) -> Dict[str, Any]:
        """Flat export view. Facts collapse to their value; unknown stays None."""
        row: Dict[str, Any] = {
            "property_id": self.property_id,
            "lead_id": self.lead_id,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "county": self.county,
            "zip": self.zip_code,
            "property_type": str(self.property_type),
            "occupancy": str(self.occupancy),
            "condition": str(self.condition),
        }
        for name, fact in self.researched_fields.items():
            row[name] = fact.value
        row.update(
            {
                "owner_name": self.owner_name,
                "owner_mailing_address": self.owner_mailing_address,
                "absentee_owner": self.absentee_owner,
                "estimated_equity": self.equity.equity_amount,
                "equity_percentage": self.equity.equity_percentage,
                "equity_status": str(self.equity.equity_status),
                "equity_confidence": str(self.equity.equity_confidence),
                "pre_foreclosure": self.pre_foreclosure,
                "tax_delinquent": self.tax_delinquent,
                "probate": self.probate,
                "inherited": self.inherited,
                "code_violation": self.code_violation,
                "vacant": self.vacant,
                "distress_count": self.distress.count,
                "distress_signals": ", ".join(self.distress.labelled()),
                "source": self.source,
                "source_confidence": str(self.source_confidence),
                "missing_fields": ", ".join(self.missing_fields),
            }
        )
        return row
