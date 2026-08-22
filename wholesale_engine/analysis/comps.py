"""Comparable-sales evaluation and comp-derived ARV.

The engine grades every comp the user supplies against the subject property,
in the priority order the underwriting rules require:

1. Closed sales over pending over active
2. Same property type
3. Similar beds / baths
4. Similar square footage
5. Similar age and condition
6. Closest geographic proximity
7. Most recent sales

Comps are never invented. If the user supplies none, the analysis says
"no comps supplied" and the ARV falls back to whatever the user asserted —
clearly labelled as unverified.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from ..config import DEFAULT_CONFIG, EngineConfig
from ..models.enums import CompConfidence, PropertyType, SaleStatus
from ..models.property import Comp, PropertyLead
from ..models.results import CompAnalysis, CompEvaluation

#: Relative importance of each comparison criterion. Sums to 1.0.
CRITERION_WEIGHTS = {
    "sale_status": 0.22,
    "sqft": 0.16,
    "property_type": 0.14,
    "beds_baths": 0.14,
    "proximity": 0.14,
    "age": 0.10,
    "recency": 0.10,
}

#: Sub-score used whenever a criterion cannot be evaluated. Deliberately below
#: 1.0: unknown data must never score as well as a confirmed match.
_UNKNOWN_SCORE = 0.45

_STATUS_SCORES = {
    SaleStatus.CLOSED: 1.00,
    SaleStatus.PENDING: 0.55,
    SaleStatus.ACTIVE: 0.30,
    SaleStatus.UNKNOWN: 0.25,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _score_status(comp: Comp) -> Tuple[float, Optional[str]]:
    score = _STATUS_SCORES[comp.sale_status]
    if comp.sale_status is SaleStatus.ACTIVE:
        return score, "active listing, not a closed sale — asking price is not evidence of value"
    if comp.sale_status is SaleStatus.PENDING:
        return score, "pending sale, final price not confirmed"
    if comp.sale_status is SaleStatus.UNKNOWN:
        return score, "sale status unknown"
    return score, None


def _score_property_type(lead: PropertyLead, comp: Comp) -> Tuple[float, Optional[str]]:
    if lead.property_type is PropertyType.UNKNOWN or comp.property_type is PropertyType.UNKNOWN:
        return _UNKNOWN_SCORE, "property type not confirmed on both sides"
    if lead.property_type is comp.property_type:
        return 1.0, None
    return 0.25, f"different property type ({comp.property_type} vs {lead.property_type})"


def _score_beds_baths(lead: PropertyLead, comp: Comp) -> Tuple[float, Optional[str]]:
    if lead.beds is None or comp.beds is None:
        return _UNKNOWN_SCORE, "bed count missing on subject or comp"
    penalty = 0.25 * abs(lead.beds - comp.beds)
    note = None
    if lead.baths is not None and comp.baths is not None:
        penalty += 0.20 * abs(lead.baths - comp.baths)
    else:
        penalty += 0.10
        note = "bath count missing on subject or comp"
    score = _clamp(1.0 - penalty)
    if score < 0.6 and note is None:
        note = f"bed/bath mismatch ({comp.beds}bd/{comp.baths}ba vs subject)"
    return score, note


def _score_sqft(lead: PropertyLead, comp: Comp) -> Tuple[float, Optional[str]]:
    if not lead.sqft or not comp.sqft:
        return _UNKNOWN_SCORE, "square footage missing on subject or comp"
    ratio = min(lead.sqft, comp.sqft) / max(lead.sqft, comp.sqft)
    # 100% match -> 1.0, 60% or worse -> 0.0
    score = _clamp((ratio - 0.60) / 0.40)
    note = None
    if ratio < 0.80:
        note = f"size gap: comp {comp.sqft:,} sqft vs subject {lead.sqft:,} sqft"
    return score, note


def _score_age(lead: PropertyLead, comp: Comp) -> Tuple[float, Optional[str]]:
    if lead.year_built is None or comp.year_built is None:
        return _UNKNOWN_SCORE, "year built missing on subject or comp"
    delta = abs(lead.year_built - comp.year_built)
    score = _clamp(1.0 - delta / 40.0)
    note = f"{delta} year age gap" if delta > 20 else None
    return score, note


def _score_proximity(comp: Comp, config: EngineConfig) -> Tuple[float, Optional[str]]:
    if comp.distance_miles is None:
        return 0.40, "distance from subject not supplied"
    if comp.distance_miles <= 0.25:
        return 1.0, None
    span = max(config.comp_max_distance_miles - 0.25, 0.01)
    score = _clamp(1.0 - (comp.distance_miles - 0.25) / span)
    note = f"{comp.distance_miles:.2f} miles away" if comp.distance_miles > 1.0 else None
    return score, note


def _score_recency(comp: Comp, config: EngineConfig, as_of: date) -> Tuple[float, Optional[str]]:
    days = comp.days_old(as_of)
    if days is None:
        return 0.35, "sale date not supplied"
    if days < 0:
        return 0.35, "sale date is in the future — check the data"
    if days <= config.comp_fresh_days:
        return 1.0, None
    span = max(config.comp_max_age_days - config.comp_fresh_days, 1)
    score = _clamp(1.0 - (days - config.comp_fresh_days) / span)
    note = f"sale is {days} days old" if days > config.comp_fresh_days else None
    return score, note


def evaluate_comp(
    lead: PropertyLead,
    comp: Comp,
    config: EngineConfig = DEFAULT_CONFIG,
    as_of: Optional[date] = None,
) -> CompEvaluation:
    """Grade one comp against the subject property, 0.0 - 1.0."""
    reference = as_of or date.today()
    criteria: dict = {}
    reasons: List[str] = []

    for name, (score, note) in {
        "sale_status": _score_status(comp),
        "property_type": _score_property_type(lead, comp),
        "beds_baths": _score_beds_baths(lead, comp),
        "sqft": _score_sqft(lead, comp),
        "age": _score_age(lead, comp),
        "proximity": _score_proximity(comp, config),
        "recency": _score_recency(comp, config, reference),
    }.items():
        criteria[name] = score
        if note:
            reasons.append(note)

    quality = sum(criteria[name] * weight for name, weight in CRITERION_WEIGHTS.items())

    usable_price = comp.sale_price is not None and comp.sale_price > 0
    if not usable_price:
        reasons.append("no sale price supplied — cannot be used for valuation")
        quality = 0.0

    reliable = usable_price and quality >= config.comp_reliability_threshold
    return CompEvaluation(
        comp=comp,
        quality_score=round(quality, 4),
        reliable=reliable,
        criteria=criteria,
        reasons=reasons,
    )


def _comp_confidence(
    reliable: List[CompEvaluation], mean_quality: float, config: EngineConfig
) -> CompConfidence:
    count = len(reliable)
    if count >= config.strong_comp_count and mean_quality >= config.high_quality_threshold:
        return CompConfidence.HIGH
    if count >= config.medium_comp_count and mean_quality >= config.medium_quality_threshold:
        return CompConfidence.MEDIUM
    if count >= 1:
        return CompConfidence.LOW
    return CompConfidence.NONE


def _weighted_average(pairs: List[Tuple[float, float]]) -> Optional[float]:
    """pairs of (value, weight)."""
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in pairs) / total_weight


def analyze_comps(
    lead: PropertyLead,
    config: EngineConfig = DEFAULT_CONFIG,
    as_of: Optional[date] = None,
) -> CompAnalysis:
    """Evaluate every comp on the lead and derive an ARV where the data allows."""
    analysis = CompAnalysis()
    if not lead.comps:
        analysis.notes.append("No comparable sales were supplied with this lead.")
        return analysis

    analysis.evaluations = [evaluate_comp(lead, comp, config, as_of) for comp in lead.comps]
    analysis.reliable_evaluations = [e for e in analysis.evaluations if e.reliable]
    reliable = analysis.reliable_evaluations

    if reliable:
        analysis.mean_quality = sum(e.quality_score for e in reliable) / len(reliable)
    analysis.confidence = _comp_confidence(reliable, analysis.mean_quality, config)

    if not reliable:
        analysis.arv_basis = (
            f"{analysis.count} comp(s) supplied, none met the reliability bar "
            f"(quality >= {config.comp_reliability_threshold:.2f})"
        )
        analysis.notes.append(
            "Every supplied comp was too weak to support a value conclusion."
        )
        return analysis

    # Prefer a price-per-square-foot model; fall back to raw sale prices when
    # square footage is missing on the subject or across the comps.
    psf_pairs = [
        (e.comp.price_per_sqft, e.quality_score)
        for e in reliable
        if e.comp.price_per_sqft is not None
    ]
    if psf_pairs:
        psf_values = [v for v, _ in psf_pairs]
        analysis.price_per_sqft_low = min(psf_values)
        analysis.price_per_sqft_high = max(psf_values)

    if lead.sqft and psf_pairs:
        blended_psf = _weighted_average(psf_pairs)
        if blended_psf is not None:
            analysis.comp_derived_arv = round(blended_psf * lead.sqft, -3)
            analysis.arv_basis = (
                f"{len(psf_pairs)} reliable comp(s) at a quality-weighted "
                f"${blended_psf:,.0f}/sqft x {lead.sqft:,} sqft"
            )
    else:
        price_pairs = [(e.comp.sale_price, e.quality_score) for e in reliable]
        blended_price = _weighted_average(price_pairs)  # type: ignore[arg-type]
        if blended_price is not None:
            analysis.comp_derived_arv = round(blended_price, -3)
            analysis.arv_basis = (
                f"quality-weighted average of {len(price_pairs)} reliable comp sale price(s)"
            )
            analysis.notes.append(
                "Subject square footage unavailable, so comps were blended on raw sale "
                "price instead of price per square foot. This is a coarser method."
            )

    spread_note = _dispersion_note(reliable)
    if spread_note:
        analysis.notes.append(spread_note)

    return analysis


def _dispersion_note(reliable: List[CompEvaluation]) -> Optional[str]:
    """Flag comp sets whose prices disagree with each other."""
    prices = [e.comp.sale_price for e in reliable if e.comp.sale_price]
    if len(prices) < 2:
        return None
    low, high = min(prices), max(prices)
    if low <= 0:
        return None
    spread = (high - low) / low
    if spread > 0.30:
        return (
            f"Reliable comps range from ${low:,.0f} to ${high:,.0f} "
            f"({spread * 100:.0f}% spread) — the comp set does not agree on value."
        )
    return None
