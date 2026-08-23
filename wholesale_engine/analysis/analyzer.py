"""The orchestrator: one lead in, one :class:`AnalysisResult` out.

Order of operations:

1. Grade the comps.
2. Reconcile the ARV against them.
3. Build the rehab band.
4. Run the deal math (MAO, offer, assignment price, spread, scenarios).
5. Score the deal.
6. Collect risk flags, missing data, and a final decision.

Steps 1-5 live in sibling modules; this file only sequences them and applies
the judgement that spans more than one of them.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from ..config import DEFAULT_CONFIG, EngineConfig
from ..formatting import money
from ..models.enums import (
    ARVConfidence,
    CompConfidence,
    Condition,
    Decision,
    Occupancy,
    PropertyType,
    RepairConfidence,
    SellerMotivation,
    Severity,
    WholesaleFeeStatus,
)
from ..models.property import PropertyLead
from ..models.results import (
    AnalysisResult,
    ARVAssessment,
    CompAnalysis,
    DealScore,
    FinancialSummary,
    RepairEstimate,
    RiskFlag,
)
from . import financials as fin
from .comps import analyze_comps
from .repairs import estimate_repairs
from .scoring import apply_score_cap, score_deal
from .valuation import assess_arv


# ---------------------------------------------------------------------------
# Offer construction
# ---------------------------------------------------------------------------


def _risk_discount_inputs(
    lead: PropertyLead,
    comps: CompAnalysis,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    config: EngineConfig,
) -> List[Tuple[str, float]]:
    """Labelled reasons to offer below MAO, each worth a slice of discount."""
    points: List[Tuple[str, float]] = []

    if arv.confidence is ARVConfidence.USER_PROVIDED:
        points.append(("ARV is unverified by comps", 0.10))
    elif arv.confidence is ARVConfidence.ESTIMATED:
        points.append(("ARV is an estimate rather than a value conclusion", 0.05))

    if comps.confidence is CompConfidence.NONE:
        points.append(("no reliable comparable sales", 0.06))
    elif comps.confidence is CompConfidence.LOW:
        points.append(("thin comp support", 0.04))

    if arv.deviation_pct is not None and abs(arv.deviation_pct) > config.arv_conflict_tolerance:
        points.append(("your ARV and the comps disagree", 0.04))

    if repairs.confidence is RepairConfidence.CONDITION_BASED:
        points.append(("rehab budget inferred from condition, not inspected", 0.05))
    elif repairs.confidence is RepairConfidence.USER_PROVIDED:
        points.append(("rehab budget not yet confirmed by a contractor", 0.02))

    if lead.condition in (Condition.HEAVY, Condition.TEARDOWN):
        points.append((f"{lead.condition} condition hides expensive surprises", 0.04))
    elif lead.condition is Condition.UNKNOWN:
        points.append(("condition unreported", 0.05))

    if lead.occupancy in (Occupancy.TENANT_OCCUPIED, Occupancy.UNKNOWN):
        points.append(("occupancy complicates access and possession", 0.03))

    if lead.year_built is not None and lead.year_built < config.lead_paint_year:
        points.append(("pre-1978 construction (abatement risk)", 0.02))

    if lead.seller_motivation in (SellerMotivation.HIGH, SellerMotivation.MODERATE):
        points.append(("motivated seller — room to open below MAO", 0.03))
    elif lead.seller_motivation is SellerMotivation.UNKNOWN:
        points.append(("seller motivation unknown", 0.02))

    if lead.property_type in (PropertyType.MOBILE, PropertyType.LAND, PropertyType.CONDO):
        points.append((f"{lead.property_type} has a narrower cash-buyer pool", 0.03))

    return points


def _build_financials(
    lead: PropertyLead,
    comps: CompAnalysis,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    config: EngineConfig,
) -> FinancialSummary:
    summary = FinancialSummary(target_wholesale_fee=config.target_wholesale_fee)
    if not arv.is_usable:
        return summary

    summary.arv = arv.arv
    summary.seventy_percent_arv = fin.seventy_percent_arv(arv.arv, config)  # type: ignore[arg-type]
    if lead.asking_price is not None:
        summary.discount_from_arv_pct = fin.discount_from_arv(lead.asking_price, arv.arv)  # type: ignore[arg-type]

    if not repairs.is_usable:
        return summary

    summary.repairs_used = repairs.base
    summary.end_buyer_max_price = fin.end_buyer_max_price(
        arv.arv, repairs.base, config  # type: ignore[arg-type]
    )
    summary.mao = fin.maximum_allowable_offer(arv.arv, repairs.base, config)  # type: ignore[arg-type]

    discount, reasons = fin.offer_risk_discount(
        _risk_discount_inputs(lead, comps, arv, repairs, config), config
    )
    summary.offer_discount_pct = discount
    summary.offer_discount_reasons = reasons
    if summary.mao > 0:
        # A non-positive MAO means no purchase price works, so there is no offer
        # to recommend — leave it unset rather than printing a meaningless $0.
        summary.recommended_offer = fin.recommended_offer(
            summary.mao, discount, lead.asking_price, config
        )
        summary.assignment_price = fin.assignment_price(summary.recommended_offer, config)
        summary.potential_gross_spread = fin.gross_spread(
            summary.mao, summary.recommended_offer
        )
    if lead.asking_price is not None:
        summary.spread_vs_asking = summary.mao - lead.asking_price
        summary.wholesale_fee_at_asking = fin.potential_wholesale_fee(
            arv.arv, repairs.base, lead.asking_price, config  # type: ignore[arg-type]
        )

    # The fee the deal actually supports — distinct from the MAO cushion.
    if summary.recommended_offer is not None:
        summary.potential_wholesale_fee = fin.potential_wholesale_fee(
            arv.arv, repairs.base, summary.recommended_offer, config  # type: ignore[arg-type]
        )
        summary.buyer_margin = fin.buyer_margin(
            arv.arv, repairs.base, summary.assignment_price, config  # type: ignore[arg-type]
        )

    # Judge the fee at the price actually on the table: an offer the seller has
    # not accepted cannot be what qualifies a deal.
    binding_price = fin.binding_purchase_price(summary.recommended_offer, lead.asking_price)
    if binding_price is not None:
        summary.binding_wholesale_fee = fin.potential_wholesale_fee(
            arv.arv, repairs.base, binding_price, config  # type: ignore[arg-type]
        )
    summary.wholesale_fee_status = fin.classify_wholesale_fee(
        summary.binding_wholesale_fee, config
    )

    summary.scenarios = fin.build_mao_scenarios(
        arv.arv,  # type: ignore[arg-type]
        repairs.low,
        repairs.mid,
        repairs.high,
        lead.asking_price,
        config,
    )
    return summary


# ---------------------------------------------------------------------------
# Risk flags and gaps
# ---------------------------------------------------------------------------


def _deal_risk_flags(
    lead: PropertyLead,
    comps: CompAnalysis,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    summary: FinancialSummary,
    config: EngineConfig,
) -> List[RiskFlag]:
    flags: List[RiskFlag] = []

    if lead.asking_price is None:
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "no_asking_price",
                "No asking price supplied — there is nothing to measure the offer against.",
            )
        )

    if arv.is_usable and lead.asking_price is not None:
        ratio = lead.asking_price / arv.arv  # type: ignore[operator]
        if ratio >= 1.0:
            flags.append(
                RiskFlag(
                    Severity.CRITICAL,
                    "asking_above_arv",
                    (
                        f"OVERPRICED: asking ${lead.asking_price:,.0f} is at or above the "
                        f"ARV of ${arv.arv:,.0f}. There is no margin here at any rehab level."
                    ),
                )
            )
        elif ratio >= config.overpriced_ratio:
            flags.append(
                RiskFlag(
                    Severity.HIGH,
                    "overpriced",
                    (
                        f"OVERPRICED: asking ${lead.asking_price:,.0f} is "
                        f"{ratio * 100:.0f}% of ARV, before any repairs."
                    ),
                )
            )

    if summary.mao is not None and summary.mao <= 0:
        flags.append(
            RiskFlag(
                Severity.CRITICAL,
                "negative_mao",
                (
                    f"MAO is {money(summary.mao)}. At this ARV and rehab budget there is no "
                    "purchase price that leaves room for the fee and the end buyer."
                ),
            )
        )

    if summary.spread_vs_asking is not None and summary.mao is not None and summary.mao > 0:
        if summary.spread_vs_asking < 0:
            gap = abs(summary.spread_vs_asking)
            gap_ratio = gap / summary.mao
            severity = Severity.HIGH if gap_ratio > config.max_negotiable_gap else Severity.MEDIUM
            flags.append(
                RiskFlag(
                    severity,
                    "price_gap",
                    (
                        f"Asking is ${gap:,.0f} ({gap_ratio * 100:.0f}%) above MAO. The seller "
                        "must come down by that much before this is a deal."
                    ),
                )
            )

    if summary.wholesale_fee_status is WholesaleFeeStatus.BELOW_TARGET:
        fee = summary.binding_wholesale_fee
        at_asking = (
            summary.wholesale_fee_at_asking is not None
            and lead.asking_price is not None
            and summary.binding_wholesale_fee == summary.wholesale_fee_at_asking
        )
        where = (
            f"at the asking price of {money(lead.asking_price)}"
            if at_asking
            else f"at the recommended offer of {money(summary.recommended_offer)}"
        )
        shortfall = config.target_wholesale_fee - (fee or 0.0)
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "below_target_wholesale_fee",
                (
                    f"BELOW TARGET WHOLESALE FEE: this deal supports about {money(fee)} "
                    f"{where}, against your target of "
                    f"{money(config.target_wholesale_fee)} — a shortfall of "
                    f"{money(shortfall)}. That is a label, not a rejection: the deal "
                    f"score above still decides. Coming down {money(shortfall)} on price "
                    "would put the fee back on target."
                ),
            )
        )
    elif summary.wholesale_fee_status is WholesaleFeeStatus.UNKNOWN and summary.mao is not None:
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "wholesale_fee_unknown",
                (
                    "Wholesale fee status is UNKNOWN — without an asking price or a "
                    "usable offer there is no purchase price to measure the fee against."
                ),
            )
        )

    if (
        summary.buyer_margin is not None
        and summary.buyer_margin < 0
        and summary.assignment_price is not None
    ):
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "no_buyer_margin",
                (
                    f"At an assignment price of {money(summary.assignment_price)} there is no "
                    "room left for the end buyer under the same 70% rule. Your fee only "
                    "exists if someone will actually buy the contract."
                ),
            )
        )

    if (
        summary.potential_gross_spread is not None
        and summary.recommended_offer is not None
        and summary.potential_gross_spread < 0
    ):
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "no_spread",
                "The recommended offer leaves no gross spread above MAO.",
            )
        )

    if arv.is_usable and repairs.is_usable:
        ratio = repairs.base / arv.arv  # type: ignore[operator]
        if ratio >= config.extreme_repair_ratio:
            flags.append(
                RiskFlag(
                    Severity.HIGH,
                    "extreme_rehab",
                    (
                        f"Rehab is {ratio * 100:.0f}% of ARV — that is a heavy-construction "
                        "project, and the cash-buyer pool for it is small."
                    ),
                )
            )
        elif ratio >= config.heavy_repair_ratio:
            flags.append(
                RiskFlag(
                    Severity.MEDIUM,
                    "heavy_rehab",
                    f"Rehab is {ratio * 100:.0f}% of ARV — capital-intensive for an end buyer.",
                )
            )

    # High-rehab scenario stress test.
    if repairs.high is not None and arv.is_usable and lead.asking_price is not None:
        high_mao = fin.maximum_allowable_offer(arv.arv, repairs.high, config)  # type: ignore[arg-type]
        if high_mao < lead.asking_price and (
            summary.mao is not None and summary.mao >= lead.asking_price
        ):
            flags.append(
                RiskFlag(
                    Severity.HIGH,
                    "fragile_to_rehab_overrun",
                    (
                        f"This deal only works at the low end of the rehab range. At the high "
                        f"rehab scenario ({money(repairs.high)}) the MAO drops to "
                        f"{money(high_mao)}, below the asking price."
                    ),
                )
            )

    # ARV per square foot sanity check against the comp set.
    if (
        arv.is_usable
        and lead.sqft
        and comps.price_per_sqft_low is not None
        and comps.price_per_sqft_high is not None
    ):
        subject_psf = arv.arv / lead.sqft  # type: ignore[operator]
        if subject_psf > comps.price_per_sqft_high * 1.15:
            flags.append(
                RiskFlag(
                    Severity.HIGH,
                    "arv_psf_outlier",
                    (
                        f"The ARV works out to ${subject_psf:,.0f}/sqft, above the highest "
                        f"reliable comp at ${comps.price_per_sqft_high:,.0f}/sqft. The deal "
                        "may only pencil on an unrealistic ARV."
                    ),
                )
            )

    if lead.estimated_monthly_rent and arv.is_usable:
        rent_ratio = fin.rent_to_value_ratio(lead.estimated_monthly_rent, arv.arv)  # type: ignore[arg-type]
        if rent_ratio is not None and rent_ratio < 0.005:
            flags.append(
                RiskFlag(
                    Severity.LOW,
                    "weak_rent_ratio",
                    (
                        f"Estimated rent is {rent_ratio * 100:.2f}% of ARV monthly — weak for "
                        "a landlord buyer, so the exit leans on flippers."
                    ),
                )
            )

    if lead.occupancy is Occupancy.TENANT_OCCUPIED:
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "tenant_occupied",
                (
                    "Tenant-occupied: confirm the lease, deposits, and whether possession is "
                    "deliverable at closing before you contract."
                ),
            )
        )
    elif lead.occupancy is Occupancy.UNKNOWN:
        flags.append(
            RiskFlag(Severity.MEDIUM, "occupancy_unknown", "Occupancy status was not reported.")
        )

    if lead.property_type is PropertyType.MOBILE:
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "mobile_home",
                (
                    "Mobile/manufactured housing: financing, titling and land ownership all "
                    "narrow the buyer pool sharply."
                ),
            )
        )
    elif lead.property_type is PropertyType.LAND:
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "land",
                "Land does not fit the ARV/rehab model — value it on comparable lot sales.",
            )
        )
    elif lead.property_type is PropertyType.CONDO:
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "condo",
                "Condo: HOA dues, special assessments and rental caps can all kill the resale.",
            )
        )

    if lead.days_on_market is not None and lead.days_on_market >= 180:
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "stale_listing",
                (
                    f"{lead.days_on_market} days on market. Long exposure at the current price "
                    "usually means the market disagrees with the price, the condition, or both."
                ),
            )
        )

    # Internal consistency checks.
    if (
        lead.seller_motivation is SellerMotivation.HIGH
        and lead.asking_price is not None
        and arv.is_usable
        and lead.asking_price / arv.arv >= config.overpriced_ratio  # type: ignore[operator]
    ):
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "motivation_conflict",
                (
                    "CONFLICT: the seller is described as highly motivated but is asking near "
                    "full retail. Motivation claims and pricing behaviour do not match."
                ),
            )
        )

    if lead.sqft and lead.beds and lead.sqft / lead.beds < 200:
        flags.append(
            RiskFlag(
                Severity.LOW,
                "sqft_bed_mismatch",
                (
                    f"{lead.sqft:,} sqft across {lead.beds:g} bedrooms is unusually tight — "
                    "verify the square footage and bed count against public record."
                ),
            )
        )

    if lead.year_built is not None and lead.year_built < config.lead_paint_year:
        flags.append(
            RiskFlag(
                Severity.LOW,
                "pre_1978",
                (
                    f"Built {lead.year_built}: lead-paint disclosure applies and abatement can "
                    "add cost on a pre-1978 property."
                ),
            )
        )

    for indicator in lead.distress_indicators:
        flags.append(
            RiskFlag(
                Severity.MEDIUM,
                "distress_indicator",
                (
                    f"Reported distress indicator: {indicator}. This is your input — the engine "
                    "has not verified it against any public record and cannot."
                ),
            )
        )

    return flags


def _missing_data(
    lead: PropertyLead,
    comps: CompAnalysis,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    config: EngineConfig,
) -> Tuple[List[str], bool]:
    """Return the list of gaps, plus whether a critical gate failed."""
    gaps: List[str] = []
    critical = False

    if lead.asking_price is None:
        gaps.append("Asking price (or the seller's number) — required to evaluate any offer.")
        critical = True

    if arv.confidence is ARVConfidence.INSUFFICIENT_DATA:
        gaps.append(
            "An ARV, or at least 3 closed comparable sales within ~1 mile from the last "
            "6 months — required before any offer can be calculated."
        )
        critical = True
    elif arv.confidence is ARVConfidence.USER_PROVIDED:
        gaps.append(
            "Closed comparable sales to verify the ARV you supplied (target: 3 closed sales, "
            "same property type, similar beds/baths/sqft, within ~1 mile, last 6 months)."
        )
        critical = True
    elif comps.reliable_count < config.strong_comp_count:
        gaps.append(
            f"Additional closed comps — {comps.reliable_count} reliable comp(s) on file, "
            f"{config.strong_comp_count} is the target for a supported ARV."
        )

    if repairs.confidence is RepairConfidence.INSUFFICIENT_DATA:
        gaps.append(
            "A repair estimate or, at minimum, the property condition — required to compute MAO."
        )
        critical = True
    elif repairs.confidence is RepairConfidence.CONDITION_BASED:
        gaps.append(
            "A walkthrough or contractor bid — the rehab range here is inferred from the "
            "reported condition alone."
        )

    if not lead.sqft:
        gaps.append("Square footage — needed for price-per-sqft comp analysis and rehab scaling.")
    if lead.beds is None:
        gaps.append("Bed count.")
    if lead.baths is None:
        gaps.append("Bath count.")
    if lead.year_built is None:
        gaps.append("Year built — drives systems, abatement and buyer-pool risk.")
    if lead.condition is Condition.UNKNOWN:
        gaps.append("Seller-reported condition, room by room if possible.")
    if lead.occupancy is Occupancy.UNKNOWN:
        gaps.append("Occupancy status (vacant / owner / tenant) and whether possession is clean.")
    if lead.property_type is PropertyType.UNKNOWN:
        gaps.append("Property type.")
    if not lead.county:
        gaps.append("County — needed later for public-record and tax research.")
    if lead.seller_motivation is SellerMotivation.UNKNOWN:
        gaps.append("Seller's motivation, timeline, and why they are selling.")
    if lead.days_on_market is None:
        gaps.append("Days on market / how long they have been trying to sell.")
    if lead.estimated_monthly_rent is None:
        gaps.append("Estimated market rent — needed to price the landlord exit.")
    if not lead.lot_size_sqft:
        gaps.append("Lot size.")

    gaps.append(
        "Title, lien, mortgage payoff and foreclosure status — this engine has no access to "
        "public records and will never assert any of it. Pull it before you contract."
    )
    return gaps, critical


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _decide(
    lead: PropertyLead,
    arv: ARVAssessment,
    repairs: RepairEstimate,
    summary: FinancialSummary,
    score: DealScore,
    flags: List[RiskFlag],
    config: EngineConfig,
) -> Tuple[Decision, str]:
    has_critical = any(f.severity is Severity.CRITICAL for f in flags)

    if summary.mao is None or not arv.is_usable or not repairs.is_usable:
        return (
            Decision.NEED_MORE_DATA,
            (
                "The deal math could not be completed. Without a defensible ARV and a rehab "
                "figure there is no MAO, and without a MAO there is no offer to make. "
                "Fill the gaps listed under MISSING DATA and re-run this lead."
            ),
        )

    asking = lead.asking_price
    spread_ok = (
        summary.potential_gross_spread is not None
        and summary.mao is not None
        and summary.mao > 0
    )

    if has_critical:
        critical_msgs = "; ".join(
            f.message for f in flags if f.severity is Severity.CRITICAL
        )
        if summary.mao <= 0 or (asking is not None and arv.arv and asking >= arv.arv):  # type: ignore[operator]
            return (
                Decision.PASS,
                (
                    "Pass. "
                    + critical_msgs
                    + " No amount of negotiating fixes a deal where the arithmetic does not "
                    "leave room for repairs, your fee, and the end buyer's profit."
                ),
            )
        return (
            Decision.NEED_MORE_DATA,
            "Critical issues block a decision: " + critical_msgs,
        )

    if score.needs_more_data:
        return (
            Decision.NEED_MORE_DATA,
            (
                f"The numbers score {score.total:.0f}/100 ({score.classification}), but that "
                "score rests on unverified inputs. "
                + (
                    "The ARV has not been corroborated by comparable sales, which is the "
                    "assumption most likely to be wrong and most expensive when it is. "
                    if arv.confidence
                    in (ARVConfidence.USER_PROVIDED, ARVConfidence.INSUFFICIENT_DATA)
                    else ""
                )
                + "Close the gaps under MISSING DATA before you make an offer — a good score on "
                "bad data is still bad data."
            ),
        )

    if asking is None:
        return (
            Decision.NEED_MORE_DATA,
            "No asking price on file, so there is nothing to negotiate against yet.",
        )

    if summary.mao <= 0:
        return (
            Decision.PASS,
            (
                f"Pass. MAO is {money(summary.mao)} — the ARV of {money(arv.arv)} cannot carry "
                f"${repairs.base:,.0f} of repairs plus a ${config.wholesale_fee:,.0f} fee."
            ),
        )

    gap = summary.mao - asking

    meets_fee_target = summary.wholesale_fee_status is WholesaleFeeStatus.MEETS_TARGET

    # The fee target is a TARGET. It never gates the decision on its own — it
    # is reported, flagged when short, and scored continuously by the
    # ``wholesale_spread`` component. The deal score is the decision mechanism.
    #
    # The only fee-based gate is the viability floor, which sits far below the
    # target: a deal supporting $13,000 can still be a GO, one supporting
    # $2,800 cannot be a GO at any score.
    fee_ok = (
        summary.binding_wholesale_fee is not None
        and summary.binding_wholesale_fee >= config.min_viable_wholesale_fee
    )
    price_position = (
        f"At {money(asking)} the seller is already asking {money(gap)} below the MAO "
        f"of {money(summary.mao)}"
        if gap >= 0
        else (
            f"At {money(asking)} the seller is asking {money(abs(gap))} above the MAO of "
            f"{money(summary.mao)}, which trims the fee rather than killing the deal"
        )
    )

    if score.total >= config.go_score_threshold and spread_ok and fee_ok:
        return (
            Decision.GO,
            (
                f"Go. {price_position}, the ARV basis is {arv.confidence}, and the deal scores "
                f"{score.total:.0f}/100 ({score.classification}). "
                + (
                    "The economics clear your target: even"
                    if meets_fee_target
                    else "The fee comes in under your target, which the score already "
                    "accounts for: even"
                )
                + f" if the seller will not move off {money(asking)}, the deal "
                f"supports about {money(summary.wholesale_fee_at_asking)} of assignment fee "
                f"against a {money(config.target_wholesale_fee)} target. Open at "
                f"{money(summary.recommended_offer)} — below MAO, to protect against the rehab "
                f"and value risks listed above — where the fee would be about "
                f"{money(summary.potential_wholesale_fee)}, and assign around "
                f"{money(summary.assignment_price)}. Nothing here is guaranteed: verify the "
                "rehab with a walkthrough and confirm title before you tie it up."
            ),
        )

    if score.total < config.negotiate_score_threshold:
        return (
            Decision.PASS,
            (
                f"Pass. The deal scores {score.total:.0f}/100 ({score.classification}). "
                f"Even at the recommended ${summary.recommended_offer:,.0f} the combination of "
                "price, condition, comp support and buyer-pool risk does not justify the work."
            ),
        )

    if gap < 0:
        gap_ratio = abs(gap) / summary.mao
        if gap_ratio > config.max_negotiable_gap:
            return (
                Decision.PASS,
                (
                    f"Pass. Asking ${asking:,.0f} is ${abs(gap):,.0f} "
                    f"({gap_ratio * 100:.0f}%) above the MAO of ${summary.mao:,.0f}. A gap that "
                    "wide almost never closes, and chasing it costs you the leads that would."
                ),
            )
        return (
            Decision.NEGOTIATE,
            (
                f"Negotiate. The deal scores {score.total:.0f}/100 ({score.classification}), but "
                f"asking {money(asking)} sits {money(abs(gap))} above the MAO of "
                f"{money(summary.mao)}. At the asking price the assignment fee would be about "
                f"{money(summary.wholesale_fee_at_asking)} against your "
                f"{money(config.target_wholesale_fee)} target, so the price has to move "
                f"before this is a deal. Your opening number is "
                f"{money(summary.recommended_offer)} and {money(summary.mao)} is your walk-away "
                "ceiling — show the seller the repair math rather than arguing about price."
            ),
        )

    fee_note = (
        f"At the binding price the fee comes to about {money(summary.binding_wholesale_fee)} "
        f"against your {money(config.target_wholesale_fee)} target"
        + ("." if meets_fee_target else " — short of target, so price is the lever.")
    )
    return (
        Decision.NEGOTIATE,
        (
            f"Negotiate. The math works — MAO of {money(summary.mao)} against asking "
            f"{money(asking)} — but at {score.total:.0f}/100 ({score.classification}) the "
            "supporting picture is not strong enough to move without pushing on price. "
            + fee_note
            + f" Open at {money(summary.recommended_offer)}, hold {money(summary.mao)} as your "
            "ceiling, and work the risk flags above before you commit."
        ),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_property(
    lead: PropertyLead,
    config: EngineConfig = DEFAULT_CONFIG,
    as_of: Optional[date] = None,
) -> AnalysisResult:
    """Run the full underwriting pipeline for one lead."""
    comps = analyze_comps(lead, config, as_of)
    arv, arv_flags = assess_arv(lead, comps, config)
    repairs, repair_flags = estimate_repairs(lead, config)
    summary = _build_financials(lead, comps, arv, repairs, config)

    gaps, critical_gap = _missing_data(lead, comps, arv, repairs, config)
    flags = list(arv_flags) + list(repair_flags)
    flags += _deal_risk_flags(lead, comps, arv, repairs, summary, config)

    score = score_deal(lead, comps, arv, repairs, summary, critical_gap, config)
    if summary.mao is not None and summary.mao <= 0:
        # No purchase price supports this deal — it cannot rank above PASS.
        score = apply_score_cap(score, config.classification_bands["WEAK"] - 1.0, config)
    decision, explanation = _decide(lead, arv, repairs, summary, score, flags, config)

    return AnalysisResult(
        lead=lead,
        comps=comps,
        arv=arv,
        repairs=repairs,
        financials=summary,
        score=score,
        risk_flags=flags,
        missing_data=gaps,
        decision=decision,
        decision_explanation=explanation,
    )


def analyze_properties(
    leads: List[PropertyLead],
    config: EngineConfig = DEFAULT_CONFIG,
    as_of: Optional[date] = None,
) -> List[AnalysisResult]:
    """Analyze a batch of leads, preserving input order."""
    return [analyze_property(lead, config, as_of) for lead in leads]
