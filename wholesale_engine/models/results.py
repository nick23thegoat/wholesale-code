"""Output data models: what the analyzer produces for a single lead.

Nothing here computes anything — these are transport objects shared by the
analysis modules, the report writers and the CSV exporter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .enums import (
    ARVConfidence,
    Classification,
    CompConfidence,
    Decision,
    RepairConfidence,
    Severity,
)
from .property import Comp, PropertyLead


@dataclass(frozen=True)
class RiskFlag:
    """One concrete concern about the deal."""

    severity: Severity
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.message}"


@dataclass
class CompEvaluation:
    """Scored comparable sale."""

    comp: Comp
    quality_score: float  # 0.0 - 1.0
    reliable: bool
    criteria: dict = field(default_factory=dict)  # criterion -> sub-score
    reasons: List[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        if self.quality_score >= 0.80:
            return "A"
        if self.quality_score >= 0.65:
            return "B"
        if self.quality_score >= 0.50:
            return "C"
        if self.quality_score >= 0.35:
            return "D"
        return "F"

    def summary(self) -> str:
        price = (
            f"${self.comp.sale_price:,.0f}" if self.comp.sale_price is not None else "price unknown"
        )
        return (
            f"{self.comp.label()} — {price} "
            f"({self.comp.sale_status}, quality {self.quality_score:.2f}, grade {self.grade})"
        )


@dataclass
class CompAnalysis:
    """Result of evaluating every comp attached to a lead."""

    evaluations: List[CompEvaluation] = field(default_factory=list)
    reliable_evaluations: List[CompEvaluation] = field(default_factory=list)
    confidence: CompConfidence = CompConfidence.NONE
    comp_derived_arv: Optional[float] = None
    arv_basis: str = "no comps supplied"
    price_per_sqft_low: Optional[float] = None
    price_per_sqft_high: Optional[float] = None
    mean_quality: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.evaluations)

    @property
    def reliable_count(self) -> int:
        return len(self.reliable_evaluations)

    @property
    def best(self) -> Optional[CompEvaluation]:
        return max(self.evaluations, key=lambda e: e.quality_score, default=None)

    @property
    def worst(self) -> Optional[CompEvaluation]:
        return min(self.evaluations, key=lambda e: e.quality_score, default=None)


@dataclass
class ARVAssessment:
    """The after-repair value the engine is willing to underwrite with."""

    arv: Optional[float]
    confidence: ARVConfidence
    source_note: str
    user_arv: Optional[float] = None
    comp_derived_arv: Optional[float] = None
    deviation_pct: Optional[float] = None  # user vs comp-derived, as a fraction

    @property
    def is_usable(self) -> bool:
        return self.arv is not None and self.arv > 0


@dataclass
class RepairEstimate:
    """Low / mid / high rehab band. Never a contractor quote."""

    low: Optional[float]
    mid: Optional[float]
    high: Optional[float]
    base: Optional[float]  # figure used for the headline MAO
    confidence: RepairConfidence
    basis_note: str
    price_per_sqft_used: Optional[float] = None

    @property
    def is_usable(self) -> bool:
        return self.base is not None


@dataclass(frozen=True)
class MAOScenario:
    """MAO recomputed under one rehab scenario."""

    name: str
    repairs: float
    mao: float
    spread_vs_asking: Optional[float] = None


@dataclass
class FinancialSummary:
    arv: Optional[float] = None
    seventy_percent_arv: Optional[float] = None
    repairs_used: Optional[float] = None
    wholesale_fee: float = 0.0
    mao: Optional[float] = None
    recommended_offer: Optional[float] = None
    offer_discount_pct: float = 0.0
    offer_discount_reasons: List[str] = field(default_factory=list)
    assignment_price: Optional[float] = None
    potential_gross_spread: Optional[float] = None
    spread_vs_asking: Optional[float] = None
    discount_from_arv_pct: Optional[float] = None
    scenarios: List[MAOScenario] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    weight: float
    score: float  # 0.0 - 1.0
    note: str

    @property
    def points(self) -> float:
        return self.weight * self.score


@dataclass
class DealScore:
    total: float = 0.0
    classification: Classification = Classification.PASS
    components: List[ScoreComponent] = field(default_factory=list)
    needs_more_data: bool = False


@dataclass
class AnalysisResult:
    """Everything the engine concluded about one lead."""

    lead: PropertyLead
    comps: CompAnalysis
    arv: ARVAssessment
    repairs: RepairEstimate
    financials: FinancialSummary
    score: DealScore
    risk_flags: List[RiskFlag] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    decision: Decision = Decision.NEED_MORE_DATA
    decision_explanation: str = ""

    def flags_by_severity(self) -> List[RiskFlag]:
        order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        return sorted(self.risk_flags, key=lambda f: order[f.severity])
