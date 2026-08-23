"""Human-readable deal report.

The section order and headings are fixed by the underwriting spec: PROPERTY,
PROPERTY DETAILS, FINANCIALS, COMPS, DEAL SCORE, RISK FLAGS, MISSING DATA,
FINAL DECISION. Unknown values print as ``NOT PROVIDED`` — never as 0, and
never as a guess.
"""

from __future__ import annotations

import textwrap
from typing import Iterable, List, Optional

from ..config import DEFAULT_CONFIG, EngineConfig
from ..formatting import UNKNOWN as _UNKNOWN
from ..formatting import money
from ..models.enums import Condition, Occupancy, PropertyType
from ..models.results import AnalysisResult

WIDTH = 78


def number(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return _UNKNOWN
    if isinstance(value, float) and value != int(value):
        return f"{value:,.1f}{suffix}"
    return f"{int(value):,}{suffix}"


def _text(value: str) -> str:
    return value if value else _UNKNOWN


def _enum(value) -> str:
    if value in (Condition.UNKNOWN, Occupancy.UNKNOWN, PropertyType.UNKNOWN):
        return _UNKNOWN
    return str(value)


def _wrap(text: str, indent: str = "  ") -> str:
    """Wrap to the report width, keeping any bullet marker on the first line only."""
    return textwrap.fill(
        text,
        width=WIDTH,
        initial_indent=indent,
        subsequent_indent=" " * len(indent) + "  ",
    )


def _kv(label: str, value: str, indent: str = "  ", label_width: int = 27) -> str:
    """One aligned ``label: value`` line."""
    return f"{indent}{label:<{label_width}}{value}"


def _rule(char: str = "=") -> str:
    return char * WIDTH


def _header(title: str) -> str:
    return f"\n{title}\n{_rule('-')}"


def render_result(result: AnalysisResult, config: EngineConfig = DEFAULT_CONFIG) -> str:
    """Render one analysis as the full text report."""
    lead = result.lead
    fin = result.financials
    lines: List[str] = []

    lines.append(_rule())
    lines.append(f"WHOLESALE DEAL ANALYSIS — {lead.display_id()}")
    lines.append(_rule())

    lines.append(_header("PROPERTY"))
    lines.append(f"  Address:  {_text(lead.address)}")
    lines.append(f"  City:     {_text(lead.city)}")
    lines.append(f"  State:    {_text(lead.state)}")
    lines.append(f"  County:   {_text(lead.county)}")

    lines.append(_header("PROPERTY DETAILS"))
    lines.append(_kv("Beds:", number(lead.beds), label_width=16))
    lines.append(_kv("Baths:", number(lead.baths), label_width=16))
    lines.append(_kv("Sq Ft:", number(lead.sqft), label_width=16))
    lines.append(_kv("Lot Size:", number(lead.lot_size_sqft, " sqft"), label_width=16))
    lines.append(
        _kv("Year:", str(lead.year_built) if lead.year_built else _UNKNOWN, label_width=16)
    )
    lines.append(_kv("Type:", _enum(lead.property_type), label_width=16))
    lines.append(_kv("Occupancy:", _enum(lead.occupancy), label_width=16))
    lines.append(_kv("Condition:", _enum(lead.condition), label_width=16))
    lines.append(
        _kv(
            "Days on Market:",
            _UNKNOWN if lead.days_on_market is None else str(lead.days_on_market),
            label_width=16,
        )
    )
    lines.append(_kv("Est. Rent:", money(lead.estimated_monthly_rent), label_width=16))
    if lead.distress_indicators:
        lines.append(
            _kv("Distress:", ", ".join(lead.distress_indicators) + " (as reported)", label_width=16)
        )
    if lead.notes:
        lines.append(_wrap(f"Notes: {lead.notes}", indent="  "))

    lines.append(_header("FINANCIALS"))
    lines.append(_kv("Asking Price:", money(lead.asking_price)))
    lines.append(_kv("ARV:", f"{money(result.arv.arv)}  [{result.arv.confidence}]"))
    lines.append(_wrap(result.arv.source_note, indent="      "))
    lines.append(
        _kv("Repair Estimate (used):", f"{money(result.repairs.base)}  [{result.repairs.confidence}]")
    )
    lines.append(
        _kv(
            "Low / Mid / High:",
            f"{money(result.repairs.low)} / {money(result.repairs.mid)} / "
            f"{money(result.repairs.high)}",
            indent="    ",
            label_width=25,
        )
    )
    lines.append(_wrap(result.repairs.basis_note, indent="      "))
    lines.append(
        _kv(f"{config.arv_percentage * 100:.0f}% of ARV:", money(fin.seventy_percent_arv))
    )
    lines.append(_kv("Target Wholesale Fee:", money(config.target_wholesale_fee)))
    lines.append(_kv("End-Buyer Ceiling:", money(fin.end_buyer_max_price)))
    lines.append(
        _wrap(
            "The most a cash end buyer can pay under the same rule "
            f"({config.arv_percentage * 100:.0f}% of ARV less repairs). MAO is this ceiling "
            "less your target fee.",
            indent="      ",
        )
    )
    lines.append(_kv("MAO:", money(fin.mao)))
    lines.append(
        _kv(
            "Recommended Offer:",
            money(fin.recommended_offer)
            + (
                f"  ({fin.offer_discount_pct * 100:.0f}% below MAO)"
                if fin.recommended_offer is not None
                else ""
            ),
        )
    )
    if fin.recommended_offer is not None and fin.offer_discount_reasons:
        lines.append(
            _wrap(
                "Offer set below MAO because: " + "; ".join(fin.offer_discount_reasons),
                indent="      ",
            )
        )
    lines.append(_kv("Potential Assignment Price:", money(fin.assignment_price)))
    lines.append(
        _kv("Deal Cushion (MAO - Offer):", money(fin.potential_gross_spread))
    )
    lines.append(
        _wrap(
            "Cushion is room ON TOP of the target fee, which MAO already reserved. It is "
            "not the fee — read the fee on the next line.",
            indent="      ",
        )
    )

    lines.append("")
    lines.append("  WHOLESALE FEE")
    lines.append(_kv("Target Wholesale Fee:", money(config.target_wholesale_fee), indent="    ", label_width=28))
    lines.append(
        _kv("Potential Wholesale Fee:", money(fin.binding_wholesale_fee), indent="    ", label_width=28)
    )
    lines.append(
        _kv("Wholesale Fee Status:", str(fin.wholesale_fee_status), indent="    ", label_width=28)
    )
    if fin.potential_wholesale_fee is not None:
        lines.append(
            _kv(
                "  at recommended offer:",
                money(fin.potential_wholesale_fee),
                indent="    ",
                label_width=28,
            )
        )
    if fin.wholesale_fee_at_asking is not None:
        lines.append(
            _kv("  at asking price:", money(fin.wholesale_fee_at_asking), indent="    ", label_width=28)
        )
    if fin.buyer_margin is not None:
        lines.append(_kv("Buyer Margin at Assignment:", money(fin.buyer_margin), indent="    ", label_width=28))
    lines.append(
        _wrap(
            "The fee is judged at the price actually on the table: the asking price when "
            "the seller is asking more than you plan to offer, otherwise the recommended "
            "offer. An offer the seller has not accepted cannot qualify a deal.",
            indent="      ",
        )
    )
    lines.append("")
    if fin.spread_vs_asking is not None:
        lines.append(_kv("MAO vs Asking:", money(fin.spread_vs_asking)))
    if fin.discount_from_arv_pct is not None:
        lines.append(
            _kv("Asking Discount from ARV:", f"{fin.discount_from_arv_pct * 100:.1f}%")
        )

    if fin.scenarios:
        lines.append("")
        lines.append("  MAO BY REHAB SCENARIO")
        for scenario in fin.scenarios:
            gap = (
                ""
                if scenario.spread_vs_asking is None
                else f"   (vs asking: {money(scenario.spread_vs_asking)})"
            )
            lines.append(
                f"    {scenario.name:<11} repairs {money(scenario.repairs):>10}"
                f"  ->  MAO {money(scenario.mao):>10}{gap}"
            )

    lines.append(_header("COMPS"))
    comps = result.comps
    lines.append(_kv("Number of comps supplied:", str(comps.count)))
    lines.append(_kv("Comps used for valuation:", str(comps.reliable_count)))
    lines.append(_kv("Best comp:", comps.best.summary() if comps.best else _UNKNOWN))
    lines.append(
        _kv("Worst comp:", comps.worst.summary() if comps.worst and comps.count > 1 else _UNKNOWN)
    )
    lines.append(_kv("Comp confidence:", str(comps.confidence)))
    lines.append(_kv("ARV confidence:", str(result.arv.confidence)))
    if result.arv.user_arv is not None and result.arv.comp_derived_arv is not None:
        lines.append(
            f"  User ARV {money(result.arv.user_arv)} vs comp-derived "
            f"{money(result.arv.comp_derived_arv)}"
            + (
                f" ({result.arv.deviation_pct * 100:+.1f}%)"
                if result.arv.deviation_pct is not None
                else ""
            )
        )
    if comps.price_per_sqft_low is not None and comps.price_per_sqft_high is not None:
        lines.append(
            f"  Reliable comp range: ${comps.price_per_sqft_low:,.0f}-"
            f"${comps.price_per_sqft_high:,.0f} per sqft"
        )
    for note in comps.notes:
        lines.append(_wrap(note, indent="    - "))
    if comps.evaluations:
        lines.append("")
        lines.append("  COMP DETAIL")
        for evaluation in sorted(
            comps.evaluations, key=lambda e: e.quality_score, reverse=True
        ):
            mark = "USED" if evaluation.reliable else "not used"
            lines.append(f"    [{mark}] {evaluation.summary()}")
            for reason in evaluation.reasons:
                lines.append(_wrap(reason, indent="        - "))

    lines.append(_header("DEAL SCORE"))
    lines.append(f"  Score:          {result.score.total:.1f} / 100")
    lines.append(f"  Classification: {result.score.classification}")
    if result.score.needs_more_data:
        lines.append("  ⚠️ NEEDS MORE DATA — critical inputs are missing or unverified.")
    lines.append("")
    lines.append("  COMPONENT BREAKDOWN")
    for component in result.score.components:
        lines.append(
            f"    {component.name:<20} {component.points:5.1f} / {component.weight:4.1f}"
        )
        lines.append(_wrap(component.note, indent="        "))

    lines.append(_header("RISK FLAGS"))
    if result.risk_flags:
        for flag in result.flags_by_severity():
            lines.append(_wrap(f"[{flag.severity}] {flag.message}", indent="  - "))
    else:
        lines.append("  No major concerns identified from the data supplied.")

    lines.append(_header("MISSING DATA"))
    if result.missing_data:
        for gap in result.missing_data:
            lines.append(_wrap(gap, indent="  - "))
    else:
        lines.append("  Nothing material is missing.")

    lines.append(_header("FINAL DECISION"))
    lines.append(f"  {result.decision}")
    lines.append("")
    lines.append(_wrap(result.decision_explanation, indent="  "))
    lines.append("")
    lines.append(_wrap(DISCLAIMER, indent="  "))
    lines.append(_rule())
    return "\n".join(lines)


DISCLAIMER = (
    "This analysis is a screening tool built only from the data supplied. No deal is "
    "guaranteed profitable. The engine has no access to public records, title, liens, "
    "mortgages, foreclosure status, or owner contact information, and asserts none of it. "
    "Verify value, repairs, title and possession independently before contracting."
)


def render_batch_summary(results: Iterable[AnalysisResult]) -> str:
    """One-line-per-deal summary table for a batch run."""
    rows = list(results)
    lines = [_rule(), f"BATCH SUMMARY — {len(rows)} propert{'y' if len(rows) == 1 else 'ies'}", _rule()]
    header = f"{'ADDRESS':<32}{'SCORE':>7}  {'CLASS':<12}{'DECISION':<18}{'OFFER':>12}"
    lines.append(header)
    lines.append("-" * WIDTH)
    for result in sorted(rows, key=lambda r: r.score.total, reverse=True):
        address = (result.lead.address or result.lead.display_id())[:31]
        offer = money(result.financials.recommended_offer)
        lines.append(
            f"{address:<32}{result.score.total:>7.1f}  "
            f"{str(result.score.classification):<12}{str(result.decision):<18}{offer:>12}"
        )
    lines.append(_rule())
    return "\n".join(lines)
