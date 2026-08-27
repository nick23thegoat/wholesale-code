"""Wave 2 lead models.

A :class:`Lead` is what a lead source produces: raw, possibly incomplete
information about a property that might be worth pursuing. It is deliberately
*not* a second copy of :class:`~wholesale_engine.models.property.PropertyLead`
— it carries the lead-generation signals (absentee, probate, tax delinquent…)
that Wave 1 has no concept of, and it converts into a ``PropertyLead`` via
:meth:`Lead.to_property_lead` so the existing analyzer does all the
underwriting exactly as before.

Three-state booleans are used throughout for the signals: ``True`` (reported),
``False`` (reported as not applicable) and ``None`` (unknown). ``None`` never
scores and never rejects — it becomes a NEEDS VERIFICATION line instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import LEAD_SIGNALS
from ..models.enums import (
    Classification,
    Condition,
    Occupancy,
    PropertyType,
    SellerMotivation,
)
from ..models.property import Comp, PropertyLead
from ..models.results import AnalysisResult

#: Signal fields carried on every lead, in report order.
SIGNAL_FIELDS = LEAD_SIGNALS

#: Human labels for the signal fields.
SIGNAL_LABELS: Dict[str, str] = {
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
    "significant_repairs": "significant repairs",
    "seller_motivation": "seller motivation",
}


@dataclass
class Lead:
    """One normalized lead from any source.

    Every unknown stays ``None`` or blank. Nothing here is verified against a
    public record — these are claims from whatever produced the row.
    """

    # --- identity ---------------------------------------------------------
    lead_id: str = ""
    property_id: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    county: str = ""
    zip_code: str = ""
    owner_name: str = ""

    # --- money ------------------------------------------------------------
    asking_price: Optional[float] = None
    estimated_value: Optional[float] = None
    estimated_repairs: Optional[float] = None
    estimated_equity: Optional[float] = None

    # --- physical ---------------------------------------------------------
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    property_type: PropertyType = PropertyType.UNKNOWN
    occupancy: Occupancy = Occupancy.UNKNOWN
    condition: Condition = Condition.UNKNOWN

    # --- signals (tri-state: True / False / None=unknown) -----------------
    absentee_owner: Optional[bool] = None
    vacant: Optional[bool] = None
    tax_delinquent: Optional[bool] = None
    pre_foreclosure: Optional[bool] = None
    foreclosure: Optional[bool] = None
    probate: Optional[bool] = None
    inherited: Optional[bool] = None
    code_violation: Optional[bool] = None
    high_equity: Optional[bool] = None
    tired_landlord: Optional[bool] = None

    # --- market / seller --------------------------------------------------
    days_on_market: Optional[int] = None
    seller_motivation: SellerMotivation = SellerMotivation.UNKNOWN

    # --- provenance -------------------------------------------------------
    source: str = ""
    source_url: str = ""
    notes: str = ""

    # --- evidence ---------------------------------------------------------
    #: Comparable sales supplied alongside the lead list, if any. Without
    #: these the ARV stays SOURCE-PROVIDED and the deal cannot clear the
    #: Wave 1 verification gate.
    comps: List[Comp] = field(default_factory=list)

    # --- bookkeeping filled in by the pipeline ----------------------------
    normalized_address: str = ""
    normalized_city: str = ""
    normalized_state: str = ""
    normalized_zip: str = ""
    merged_from: List[str] = field(default_factory=list)
    needs_verification: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    raw: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    @property
    def dedupe_key(self) -> tuple:
        """Address-based identity used for duplicate detection."""
        return (self.normalized_address, self.normalized_city, self.normalized_state)

    @property
    def equity_estimate(self) -> Optional[float]:
        """Reported equity, or value minus asking price when both are known.

        Returns ``None`` when neither is available — the engine does not guess
        at equity, and it has no access to mortgage balances.
        """
        if self.estimated_equity is not None:
            return self.estimated_equity
        if self.estimated_value is not None and self.asking_price is not None:
            return self.estimated_value - self.asking_price
        return None

    @property
    def equity_is_derived(self) -> bool:
        """True when equity was computed rather than reported."""
        return self.estimated_equity is None and self.equity_estimate is not None

    @property
    def equity_ratio(self) -> Optional[float]:
        equity = self.equity_estimate
        if equity is None or not self.estimated_value:
            return None
        return equity / self.estimated_value

    def signal_values(self) -> Dict[str, Optional[bool]]:
        """The ten reported signals as ``{name: True/False/None}``."""
        return {name: getattr(self, name) for name in SIGNAL_FIELDS}

    def confirmed_signals(self) -> List[str]:
        return [name for name, value in self.signal_values().items() if value is True]

    def unknown_signals(self) -> List[str]:
        return [name for name, value in self.signal_values().items() if value is None]

    def display_id(self) -> str:
        return self.lead_id or self.property_id or self.address or "(unidentified lead)"

    def full_address(self) -> str:
        parts = [self.address, self.city, self.state, self.zip_code]
        return ", ".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Bridge into Wave 1
    # ------------------------------------------------------------------

    def to_property_lead(self) -> PropertyLead:
        """Convert to the Wave 1 model so the existing analyzer can run.

        The source's ``estimated_value`` becomes ``user_arv``: Wave 1 already
        treats a supplied ARV as an unverified claim and will only upgrade it
        to VERIFIED/SUPPORTED when comps back it up. Confirmed signals ride
        along as distress indicators, which Wave 1 reports back as *reported,
        not verified*.
        """
        indicators = [SIGNAL_LABELS.get(name, name) for name in self.confirmed_signals()]
        return PropertyLead(
            property_id=self.property_id or self.lead_id,
            address=self.address,
            city=self.city,
            state=self.state,
            county=self.county,
            zip_code=self.zip_code,
            beds=self.beds,
            baths=self.baths,
            sqft=self.sqft,
            year_built=self.year_built,
            property_type=self.property_type,
            occupancy=self.occupancy,
            condition=self.condition,
            asking_price=self.asking_price,
            user_arv=self.estimated_value,
            user_repair_estimate=self.estimated_repairs,
            days_on_market=self.days_on_market,
            seller_motivation=self.seller_motivation,
            distress_indicators=indicators,
            notes=self.notes,
            comps=list(self.comps),
            source=self.source or "lead-hunter",
        )


@dataclass(frozen=True)
class SignalHit:
    """One signal that contributed points to the lead score."""

    name: str
    points: float
    basis: str

    @property
    def label(self) -> str:
        return SIGNAL_LABELS.get(self.name, self.name)


@dataclass
class LeadScore:
    """The 0-100 LEAD score. Separate from, and never a substitute for, the
    Wave 1 DEAL score."""

    total: float = 0.0
    classification: Classification = Classification.PASS
    hits: List[SignalHit] = field(default_factory=list)
    unknown_signals: List[str] = field(default_factory=list)
    suppressed: List[str] = field(default_factory=list)  # de-duplicated group members

    @property
    def signal_names(self) -> List[str]:
        return [hit.name for hit in self.hits]

    def summary(self) -> str:
        if not self.hits:
            return "no confirmed lead signals"
        return ", ".join(f"{hit.label} +{hit.points:g}" for hit in self.hits)


@dataclass
class FilterOutcome:
    """Why a lead was kept or dropped."""

    passed: bool = True
    reasons: List[str] = field(default_factory=list)  # why it was dropped
    warnings: List[str] = field(default_factory=list)  # unknowns worth chasing

    def reject(self, reason: str) -> None:
        self.passed = False
        self.reasons.append(reason)

    def warn(self, warning: str) -> None:
        self.warnings.append(warning)


# Pipeline status values.
STATUS_ANALYZED = "analyzed"
STATUS_FILTERED = "filtered_out"
STATUS_BELOW_DEAL_SCORE = "below_min_deal_score"
STATUS_DUPLICATE = "duplicate_merged"

# ARV provenance labels (Wave 2 vocabulary layered over Wave 1 confidence).
ARV_SOURCE_PROVIDED = "SOURCE-PROVIDED — NEEDS ARV VERIFICATION"
ARV_NEEDS_VERIFICATION = "NEEDS ARV VERIFICATION"
ARV_COMP_ESTIMATED = "ESTIMATED FROM COMPS"
ARV_COMP_SUPPORTED = "VERIFIED/SUPPORTED BY COMPS"


@dataclass
class LeadResult:
    """One lead all the way through the pipeline."""

    lead: Lead
    score: LeadScore
    filter_outcome: FilterOutcome
    analysis: Optional[AnalysisResult] = None
    status: str = STATUS_ANALYZED
    arv_status: str = ARV_NEEDS_VERIFICATION

    @property
    def deal_score(self) -> Optional[float]:
        return None if self.analysis is None else self.analysis.score.total

    @property
    def deal_classification(self) -> Optional[Classification]:
        return None if self.analysis is None else self.analysis.score.classification

    @property
    def potential_spread(self) -> Optional[float]:
        if self.analysis is None:
            return None
        return self.analysis.financials.potential_gross_spread

    @property
    def is_hot_lead(self) -> bool:
        return self.score.classification in (Classification.HOT, Classification.STRONG)


@dataclass
class LeadPipelineReport:
    """Everything one pipeline run produced."""

    results: List[LeadResult] = field(default_factory=list)
    duplicates: List[Lead] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_name: str = ""
    rows_read: int = 0

    @property
    def analyzed(self) -> List[LeadResult]:
        return [r for r in self.results if r.status == STATUS_ANALYZED]

    @property
    def filtered_out(self) -> List[LeadResult]:
        return [r for r in self.results if r.status != STATUS_ANALYZED]

    def __len__(self) -> int:
        return len(self.results)
