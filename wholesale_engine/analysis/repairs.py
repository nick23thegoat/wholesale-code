"""Preliminary rehab budgeting.

Two paths:

* The user supplied a repair number — use it, label it USER-PROVIDED, and
  build a low/mid/high band around it because self-reported repair figures
  are almost always the optimistic end of the range.
* Repairs are unknown — derive a band from the reported condition alone,
  scaled by square footage and the age of the house.

Neither path produces a contractor quote, and the report says so every time.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..config import (
    DEFAULT_CONFIG,
    REPAIR_COST_PER_SQFT,
    REPAIR_FLAT_FALLBACK,
    EngineConfig,
)
from ..models.enums import Condition, Occupancy, RepairConfidence, Severity
from ..models.property import PropertyLead
from ..models.results import RepairEstimate, RiskFlag

_NOT_A_QUOTE = "This is a planning range, not a contractor quote."


def _age_multiplier(lead: PropertyLead, config: EngineConfig) -> Tuple[float, Optional[str]]:
    if lead.year_built is None:
        return 1.0, None
    if lead.year_built < config.old_systems_year:
        return (
            config.pre_1950_multiplier,
            f"built {lead.year_built}: pre-{config.old_systems_year} systems "
            "(wiring, plumbing, foundation) commonly need replacement",
        )
    if lead.year_built < config.lead_paint_year:
        return (
            config.pre_lead_paint_multiplier,
            f"built {lead.year_built}: pre-{config.lead_paint_year} stock carries "
            "lead paint and asbestos abatement risk",
        )
    return 1.0, None


def condition_based_band(
    lead: PropertyLead, config: EngineConfig = DEFAULT_CONFIG
) -> Optional[Tuple[float, float, float, Optional[float], List[str]]]:
    """(low, mid, high, psf_used, notes) derived from condition alone.

    Returns ``None`` when the condition is unknown — with no condition and no
    user figure there is nothing honest to estimate from.
    """
    if lead.condition is Condition.UNKNOWN:
        return None

    key = lead.condition.value
    notes: List[str] = []
    psf_used: Optional[float] = None

    if lead.sqft and key in REPAIR_COST_PER_SQFT:
        low_psf, mid_psf, high_psf = REPAIR_COST_PER_SQFT[key]
        low, mid, high = (low_psf * lead.sqft, mid_psf * lead.sqft, high_psf * lead.sqft)
        psf_used = mid_psf
        notes.append(
            f"{lead.condition} condition at ${low_psf:,.0f}-${high_psf:,.0f}/sqft "
            f"across {lead.sqft:,} sqft"
        )
        # Never let a tiny house produce an implausibly small rehab budget.
        floor_low, floor_mid, floor_high = REPAIR_FLAT_FALLBACK[key]
        low, mid, high = (
            max(low, floor_low * 0.5),
            max(mid, floor_mid * 0.5),
            max(high, floor_high * 0.5),
        )
    else:
        low, mid, high = REPAIR_FLAT_FALLBACK[key]
        notes.append(
            f"{lead.condition} condition with square footage unknown — flat range applied"
        )

    multiplier, age_note = _age_multiplier(lead, config)
    if multiplier != 1.0:
        low, mid, high = low * multiplier, mid * multiplier, high * multiplier
        if age_note:
            notes.append(age_note)

    # Contingency on the high end only: the high scenario is the "what if the
    # walls come open and it is worse than reported" case.
    high *= 1.0 + config.rehab_contingency
    notes.append(f"{config.rehab_contingency * 100:.0f}% contingency added to the high scenario")

    if lead.occupancy in (Occupancy.TENANT_OCCUPIED, Occupancy.OWNER_OCCUPIED):
        notes.append(
            "Property is occupied — interior condition is reported, not verified; "
            "budget could move once you walk it"
        )

    return (round(low, -2), round(mid, -2), round(high, -2), psf_used, notes)


def estimate_repairs(
    lead: PropertyLead, config: EngineConfig = DEFAULT_CONFIG
) -> Tuple[RepairEstimate, List[RiskFlag]]:
    """Produce the low / mid / high rehab band and any repair-related flags."""
    flags: List[RiskFlag] = []
    derived = condition_based_band(lead, config)
    user_value = (
        lead.user_repair_estimate
        if lead.user_repair_estimate is not None and lead.user_repair_estimate >= 0
        else None
    )

    if user_value is not None:
        low = user_value
        mid = round(user_value * config.user_repair_mid_multiplier, -2)
        high = round(user_value * config.user_repair_high_multiplier, -2)
        notes = [
            f"Your figure of ${user_value:,.0f} is used as the low end of the band; "
            f"mid and high add {(config.user_repair_mid_multiplier - 1) * 100:.0f}% and "
            f"{(config.user_repair_high_multiplier - 1) * 100:.0f}% for overruns"
        ]

        if derived:
            d_low, d_mid, d_high, _, d_notes = derived
            notes.append(
                f"Condition-based cross-check for {lead.condition} condition: "
                f"${d_low:,.0f} / ${d_mid:,.0f} / ${d_high:,.0f}"
            )
            if user_value < d_low * config.user_repair_suspicious_ratio:
                flags.append(
                    RiskFlag(
                        Severity.HIGH,
                        "repairs_understated",
                        (
                            f"Repair estimate of ${user_value:,.0f} is well below the "
                            f"${d_low:,.0f}-${d_high:,.0f} range that {lead.condition} "
                            "condition typically implies. If the true rehab lands in that "
                            "range, the MAO shown here is too high."
                        ),
                    )
                )
            elif user_value > d_high * 1.3:
                flags.append(
                    RiskFlag(
                        Severity.LOW,
                        "repairs_conservative",
                        (
                            f"Repair estimate of ${user_value:,.0f} sits above the "
                            f"condition-implied range (${d_low:,.0f}-${d_high:,.0f}). "
                            "Either you know something the condition label does not say, "
                            "or there is room to sharpen the number."
                        ),
                    )
                )
            _ = d_notes
        else:
            flags.append(
                RiskFlag(
                    Severity.MEDIUM,
                    "repairs_uncorroborated",
                    (
                        "No condition was reported, so your repair figure could not be "
                        "cross-checked against anything."
                    ),
                )
            )

        return (
            RepairEstimate(
                low=low,
                mid=mid,
                high=high,
                base=user_value,
                confidence=RepairConfidence.USER_PROVIDED,
                basis_note=". ".join(notes) + f". {_NOT_A_QUOTE}",
            ),
            flags,
        )

    if derived is None:
        flags.append(
            RiskFlag(
                Severity.CRITICAL,
                "no_repair_basis",
                (
                    "No repair estimate and no property condition were supplied. "
                    "Rehab cost is the largest swing factor in a wholesale deal and "
                    "cannot be guessed from an address."
                ),
            )
        )
        return (
            RepairEstimate(
                low=None,
                mid=None,
                high=None,
                base=None,
                confidence=RepairConfidence.INSUFFICIENT_DATA,
                basis_note=(
                    "Neither a repair estimate nor a condition was supplied, so no rehab "
                    "range was produced."
                ),
            ),
            flags,
        )

    low, mid, high, psf_used, notes = derived
    flags.append(
        RiskFlag(
            Severity.MEDIUM,
            "repairs_estimated",
            (
                f"Repairs were not supplied. The ${low:,.0f}-${high:,.0f} range is inferred "
                f"from the reported {lead.condition} condition only, and a walkthrough or "
                "contractor bid could move it materially."
            ),
        )
    )
    return (
        RepairEstimate(
            low=low,
            mid=mid,
            high=high,
            base=mid,
            confidence=RepairConfidence.CONDITION_BASED,
            basis_note="; ".join(notes) + f". {_NOT_A_QUOTE}",
            price_per_sqft_used=psf_used,
        ),
        flags,
    )
