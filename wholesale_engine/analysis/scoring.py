"""0-100 deal score and its classification band.

The score is a weighted sum of nine independently computed components, each
normalised to 0.0-1.0 and each carrying a one-line explanation so the report
can show its work. Weights live in :class:`~wholesale_engine.config.EngineConfig`.

A high score never overrides missing critical information: when the caller
reports that a critical gate failed, ``DealScore.needs_more_data`` is set and
the final decision downgrades regardless of the number.
"""

from __future__ import annotations

from typing import List

from ..config import DEFAULT_CONFIG, IMPORTANT_FIELDS, EngineConfig
from ..formatting import money
from ..models.enums import (
    ARVConfidence,
    Classification,
    CompConfidence,
    Condition,
    Occupancy,
    PropertyType,
    RepairConfidence,
    SellerMotivation,
)
from ..models.property import PropertyLead
from ..models.results import (
    ARVAssessment,
    CompAnalysis,
    DealScore,
    FinancialSummary,
    RepairEstimate,
    ScoreComponent,
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _linear(value: float, at_zero: float, at_one: float) -> float:
    """Map ``value`` onto 0..1, where ``at_zero`` scores 0 and ``at_one`` scores 1."""
    if at_one == at_zero:
        return 0.5
    return _clamp((value - at_zero) / (at_one - at_zero))


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def _score_discount_from_arv(
    lead: PropertyLead, arv: ARVAssessment, config: EngineConfig
) -> ScoreComponent:
    weight = config.weight("discount_from_arv")
    if not arv.is_usable or lead.asking_price is None:
        return ScoreComponent(
            "Discount from ARV", weight, 0.0, "Cannot be measured without both ARV and asking price."
        )
    discount = (arv.arv - lead.asking_price) / arv.arv  # type: ignore[operator]
    score = _linear(discount, 0.10, 0.50)
    return ScoreComponent(
        "Discount from ARV",
        weight,
        score,
        f"Asking price is {discount * 100:.1f}% below the ARV being underwritten.",
    )


def _score_wholesale_spread(
    lead: PropertyLead, financials: FinancialSummary, config: EngineConfig
) -> ScoreComponent:
    weight = config.weight("wholesale_spread")
    if financials.mao is None or lead.asking_price is None:
        return ScoreComponent(
            "Wholesale spread", weight, 0.0, "Cannot be measured without a MAO and an asking price."
        )
    spread = financials.mao - lead.asking_price
    fee = max(config.target_wholesale_fee, 1.0)
    # Scoring 0.5 means the fee lands exactly on target: MAO == asking.
    score = _clamp(0.5 + (spread / (2 * fee)) * 0.5)
    achievable = financials.wholesale_fee_at_asking
    if spread >= 0:
        note = (
            f"At the asking price the deal supports about {money(achievable)} of "
            f"assignment fee, against a {money(config.target_wholesale_fee)} target."
        )
    else:
        note = (
            f"Asking is {money(abs(spread))} above MAO, leaving about {money(achievable)} "
            f"of fee against a {money(config.target_wholesale_fee)} target — the price "
            "has to come down."
        )
    return ScoreComponent("Wholesale economics", weight, score, note)


def _score_comp_quality(
    comps: CompAnalysis, arv: ARVAssessment, config: EngineConfig
) -> ScoreComponent:
    weight = config.weight("comp_quality")
    base = {
        CompConfidence.HIGH: 1.0,
        CompConfidence.MEDIUM: 0.70,
        CompConfidence.LOW: 0.40,
        CompConfidence.NONE: 0.10,
    }[comps.confidence]
    if comps.reliable_count:
        base = (base + comps.mean_quality) / 2
    if arv.confidence is ARVConfidence.INSUFFICIENT_DATA:
        base = 0.0
    note = (
        f"{comps.reliable_count} of {comps.count} comps met the reliability bar "
        f"({comps.confidence} comp confidence, {arv.confidence})."
    )
    return ScoreComponent("Comp quality", weight, _clamp(base), note)


def _score_repair_risk(
    arv: ARVAssessment, repairs: RepairEstimate, config: EngineConfig
) -> ScoreComponent:
    weight = config.weight("repair_risk")
    if not repairs.is_usable or not arv.is_usable:
        return ScoreComponent(
            "Repair risk", weight, 0.0, "No usable rehab figure to weigh against the ARV."
        )
    ratio = repairs.base / arv.arv  # type: ignore[operator]
    score = _linear(ratio, config.extreme_repair_ratio, 0.05)
    if repairs.confidence is RepairConfidence.CONDITION_BASED:
        score *= 0.85
    spread_note = ""
    if repairs.low is not None and repairs.high is not None and repairs.low > 0:
        band = (repairs.high - repairs.low) / repairs.low
        if band > 0.5:
            score *= 0.9
            spread_note = " Wide low-to-high band adds execution risk."
    note = (
        f"Rehab of ${repairs.base:,.0f} is {ratio * 100:.1f}% of ARV "
        f"({repairs.confidence})." + spread_note
    )
    return ScoreComponent("Repair risk", weight, _clamp(score), note)


def _score_seller_motivation(lead: PropertyLead, config: EngineConfig) -> ScoreComponent:
    weight = config.weight("seller_motivation")
    base = {
        SellerMotivation.HIGH: 1.00,
        SellerMotivation.MODERATE: 0.60,
        SellerMotivation.LOW: 0.25,
        SellerMotivation.UNKNOWN: 0.40,
    }[lead.seller_motivation]
    parts = [f"Reported motivation: {lead.seller_motivation}."]
    if lead.distress_indicators:
        base += min(0.05 * len(lead.distress_indicators), 0.20)
        parts.append(
            f"{len(lead.distress_indicators)} distress indicator(s) reported: "
            + ", ".join(lead.distress_indicators)
            + "."
        )
    if lead.days_on_market is not None and lead.days_on_market >= 90:
        base += 0.10
        parts.append(f"{lead.days_on_market} days on market builds negotiating leverage.")
    elif lead.days_on_market is not None and lead.days_on_market <= 7:
        base -= 0.05
        parts.append(f"Only {lead.days_on_market} days on market — the seller has not sweated yet.")
    return ScoreComponent("Seller motivation", weight, _clamp(base), " ".join(parts))


def _score_condition(lead: PropertyLead, config: EngineConfig) -> ScoreComponent:
    weight = config.weight("condition")
    base = {
        Condition.COSMETIC: 1.00,
        Condition.TURNKEY: 0.85,
        Condition.MODERATE: 0.75,
        Condition.HEAVY: 0.45,
        Condition.TEARDOWN: 0.20,
        Condition.UNKNOWN: 0.35,
    }[lead.condition]
    if lead.condition is Condition.UNKNOWN:
        note = "Condition was not reported, which limits every rehab conclusion."
    else:
        note = f"Reported condition: {lead.condition}."
    return ScoreComponent("Property condition", weight, base, note)


def _score_marketability(lead: PropertyLead, config: EngineConfig) -> ScoreComponent:
    weight = config.weight("marketability")
    base = {
        PropertyType.SINGLE_FAMILY: 1.00,
        PropertyType.TOWNHOUSE: 0.85,
        PropertyType.DUPLEX: 0.80,
        PropertyType.TRIPLEX: 0.72,
        PropertyType.FOURPLEX: 0.70,
        PropertyType.MULTI_FAMILY: 0.70,
        PropertyType.CONDO: 0.60,
        PropertyType.MOBILE: 0.25,
        PropertyType.LAND: 0.30,
        PropertyType.COMMERCIAL: 0.35,
        PropertyType.UNKNOWN: 0.50,
    }[lead.property_type]
    parts = [f"Type: {lead.property_type}."]
    if lead.beds is not None:
        if lead.beds >= 3:
            base += 0.05
        elif lead.beds <= 1:
            base -= 0.15
            parts.append("One-bedroom stock has a thinner cash-buyer pool.")
    if lead.occupancy is Occupancy.VACANT:
        base += 0.05
        parts.append("Vacant, so access and closing timelines are simpler.")
    elif lead.occupancy is Occupancy.TENANT_OCCUPIED:
        base -= 0.10
        parts.append("Tenant-occupied: access, lease terms and possession all become issues.")
    if lead.days_on_market is not None and lead.days_on_market >= 180:
        base -= 0.10
        parts.append(f"{lead.days_on_market} days unsold suggests the market is rejecting it.")
    return ScoreComponent("Marketability", weight, _clamp(base), " ".join(parts))


def _score_equity_potential(
    arv: ARVAssessment,
    repairs: RepairEstimate,
    financials: FinancialSummary,
    config: EngineConfig,
) -> ScoreComponent:
    weight = config.weight("equity_potential")
    if not arv.is_usable or not repairs.is_usable or financials.recommended_offer is None:
        return ScoreComponent(
            "Equity potential", weight, 0.0, "Needs an ARV, a rehab figure and an offer."
        )
    equity = arv.arv - financials.recommended_offer - repairs.base  # type: ignore[operator]
    ratio = equity / arv.arv  # type: ignore[operator]
    score = _linear(ratio, 0.10, 0.35)
    return ScoreComponent(
        "Equity potential",
        weight,
        score,
        (
            f"At the recommended offer, {money(equity)} of ARV remains after rehab "
            f"({ratio * 100:.1f}% of ARV) to cover the end buyer's profit, holding "
            "costs and closing costs."
        ),
    )


def _score_data_confidence(
    lead: PropertyLead, arv: ARVAssessment, repairs: RepairEstimate, config: EngineConfig
) -> ScoreComponent:
    weight = config.weight("data_confidence")
    present = 0
    for field_name in IMPORTANT_FIELDS:
        value = getattr(lead, field_name, None)
        if field_name == "comps":
            present += 1 if lead.comps else 0
        elif hasattr(value, "value") and getattr(value, "value", None) == "unknown":
            continue
        elif value not in (None, "", []):
            present += 1
    completeness = present / len(IMPORTANT_FIELDS)
    arv_factor = {
        ARVConfidence.VERIFIED_SUPPORTED: 1.0,
        ARVConfidence.ESTIMATED: 0.75,
        ARVConfidence.USER_PROVIDED: 0.45,
        ARVConfidence.INSUFFICIENT_DATA: 0.0,
    }[arv.confidence]
    repair_factor = {
        RepairConfidence.USER_PROVIDED: 0.9,
        RepairConfidence.CONDITION_BASED: 0.7,
        RepairConfidence.INSUFFICIENT_DATA: 0.0,
    }[repairs.confidence]
    score = completeness * 0.4 + arv_factor * 0.35 + repair_factor * 0.25
    return ScoreComponent(
        "Data confidence",
        weight,
        _clamp(score),
        (
            f"{present}/{len(IMPORTANT_FIELDS)} supporting fields supplied; "
            f"ARV basis {arv.confidence}; rehab basis {repairs.confidence}."
        ),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def classify(score: float, config: EngineConfig = DEFAULT_CONFIG) -> Classification:
    """Map a 0-100 score onto its classification band."""
    bands = config.classification_bands
    if score >= bands["HOT"]:
        return Classification.HOT
    if score >= bands["STRONG"]:
        return Classification.STRONG
    if score >= bands["POSSIBLE"]:
        return Classification.POSSIBLE
    if score >= bands["WEAK"]:
        return Classification.WEAK
    return Classification.PASS


def apply_score_cap(score: DealScore, cap: float, config: EngineConfig = DEFAULT_CONFIG) -> DealScore:
    """Force a score down to ``cap`` and reclassify.

    Used for structural vetoes — a deal whose arithmetic leaves no viable
    purchase price must not read as POSSIBLE because the asking price happens
    to look like a steep discount off a shaky ARV.
    """
    if score.total <= cap:
        return score
    score.total = round(cap, 1)
    score.classification = classify(score.total, config)
    return score


def score_deal(
    lead: PropertyLead,
    comps: CompAnalysis,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    financials: FinancialSummary,
    needs_more_data: bool = False,
    config: EngineConfig = DEFAULT_CONFIG,
) -> DealScore:
    """Compute the weighted 0-100 deal score and its classification."""
    components: List[ScoreComponent] = [
        _score_discount_from_arv(lead, arv, config),
        _score_wholesale_spread(lead, financials, config),
        _score_comp_quality(comps, arv, config),
        _score_repair_risk(arv, repairs, config),
        _score_seller_motivation(lead, config),
        _score_condition(lead, config),
        _score_marketability(lead, config),
        _score_equity_potential(arv, repairs, financials, config),
        _score_data_confidence(lead, arv, repairs, config),
    ]
    total_weight = sum(c.weight for c in components)
    raw = sum(c.points for c in components)
    total = (raw / total_weight * 100.0) if total_weight else 0.0
    total = round(_clamp(total, 0.0, 100.0), 1)
    return DealScore(
        total=total,
        classification=classify(total, config),
        components=components,
        needs_more_data=needs_more_data,
    )
