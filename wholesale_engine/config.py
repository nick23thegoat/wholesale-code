"""Every tunable number in the engine lives here.

Nothing in :mod:`wholesale_engine.analysis` hard-codes a threshold; each
function takes an :class:`EngineConfig` (defaulting to :data:`DEFAULT_CONFIG`).
That keeps the underwriting assumptions auditable and lets you run the same
lead through two rule sets without touching the logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

# ---------------------------------------------------------------------------
# Core deal formula
# ---------------------------------------------------------------------------

#: MAO = (ARV x ARV_PERCENTAGE) - repairs - wholesale fee
ARV_PERCENTAGE: float = 0.70

#: Default minimum assignment fee targeted on every deal.
WHOLESALE_FEE: float = 18_000.0


# ---------------------------------------------------------------------------
# Rehab cost tables (preliminary planning numbers, NOT contractor quotes)
# ---------------------------------------------------------------------------

#: condition -> (low, mid, high) dollars per square foot.
REPAIR_COST_PER_SQFT: Mapping[str, Tuple[float, float, float]] = {
    "turnkey": (2.0, 5.0, 9.0),
    "cosmetic": (8.0, 14.0, 22.0),
    "moderate": (20.0, 30.0, 45.0),
    "heavy": (40.0, 58.0, 80.0),
    "teardown": (70.0, 95.0, 130.0),
}

#: condition -> (low, mid, high) flat dollars, used when square footage is unknown.
REPAIR_FLAT_FALLBACK: Mapping[str, Tuple[float, float, float]] = {
    "turnkey": (3_000.0, 8_000.0, 15_000.0),
    "cosmetic": (10_000.0, 18_000.0, 30_000.0),
    "moderate": (25_000.0, 40_000.0, 60_000.0),
    "heavy": (50_000.0, 75_000.0, 110_000.0),
    "teardown": (90_000.0, 130_000.0, 180_000.0),
}


@dataclass(frozen=True)
class EngineConfig:
    """Underwriting assumptions for one analysis run."""

    # --- deal formula -----------------------------------------------------
    arv_percentage: float = ARV_PERCENTAGE
    wholesale_fee: float = WHOLESALE_FEE
    #: A spread below this is not worth contracting at the recommended offer.
    min_acceptable_spread: float = WHOLESALE_FEE

    # --- offer construction ----------------------------------------------
    #: The engine never recommends paying full MAO; this is the floor haircut.
    min_offer_discount: float = 0.03
    #: Ceiling on the risk haircut applied below MAO.
    max_offer_discount: float = 0.28
    #: Recommended offers are rounded down to a multiple of this.
    offer_rounding: float = 500.0

    # --- comp evaluation --------------------------------------------------
    #: A comp scoring below this is not used to derive ARV.
    comp_reliability_threshold: float = 0.55
    #: Reliable comps needed before ARV can be called VERIFIED/SUPPORTED.
    strong_comp_count: int = 3
    #: Reliable comps needed before ARV is more than a rough estimate.
    medium_comp_count: int = 2
    #: Mean quality required for HIGH comp confidence.
    high_quality_threshold: float = 0.72
    #: Mean quality required for MEDIUM comp confidence.
    medium_quality_threshold: float = 0.60
    #: Comps beyond this distance score zero on proximity.
    comp_max_distance_miles: float = 2.0
    #: Comps older than this score zero on recency.
    comp_max_age_days: int = 365
    #: Sales within this window are treated as fully current.
    comp_fresh_days: int = 90

    # --- ARV reconciliation ----------------------------------------------
    #: |user ARV - comp ARV| above this fraction is a conflict worth flagging.
    arv_conflict_tolerance: float = 0.07
    #: Above this fraction the conflict is severe.
    arv_major_conflict_tolerance: float = 0.15

    # --- risk thresholds --------------------------------------------------
    #: Repairs above this fraction of ARV signal a heavy, capital-intensive project.
    heavy_repair_ratio: float = 0.25
    #: Repairs above this fraction of ARV are a red flag for a wholesale exit.
    extreme_repair_ratio: float = 0.40
    #: Asking above this fraction of ARV means there is little room for anyone.
    overpriced_ratio: float = 0.85
    #: Homes built before this year commonly carry lead paint / asbestos risk.
    lead_paint_year: int = 1978
    #: Homes built before this year commonly carry systems-replacement risk.
    old_systems_year: int = 1950
    #: Age multipliers applied to condition-based rehab estimates.
    pre_lead_paint_multiplier: float = 1.10
    pre_1950_multiplier: float = 1.20
    #: Contingency added to the high rehab scenario.
    rehab_contingency: float = 0.10
    #: Multipliers applied around a user-supplied repair number to build a band.
    user_repair_mid_multiplier: float = 1.15
    user_repair_high_multiplier: float = 1.35
    #: A user repair figure below this fraction of the condition-based low is suspect.
    user_repair_suspicious_ratio: float = 0.70
    #: Rent-to-price ratio (monthly rent / ARV) that reads as a strong rental exit.
    strong_rent_ratio: float = 0.01

    # --- scoring ----------------------------------------------------------
    score_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "discount_from_arv": 18.0,
            "wholesale_spread": 16.0,
            "comp_quality": 14.0,
            "repair_risk": 12.0,
            "seller_motivation": 10.0,
            "condition": 8.0,
            "marketability": 8.0,
            "equity_potential": 8.0,
            "data_confidence": 6.0,
        }
    )

    #: Lower bound (inclusive) of each classification band.
    classification_bands: Dict[str, float] = field(
        default_factory=lambda: {
            "HOT": 90.0,
            "STRONG": 75.0,
            "POSSIBLE": 60.0,
            "WEAK": 40.0,
        }
    )

    # --- decision gates ---------------------------------------------------
    #: Minimum score for a GO once every data gate has been cleared.
    go_score_threshold: float = 75.0
    #: Below this score the engine will not even negotiate.
    negotiate_score_threshold: float = 40.0
    #: Asking above MAO by more than this fraction is likely not bridgeable.
    max_negotiable_gap: float = 0.40

    def weight(self, name: str) -> float:
        return self.score_weights.get(name, 0.0)


#: Shared default instance. Pass a different one to override any assumption.
DEFAULT_CONFIG = EngineConfig()


# ---------------------------------------------------------------------------
# Fields the engine considers critical. Missing any of these means the deal is
# gated to "NEEDS MORE DATA" no matter how good the score looks.
# ---------------------------------------------------------------------------

CRITICAL_FIELDS: Tuple[str, ...] = (
    "asking_price",
    "arv_basis",
    "repair_basis",
)

#: Fields that materially improve an analysis but do not block it.
IMPORTANT_FIELDS: Tuple[str, ...] = (
    "sqft",
    "beds",
    "baths",
    "year_built",
    "condition",
    "occupancy",
    "property_type",
    "county",
    "comps",
    "seller_motivation",
    "days_on_market",
    "estimated_monthly_rent",
    "lot_size_sqft",
)
