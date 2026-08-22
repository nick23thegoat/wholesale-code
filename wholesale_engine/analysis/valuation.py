"""Reconcile the user-supplied ARV with the comp-derived ARV.

Rule of the house: a number the user typed is a claim, not a fact. When comps
support it, the ARV is labelled VERIFIED/SUPPORTED. When comps contradict it,
the engine takes the more conservative of the two and flags the conflict. When
there are no usable comps at all, the ARV stays labelled USER-PROVIDED and the
whole analysis inherits that uncertainty.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..config import DEFAULT_CONFIG, EngineConfig
from ..models.enums import ARVConfidence, CompConfidence, Severity
from ..models.property import PropertyLead
from ..models.results import ARVAssessment, CompAnalysis, RiskFlag


def assess_arv(
    lead: PropertyLead,
    comp_analysis: CompAnalysis,
    config: EngineConfig = DEFAULT_CONFIG,
) -> Tuple[ARVAssessment, List[RiskFlag]]:
    """Decide which ARV to underwrite with, and how much to trust it."""
    flags: List[RiskFlag] = []
    user_arv = lead.user_arv if lead.user_arv and lead.user_arv > 0 else None
    comp_arv = comp_analysis.comp_derived_arv
    deviation: Optional[float] = None

    if comp_arv and user_arv:
        deviation = (user_arv - comp_arv) / comp_arv

    # --- No comp support at all ------------------------------------------
    if comp_arv is None:
        if user_arv is None:
            return (
                ARVAssessment(
                    arv=None,
                    confidence=ARVConfidence.INSUFFICIENT_DATA,
                    source_note=(
                        "No ARV was supplied and no usable comps were provided. "
                        "The engine will not invent a value."
                    ),
                    user_arv=None,
                    comp_derived_arv=None,
                ),
                [
                    RiskFlag(
                        Severity.CRITICAL,
                        "no_arv",
                        "No ARV and no usable comps — the deal cannot be underwritten yet.",
                    )
                ],
            )
        flags.append(
            RiskFlag(
                Severity.HIGH,
                "unverified_arv",
                (
                    f"ARV of ${user_arv:,.0f} is user-provided and unverified: "
                    f"{comp_analysis.arv_basis}. Every number downstream inherits this risk."
                ),
            )
        )
        return (
            ARVAssessment(
                arv=user_arv,
                confidence=ARVConfidence.USER_PROVIDED,
                source_note=(
                    "Using the ARV you supplied. It has not been corroborated by comps "
                    f"({comp_analysis.arv_basis})."
                ),
                user_arv=user_arv,
                comp_derived_arv=None,
            ),
            flags,
        )

    # --- Comps exist, no user ARV ----------------------------------------
    if user_arv is None:
        confidence = (
            ARVConfidence.VERIFIED_SUPPORTED
            if comp_analysis.confidence is CompConfidence.HIGH
            else ARVConfidence.ESTIMATED
        )
        if confidence is ARVConfidence.ESTIMATED:
            flags.append(
                RiskFlag(
                    Severity.MEDIUM,
                    "thin_comp_support",
                    (
                        f"ARV of ${comp_arv:,.0f} rests on {comp_analysis.reliable_count} "
                        f"reliable comp(s) at {comp_analysis.confidence} confidence — "
                        "treat it as an estimate, not a value conclusion."
                    ),
                )
            )
        return (
            ARVAssessment(
                arv=comp_arv,
                confidence=confidence,
                source_note=f"Derived from comps: {comp_analysis.arv_basis}.",
                user_arv=None,
                comp_derived_arv=comp_arv,
            ),
            flags,
        )

    # --- Both exist: reconcile -------------------------------------------
    assert deviation is not None
    abs_dev = abs(deviation)
    direction = "above" if deviation > 0 else "below"

    if abs_dev <= config.arv_conflict_tolerance:
        confidence = (
            ARVConfidence.VERIFIED_SUPPORTED
            if comp_analysis.confidence in (CompConfidence.HIGH, CompConfidence.MEDIUM)
            else ARVConfidence.ESTIMATED
        )
        chosen = min(user_arv, comp_arv)
        note = (
            f"Your ARV of ${user_arv:,.0f} is within {abs_dev * 100:.1f}% of the "
            f"comp-derived ${comp_arv:,.0f}. Underwriting at the more conservative "
            f"${chosen:,.0f}. Basis: {comp_analysis.arv_basis}."
        )
        if confidence is ARVConfidence.ESTIMATED:
            flags.append(
                RiskFlag(
                    Severity.MEDIUM,
                    "thin_comp_support",
                    (
                        f"Comps agree with your ARV but only {comp_analysis.reliable_count} "
                        f"reliable comp(s) support it ({comp_analysis.confidence} confidence)."
                    ),
                )
            )
        return (
            ARVAssessment(
                arv=chosen,
                confidence=confidence,
                source_note=note,
                user_arv=user_arv,
                comp_derived_arv=comp_arv,
                deviation_pct=deviation,
            ),
            flags,
        )

    severity = (
        Severity.HIGH if abs_dev > config.arv_major_conflict_tolerance else Severity.MEDIUM
    )
    flags.append(
        RiskFlag(
            severity,
            "arv_conflict",
            (
                f"CONFLICT: your ARV of ${user_arv:,.0f} is {abs_dev * 100:.1f}% {direction} "
                f"the comp-derived ${comp_arv:,.0f}. "
                + (
                    "An inflated ARV is the single most common way a wholesale deal loses money."
                    if deviation > 0
                    else "Your ARV may be leaving money on the table, or the comps may be stale."
                )
            ),
        )
    )

    if comp_analysis.confidence in (CompConfidence.HIGH, CompConfidence.MEDIUM):
        chosen = min(user_arv, comp_arv)
        confidence = ARVConfidence.ESTIMATED
        note = (
            f"Comps ({comp_analysis.confidence} confidence) do not support your ARV. "
            f"Underwriting at the more conservative ${chosen:,.0f}. "
            f"Basis: {comp_analysis.arv_basis}."
        )
    else:
        chosen = min(user_arv, comp_arv)
        confidence = ARVConfidence.ESTIMATED
        note = (
            f"Comp support is weak ({comp_analysis.confidence}) and conflicts with your "
            f"ARV. Underwriting at the more conservative ${chosen:,.0f}, but this number "
            "needs real verification before you contract."
        )

    return (
        ARVAssessment(
            arv=chosen,
            confidence=confidence,
            source_note=note,
            user_arv=user_arv,
            comp_derived_arv=comp_arv,
            deviation_pct=deviation,
        ),
        flags,
    )
