"""Lead filtering.

Two rules govern everything here:

1. A lead is rejected only on information that is actually present. "State is
   not FL/TX/MO" is a rejection; "state is blank" is a NEEDS VERIFICATION
   warning, because a blank cell is not evidence that a lead is bad.
2. Everything is configurable through :class:`LeadHunterConfig` — no state,
   property type or threshold is hard-coded into the logic below.
"""

from __future__ import annotations

from typing import List

from ..config import DEFAULT_LEAD_CONFIG, LeadHunterConfig
from ..models.enums import Condition, Occupancy, PropertyType
from .models import SIGNAL_LABELS, FilterOutcome, Lead, LeadScore


def _record_missing(lead: Lead, label: str) -> None:
    if label not in lead.missing_data:
        lead.missing_data.append(label)


def _needs_verification(lead: Lead, note: str) -> None:
    if note not in lead.needs_verification:
        lead.needs_verification.append(note)


def collect_gaps(lead: Lead) -> None:
    """Record what is missing on this lead, without rejecting it."""
    checks = (
        (not lead.address, "address"),
        (not lead.city, "city"),
        (not lead.state, "state"),
        (not lead.county, "county"),
        (not lead.zip_code, "zip code"),
        (lead.asking_price is None, "asking price"),
        (lead.estimated_value is None, "estimated value / ARV"),
        (lead.estimated_repairs is None, "repair estimate"),
        (lead.beds is None, "beds"),
        (lead.baths is None, "baths"),
        (not lead.sqft, "square footage"),
        (lead.year_built is None, "year built"),
        (lead.property_type is PropertyType.UNKNOWN, "property type"),
        (lead.occupancy is Occupancy.UNKNOWN, "occupancy"),
        (lead.condition is Condition.UNKNOWN, "condition"),
    )
    for is_missing, label in checks:
        if is_missing:
            _record_missing(lead, label)

    unknown = lead.unknown_signals()
    if unknown:
        _needs_verification(
            lead,
            "unconfirmed signals: " + ", ".join(SIGNAL_LABELS.get(s, s) for s in unknown),
        )
    if lead.equity_is_derived:
        _needs_verification(
            lead,
            "equity is derived from the supplied value and asking price, not from a "
            "title search or payoff statement",
        )


def apply_filters(
    lead: Lead,
    score: LeadScore,
    config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
) -> FilterOutcome:
    """Decide whether a lead is worth analyzing, and say why either way."""
    outcome = FilterOutcome()
    collect_gaps(lead)

    # --- market ----------------------------------------------------------
    if lead.state:
        if not config.targets_state(lead.state):
            outcome.reject(
                f"state {lead.state.upper()} is outside the target markets "
                f"({', '.join(config.target_states)})"
            )
    else:
        outcome.warn("state unknown — cannot confirm it is in a target market")

    # --- property type ---------------------------------------------------
    if lead.property_type is PropertyType.UNKNOWN:
        outcome.warn("property type unknown — cannot confirm it is a type we buy")
    elif not config.targets_property_type(lead.property_type.value):
        outcome.reject(
            f"property type {lead.property_type} is not in the target set "
            f"({', '.join(config.preferred_property_types)})"
        )

    # --- lead score ------------------------------------------------------
    if score.total < config.min_lead_score:
        outcome.reject(
            f"lead score {score.total:.0f} is below the minimum of {config.min_lead_score:.0f}"
        )

    # --- price -----------------------------------------------------------
    if config.max_asking_price is not None:
        if lead.asking_price is None:
            outcome.warn("asking price unknown — cannot apply the maximum price filter")
        elif lead.asking_price > config.max_asking_price:
            outcome.reject(
                f"asking price ${lead.asking_price:,.0f} exceeds the maximum of "
                f"${config.max_asking_price:,.0f}"
            )

    # --- equity ----------------------------------------------------------
    if config.min_equity is not None:
        equity = lead.equity_estimate
        if equity is None:
            outcome.warn("equity unknown — cannot apply the minimum equity filter")
        elif equity < config.min_equity:
            outcome.reject(
                f"estimated equity ${equity:,.0f} is below the minimum of "
                f"${config.min_equity:,.0f}"
            )

    # --- occupancy -------------------------------------------------------
    if config.allowed_occupancy:
        if lead.occupancy is Occupancy.UNKNOWN:
            outcome.warn("occupancy unknown — cannot apply the occupancy filter")
        elif lead.occupancy.value not in config.allowed_occupancy:
            outcome.reject(
                f"occupancy {lead.occupancy} is not in the allowed set "
                f"({', '.join(config.allowed_occupancy)})"
            )

    # --- distress signals ------------------------------------------------
    confirmed = set(lead.confirmed_signals())
    if config.required_signals:
        if not confirmed & set(config.required_signals):
            wanted = ", ".join(SIGNAL_LABELS.get(s, s) for s in config.required_signals)
            if lead.unknown_signals():
                outcome.warn(
                    f"none of the required signals ({wanted}) are confirmed, and some "
                    "are unverified"
                )
            outcome.reject(f"no confirmed signal from the required set ({wanted})")
    if config.min_signal_count and len(confirmed) < config.min_signal_count:
        outcome.reject(
            f"{len(confirmed)} confirmed signal(s), minimum is {config.min_signal_count}"
        )

    return outcome


def summarize_filters(outcomes: List[FilterOutcome]) -> str:
    kept = sum(1 for outcome in outcomes if outcome.passed)
    return f"{kept} of {len(outcomes)} leads passed the filters"
