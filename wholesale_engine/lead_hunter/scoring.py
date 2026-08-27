"""The 0-100 LEAD score.

This score answers one question: **is this lead worth working?** It measures
distress and opportunity signals, not economics. It is deliberately kept apart
from the Wave 1 DEAL score in
:mod:`wholesale_engine.analysis.scoring`, which answers a completely different
question: is the property worth buying at the price on offer?

A 🔥 HOT lead with a ❌ PASS deal is a normal, expected outcome. It means the
seller is worth a phone call and the price is not worth a contract.
"""

from __future__ import annotations

from typing import List

from ..config import DEFAULT_LEAD_CONFIG, LeadHunterConfig
from ..models.enums import Classification, Condition, SellerMotivation
from .models import Lead, LeadScore, SignalHit


def classify_lead(score: float, config: LeadHunterConfig = DEFAULT_LEAD_CONFIG) -> Classification:
    """Map a 0-100 lead score onto its band (same bands as the deal score)."""
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


def _high_equity_hit(lead: Lead, config: LeadHunterConfig) -> tuple:
    """(is_high_equity, basis) — reported first, otherwise derived arithmetic."""
    if lead.high_equity is True:
        return True, "reported by the source"
    if lead.high_equity is False:
        return False, ""
    ratio = lead.equity_ratio
    if ratio is None:
        return False, ""
    if ratio >= config.high_equity_ratio:
        return True, (
            f"derived: estimated value less asking price leaves {ratio * 100:.0f}% "
            "equity (arithmetic on supplied figures, not a title search)"
        )
    return False, ""


def _significant_repairs_hit(lead: Lead, config: LeadHunterConfig) -> tuple:
    if lead.estimated_repairs is not None and (
        lead.estimated_repairs >= config.significant_repair_threshold
    ):
        return True, f"reported repairs of ${lead.estimated_repairs:,.0f}"
    if lead.condition is not Condition.UNKNOWN and (
        lead.condition.value in config.significant_repair_conditions
    ):
        return True, f"reported {lead.condition} condition"
    return False, ""


def _motivation_hit(lead: Lead, config: LeadHunterConfig) -> tuple:
    """Motivation scores only when motivation information was supplied."""
    if lead.seller_motivation is SellerMotivation.UNKNOWN:
        return 0.0, ""
    points = config.motivation_points.get(lead.seller_motivation.value, 0.0)
    if points <= 0:
        return 0.0, ""
    return points, f"reported {lead.seller_motivation} seller motivation"


def score_lead(lead: Lead, config: LeadHunterConfig = DEFAULT_LEAD_CONFIG) -> LeadScore:
    """Score one lead 0-100 on its distress and opportunity signals."""
    candidates: List[SignalHit] = []

    for name, value in lead.signal_values().items():
        if name == "high_equity":
            continue
        if value is True:
            candidates.append(
                SignalHit(name, config.signal_value(name), "reported by the source")
            )

    is_high_equity, basis = _high_equity_hit(lead, config)
    if is_high_equity:
        candidates.append(SignalHit("high_equity", config.signal_value("high_equity"), basis))

    has_repairs, repair_basis = _significant_repairs_hit(lead, config)
    if has_repairs:
        candidates.append(
            SignalHit("significant_repairs", config.signal_value("significant_repairs"), repair_basis)
        )

    motivation_points, motivation_basis = _motivation_hit(lead, config)
    if motivation_points > 0:
        candidates.append(SignalHit("seller_motivation", motivation_points, motivation_basis))

    # Collapse signals that describe the same underlying event: score the
    # highest member of each group once instead of stacking them.
    suppressed: List[str] = []
    by_name = {hit.name: hit for hit in candidates}
    for group in config.exclusive_signal_groups:
        present = [name for name in group if name in by_name]
        if len(present) > 1:
            keep = max(present, key=lambda name: by_name[name].points)
            for name in present:
                if name != keep:
                    suppressed.append(name)
                    by_name.pop(name)

    hits = [hit for hit in candidates if hit.name in by_name]
    total = min(sum(hit.points for hit in hits), config.max_lead_score)

    return LeadScore(
        total=round(total, 1),
        classification=classify_lead(total, config),
        hits=hits,
        unknown_signals=lead.unknown_signals(),
        suppressed=suppressed,
    )
