"""Every tunable number in the engine lives here.

Nothing in :mod:`wholesale_engine.analysis` hard-codes a threshold; each
function takes an :class:`EngineConfig` (defaulting to :data:`DEFAULT_CONFIG`).
That keeps the underwriting assumptions auditable and lets you run the same
lead through two rule sets without touching the logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Core deal formula
# ---------------------------------------------------------------------------

#: MAO = (ARV x ARV_PERCENTAGE) - repairs - wholesale fee
ARV_PERCENTAGE: float = 0.70

#: Default TARGET assignment fee. This is the fee you are aiming for on every
#: deal. It is deliberately NOT a minimum: a deal that supports less is
#: labelled BELOW TARGET, not rejected. The deal score decides.
TARGET_WHOLESALE_FEE: float = 18_000.0

#: Backwards-compatible alias for the module-level constant.
WHOLESALE_FEE: float = TARGET_WHOLESALE_FEE


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
    #: The fee you are TARGETING. MAO reserves exactly this much for you, so a
    #: purchase at MAO yields exactly this fee and no more.
    #:
    #: TARGET, NOT MINIMUM. Nothing in the engine rejects or downgrades a deal
    #: for coming in under it. A shortfall is labelled BELOW TARGET, raised as
    #: a risk flag, and fed continuously into the ``wholesale_spread`` score
    #: component — where a $13,000 fee scores lower than an $18,000 one but
    #: still competes on the strength of the rest of the deal.
    target_wholesale_fee: float = TARGET_WHOLESALE_FEE
    #: The fee below which the engine stops calling a deal a green light.
    #:
    #: This is an ECONOMIC VIABILITY floor, not the target. It exists because
    #: "GO" has to mean something — a deal supporting $2,800 is not a go at any
    #: score — but it sits well under the target on purpose, so a $13,000
    #: assignment on an otherwise strong deal still reaches GO carrying a
    #: BELOW TARGET flag. Set it to 0.0 (``--min-fee 0``) to remove the floor
    #: entirely and let the deal score decide alone.
    min_viable_wholesale_fee: float = 10_000.0

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

    @property
    def wholesale_fee(self) -> float:
        """Backwards-compatible alias for :attr:`target_wholesale_fee`."""
        return self.target_wholesale_fee

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


# ===========================================================================
# Wave 2 — lead hunter configuration
# ===========================================================================
#
# Kept separate from :class:`EngineConfig` on purpose. The LEAD score answers
# "is this worth calling?" and the DEAL score answers "is this worth buying?".
# They must never be conflated: a HOT lead can still be a terrible deal.

#: Markets the lead hunter targets out of the box.
DEFAULT_TARGET_STATES: Tuple[str, ...] = ("FL", "TX", "MO")

#: Markets pre-listed for easy expansion. Add them to ``target_states`` (or
#: pass --states on the CLI) when you are ready; nothing else needs to change.
EXPANSION_STATES: Tuple[str, ...] = (
    "AL", "LA", "TN", "GA", "MS", "AR", "SC", "NC", "KY", "OK",
)

#: Property types the hunter pursues. Commercial and land are deliberately
#: absent — they do not fit the ARV/rehab model this engine underwrites with.
DEFAULT_PROPERTY_TYPES: Tuple[str, ...] = (
    "single_family", "duplex", "triplex", "fourplex",
)

#: Distress/opportunity signals the hunter recognises, in report order.
LEAD_SIGNALS: Tuple[str, ...] = (
    "absentee_owner",
    "vacant",
    "high_equity",
    "pre_foreclosure",
    "foreclosure",
    "tax_delinquent",
    "probate",
    "inherited",
    "code_violation",
    "tired_landlord",
)


@dataclass(frozen=True)
class LeadHunterConfig:
    """Targeting, scoring and filtering rules for the lead hunter."""

    # --- targeting --------------------------------------------------------
    target_states: Tuple[str, ...] = DEFAULT_TARGET_STATES
    preferred_property_types: Tuple[str, ...] = DEFAULT_PROPERTY_TYPES

    # --- lead score -------------------------------------------------------
    signal_points: Dict[str, float] = field(
        default_factory=lambda: {
            "absentee_owner": 10.0,
            "vacant": 10.0,
            "high_equity": 15.0,
            "pre_foreclosure": 15.0,
            "foreclosure": 15.0,
            "tax_delinquent": 10.0,
            "probate": 10.0,
            "inherited": 10.0,
            "code_violation": 10.0,
            "tired_landlord": 10.0,
            "significant_repairs": 10.0,
        }
    )

    #: Signals describing the same underlying event score once, at the highest
    #: value in the group — pre-foreclosure and foreclosure are one situation,
    #: not two, and probate and inherited usually are as well.
    exclusive_signal_groups: Tuple[Tuple[str, ...], ...] = (
        ("foreclosure", "pre_foreclosure"),
        ("probate", "inherited"),
    )

    #: Motivation only earns points when motivation information was actually
    #: supplied. UNKNOWN is worth nothing — silence is not motivation.
    motivation_points: Dict[str, float] = field(
        default_factory=lambda: {"high": 15.0, "moderate": 7.0, "low": 0.0}
    )

    #: A rehab at or above this figure counts as the "significant repairs" signal.
    significant_repair_threshold: float = 25_000.0
    #: Conditions that count as significant repairs even without a dollar figure.
    significant_repair_conditions: Tuple[str, ...] = ("heavy", "teardown")
    #: Derived equity at or above this share of estimated value reads as high equity.
    high_equity_ratio: float = 0.35

    max_lead_score: float = 100.0
    classification_bands: Dict[str, float] = field(
        default_factory=lambda: {"HOT": 90.0, "STRONG": 75.0, "POSSIBLE": 60.0, "WEAK": 40.0}
    )
    #: Classifications that qualify a lead for hot_leads.csv.
    hot_lead_classifications: Tuple[str, ...] = ("HOT", "STRONG")

    # --- filters ----------------------------------------------------------
    # A missing value never rejects a lead on its own. Unknowns are recorded
    # as NEEDS VERIFICATION so you can go find the answer instead of losing
    # the lead to a blank cell.
    min_lead_score: float = 0.0
    min_deal_score: float = 0.0
    max_asking_price: Optional[float] = None
    min_equity: Optional[float] = None
    #: Empty means "no occupancy filter"; e.g. ("vacant",) to hunt vacants only.
    allowed_occupancy: Tuple[str, ...] = ()
    #: Empty means "no signal required"; e.g. ("probate", "vacant") for any-of.
    required_signals: Tuple[str, ...] = ()
    min_signal_count: int = 0

    def signal_value(self, signal: str) -> float:
        return self.signal_points.get(signal, 0.0)

    def targets_state(self, state: str) -> bool:
        return state.strip().upper() in {s.upper() for s in self.target_states}

    def targets_property_type(self, property_type: str) -> bool:
        return property_type.strip().lower() in {t.lower() for t in self.preferred_property_types}


#: Shared default instance. Use ``dataclasses.replace`` for per-run overrides.
DEFAULT_LEAD_CONFIG = LeadHunterConfig()
