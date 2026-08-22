"""Input data models: the lead the user supplies and the comps attached to it.

These are plain :mod:`dataclasses` on purpose — the engine has zero runtime
dependencies, so it runs anywhere Python 3.9+ runs. Every future data source
(property API, county records, comp feed) is expected to produce these same
objects, so the rest of the engine never learns where the data came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from .enums import Condition, Occupancy, PropertyType, SaleStatus, SellerMotivation


@dataclass
class Comp:
    """A single comparable sale supplied by the user.

    The engine never invents comps. If this list is empty, the engine says so.
    """

    address: str = ""
    sale_price: Optional[float] = None
    sale_status: SaleStatus = SaleStatus.UNKNOWN
    sale_date: Optional[date] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    lot_size_sqft: Optional[int] = None
    distance_miles: Optional[float] = None
    property_type: PropertyType = PropertyType.UNKNOWN
    condition: Condition = Condition.UNKNOWN
    source: str = "user-provided"
    notes: str = ""

    @property
    def price_per_sqft(self) -> Optional[float]:
        if self.sale_price is None or not self.sqft:
            return None
        return self.sale_price / self.sqft

    def days_old(self, as_of: Optional[date] = None) -> Optional[int]:
        if self.sale_date is None:
            return None
        reference = as_of or date.today()
        return (reference - self.sale_date).days

    def label(self) -> str:
        return self.address or "(unnamed comp)"


@dataclass
class PropertyLead:
    """Everything the user knows about one lead.

    Optional fields default to ``None`` rather than to a guess. The analyzer
    reports missing fields instead of filling them in.
    """

    # Identity / location
    property_id: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    county: str = ""
    zip_code: str = ""

    # Physical characteristics
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_size_sqft: Optional[int] = None
    year_built: Optional[int] = None
    property_type: PropertyType = PropertyType.UNKNOWN
    occupancy: Occupancy = Occupancy.UNKNOWN
    condition: Condition = Condition.UNKNOWN

    # Money
    asking_price: Optional[float] = None
    user_arv: Optional[float] = None
    user_repair_estimate: Optional[float] = None
    estimated_monthly_rent: Optional[float] = None
    annual_taxes: Optional[float] = None

    # Market / seller context
    days_on_market: Optional[int] = None
    seller_motivation: SellerMotivation = SellerMotivation.UNKNOWN
    distress_indicators: List[str] = field(default_factory=list)
    notes: str = ""

    # Evidence
    comps: List[Comp] = field(default_factory=list)

    # Provenance — set by whichever loader produced this lead.
    source: str = "manual"

    @property
    def full_address(self) -> str:
        parts = [self.address, self.city, self.state]
        return ", ".join(p for p in parts if p)

    @property
    def age_years(self) -> Optional[int]:
        if self.year_built is None:
            return None
        return date.today().year - self.year_built

    @property
    def asking_price_per_sqft(self) -> Optional[float]:
        if self.asking_price is None or not self.sqft:
            return None
        return self.asking_price / self.sqft

    def display_id(self) -> str:
        return self.property_id or self.address or "(unidentified lead)"
