"""The property dossier — the main research screen.

Everything known about one property on a single page, in the order you would
actually want it when a seller picks up the phone: what it is, who owns it,
why they might sell, what it is worth, what you can pay, and what you have
already done about it.

Every section can say "unknown", and several usually will. That is the report
working correctly — a dossier that never admits a gap is a dossier that is
making things up.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..config import DEFAULT_CONFIG, EngineConfig
from ..formatting import money
from ..lead_hunter.models import LeadResult
from ..priority import PriorityScore
from ..research import PropertyResearch
from ..storage import ChangeSet, StoredLead
from .text_report import _kv, _rule, _wrap, number

WIDTH = 78


def _section(title: str) -> List[str]:
    return ["", title, "-" * WIDTH]


def _fact_line(label: str, fact, formatter=str, width: int = 26) -> str:
    """A researched value with its source, or an honest 'unknown'."""
    if fact is None or not getattr(fact, "is_known", False):
        note = getattr(fact, "note", "") if fact is not None else ""
        value = "unknown" + (f" — {note}" if note else "")
        return _kv(label, value, indent="  ", label_width=width)
    rendered = formatter(fact.value)
    return _kv(
        label, f"{rendered}  [{fact.source}, {fact.confidence}]",
        indent="  ", label_width=width,
    )


def _money_fact(label: str, fact, width: int = 26) -> str:
    return _fact_line(label, fact, lambda v: money(v), width)


def render_dossier(
    result: Optional[LeadResult] = None,
    research: Optional[PropertyResearch] = None,
    priority: Optional[PriorityScore] = None,
    stored: Optional[StoredLead] = None,
    changes: Optional[ChangeSet] = None,
    notes: Optional[Sequence[Dict[str, Any]]] = None,
    activities: Optional[Sequence[Dict[str, Any]]] = None,
    status_history: Optional[Sequence[Dict[str, Any]]] = None,
    config: EngineConfig = DEFAULT_CONFIG,
) -> str:
    """The full dossier. Every argument optional — it renders what it has."""
    lead = result.lead if result else None
    analysis = result.analysis if result else None
    address = (
        (research.address if research else None)
        or (lead.address if lead else None)
        or (stored.address if stored else "")
        or "(unidentified property)"
    )
    identifier = (
        (research.display_id() if research else None)
        or (lead.display_id() if lead else None)
        or (stored.display_id() if stored else "")
    )

    lines = [_rule(), f"PROPERTY DOSSIER — {identifier}", _rule()]

    # --- PROPERTY --------------------------------------------------------
    lines += _section("PROPERTY")
    lines.append(_kv("Address:", address, label_width=26))
    for label, value in (
        ("City:", research.city if research else (lead.city if lead else stored.city if stored else "")),
        ("State:", research.state if research else (lead.state if lead else stored.state if stored else "")),
        ("County:", research.county if research else (lead.county if lead else "")),
        ("ZIP:", research.zip_code if research else (lead.zip_code if lead else "")),
    ):
        if value:
            lines.append(_kv(label, value, label_width=26))

    if research:
        lines.append(_kv("Type:", str(research.property_type), label_width=26))
        lines.append(_kv("Occupancy:", str(research.occupancy), label_width=26))
        lines.append(_kv("Condition:", str(research.condition), label_width=26))
        lines.append(_fact_line("Beds:", research.beds, lambda v: f"{v:g}"))
        lines.append(_fact_line("Baths:", research.baths, lambda v: f"{v:g}"))
        lines.append(_fact_line("Sq Ft:", research.sqft, lambda v: f"{v:,}"))
        lines.append(_fact_line("Year Built:", research.year_built, lambda v: str(int(v))))
        lines.append(_fact_line("Lot Size:", research.lot_size, lambda v: f"{v:,.0f} sqft"))
        lines.append(_money_fact("Current Price:", research.current_price))
        lines.append(_money_fact("Last Sale Price:", research.last_sale_price))
        lines.append(_fact_line("Last Sale Date:", research.last_sale_date))
        lines.append(_fact_line("Days on Market:", research.days_on_market, lambda v: f"{v:,}"))
        lines.append(_money_fact("Tax Amount:", research.tax_amount))
        lines.append(_fact_line("Tax Status:", research.tax_status))
        lines.append(
            _kv("Research Confidence:", str(research.source_confidence), label_width=26)
        )
        lines.append(
            _kv(
                "Sources:",
                ", ".join(research.sources_used) or "none",
                label_width=26,
            )
        )

    # --- OWNER -----------------------------------------------------------
    lines += _section("OWNER")
    if research and research.owner.is_known:
        owner = research.owner
        lines.append(_fact_line("Owner of Record:", owner.owner_name))
        lines.append(_fact_line("Mailing Address:", owner.owner_mailing_address))
        lines.append(_fact_line("Absentee:", owner.absentee_owner, lambda v: "yes" if v else "no"))
        lines.append(_fact_line("Entity Owner:", owner.is_entity, lambda v: "yes" if v else "no"))
        lines.append(_fact_line("Entity Type:", owner.entity_type))
        lines.append(_fact_line("Years Owned:", owner.ownership_years, lambda v: f"{v:g}"))
        lines.append(
            _fact_line("Properties Owned:", owner.properties_owned, lambda v: f"{int(v):,}")
        )
        for note in owner.notes:
            lines.append(_wrap(note, indent="    "))
    else:
        lines.append(_wrap("Owner of record unknown.", indent="  "))
    lines.append(
        _wrap(
            "No phone number or email is held for any property. Contact lookup is "
            "skip tracing, which is a separate regulated step with no provider "
            "connected — this engine will never generate contact details.",
            indent="  ",
        )
    )

    # --- DISTRESS --------------------------------------------------------
    lines += _section("DISTRESS")
    if research and research.distress.count:
        for label in research.distress.labelled():
            lines.append(f"  [confirmed] {label}")
        lines.append(
            _kv(
                "Signals confirmed:",
                f"{research.distress.count} "
                f"({research.distress.urgent_count} time-sensitive)",
                label_width=26,
            )
        )
        lines.append(_fact_line("Foreclosure Status:", research.foreclosure_status))
        if research.distress.unknown:
            lines.append(
                _wrap(
                    "Not checked: " + ", ".join(research.distress.unknown)
                    + ". Unknown never counts against a lead — it is a gap to fill.",
                    indent="  ",
                )
            )
    elif research:
        lines.append(_wrap("No distress signals confirmed.", indent="  "))
        lines.append(_fact_line("Foreclosure Status:", research.foreclosure_status))
    else:
        lines.append(_wrap("No research pass has been run on this property.", indent="  "))

    # --- EQUITY ----------------------------------------------------------
    lines += _section("EQUITY")
    if research:
        equity = research.equity
        lines.append(_kv("Equity:", equity.describe(), label_width=26))
        lines.append(_kv("Status:", str(equity.equity_status), label_width=26))
        lines.append(_kv("Confidence:", str(equity.equity_confidence), label_width=26))
        lines.append(_money_fact("Mortgage Balance:", research.mortgage_balance))
        lines.append(_money_fact("Known Liens:", research.liens))
        lines.append(_wrap(f"Basis: {equity.basis}", indent="    "))
        for caveat in equity.caveats:
            lines.append(_wrap(caveat, indent="    "))
    else:
        lines.append(_wrap("Equity unknown — no research pass.", indent="  "))

    # --- VALUATION / COMPS / REPAIRS / MAO -------------------------------
    if analysis:
        financials = analysis.financials
        lines += _section("VALUATION")
        lines.append(_kv("ARV:", money(analysis.arv.arv), label_width=26))
        lines.append(_kv("ARV Confidence:", str(analysis.arv.confidence), label_width=26))
        lines.append(_wrap(analysis.arv.source_note, indent="    "))

        lines += _section("COMPS")
        lines.append(_kv("Supplied:", str(analysis.comps.count), label_width=26))
        lines.append(_kv("Used:", str(analysis.comps.reliable_count), label_width=26))
        lines.append(_kv("Confidence:", str(analysis.comps.confidence), label_width=26))
        for evaluation in analysis.comps.evaluations[:5]:
            mark = "USED" if evaluation.reliable else "not used"
            lines.append(f"  [{mark}] {evaluation.summary()}")

        lines += _section("REPAIRS")
        lines.append(_kv("Estimate used:", money(analysis.repairs.base), label_width=26))
        lines.append(
            _kv(
                "Low / Mid / High:",
                f"{money(analysis.repairs.low)} / {money(analysis.repairs.mid)} / "
                f"{money(analysis.repairs.high)}",
                label_width=26,
            )
        )
        lines.append(_kv("Basis:", str(analysis.repairs.confidence), label_width=26))
        lines.append(_wrap(analysis.repairs.basis_note, indent="    "))

        lines += _section("MAO AND OFFER")
        lines.append(_kv("70% of ARV:", money(financials.seventy_percent_arv), label_width=26))
        lines.append(_kv("End-Buyer Ceiling:", money(financials.end_buyer_max_price), label_width=26))
        lines.append(_kv("MAO:", money(financials.mao), label_width=26))
        lines.append(
            _kv(
                "Recommended Offer:",
                f"{money(financials.recommended_offer)} "
                f"({financials.offer_discount_pct * 100:.0f}% below MAO)",
                label_width=26,
            )
        )
        lines.append(
            _kv("Deal Cushion (MAO-Offer):", money(financials.potential_gross_spread), label_width=26)
        )

        lines += _section("WHOLESALE ECONOMICS")
        lines.append(
            _kv("Target Wholesale Fee:", money(financials.target_wholesale_fee), label_width=26)
        )
        if financials.potential_wholesale_fee is not None:
            lines.append(
                _kv(
                    f"  at your offer {money(financials.recommended_offer)}:",
                    money(financials.potential_wholesale_fee),
                    label_width=26,
                )
            )
        if financials.wholesale_fee_at_asking is not None:
            lines.append(
                _kv(
                    f"  at asking {money(lead.asking_price if lead else None)}:",
                    money(financials.wholesale_fee_at_asking),
                    label_width=26,
                )
            )
        lines.append(
            _kv("Potential Wholesale Fee:", money(financials.binding_wholesale_fee), label_width=26)
        )
        lines.append(
            _kv("Fee Status:", str(financials.wholesale_fee_status), label_width=26)
        )
        lines.append(
            _kv("Assignment Price:", money(financials.assignment_price), label_width=26)
        )
        lines.append(_kv("Buyer Margin:", money(financials.buyer_margin), label_width=26))
        lines.append(
            _wrap(
                "The target is a target. BELOW TARGET lowers the deal score and "
                "raises a flag; it never rejects a deal on its own.",
                indent="    ",
            )
        )

    # --- SCORES ----------------------------------------------------------
    lines += _section("SCORES")
    if result:
        lines.append(
            _kv(
                "LEAD SCORE:",
                f"{result.score.total:.1f} / 100   {result.score.classification}"
                "   (is this worth a call?)",
                label_width=26,
            )
        )
    if analysis:
        lines.append(
            _kv(
                "DEAL SCORE:",
                f"{analysis.score.total:.1f} / 100   {analysis.score.classification}"
                "   (is this worth a contract?)",
                label_width=26,
            )
        )
    if priority:
        lines.append(
            _kv(
                "PRIORITY SCORE:",
                f"{priority.total:.1f} / 100   {priority.band}"
                "   (what do I work first?)",
                label_width=26,
            )
        )
        for component in sorted(priority.components, key=lambda c: -c.points):
            lines.append(
                f"    {component.name:<20}{component.points:5.1f} / {component.weight:4.1f}  "
                f"{component.note}"
            )
        if priority.rejected_because:
            lines.append(_wrap(priority.rejected_because, indent="    "))

    # --- DECISION --------------------------------------------------------
    if analysis:
        lines += _section("FINAL DECISION")
        lines.append(f"  {analysis.decision}")
        lines.append("")
        lines.append(_wrap(analysis.decision_explanation, indent="    "))

    # --- RISK FLAGS ------------------------------------------------------
    lines += _section("RISK FLAGS")
    flags = analysis.flags_by_severity() if analysis else []
    if flags:
        for flag in flags:
            lines.append(_wrap(f"- [{flag.severity}] {flag.message}", indent="    "))
    else:
        lines.append(_wrap("None raised.", indent="  "))

    # --- MISSING DATA ----------------------------------------------------
    lines += _section("MISSING DATA")
    gaps: List[str] = []
    if analysis:
        gaps += list(analysis.missing_data)
    if research:
        gaps += [f"research: {name}" for name in research.missing_fields]
    if gaps:
        for gap in gaps:
            lines.append(_wrap(f"- {gap}", indent="    "))
    else:
        lines.append(_wrap("Nothing outstanding.", indent="  "))

    # --- STATUS / WATCHLIST ----------------------------------------------
    lines += _section("STATUS")
    if stored:
        lines.append(_kv("Current status:", stored.status, label_width=26))
        lines.append(_kv("First seen:", stored.first_seen, label_width=26))
        lines.append(_kv("Last seen:", stored.last_seen, label_width=26))
        lines.append(_kv("Times seen:", str(stored.times_seen), label_width=26))
        if stored.priority_band:
            lines.append(_kv("Stored priority:", f"{stored.priority_score} {stored.priority_band}", label_width=26))
    else:
        lines.append(_wrap("Not yet stored in the lead database.", indent="  "))

    if status_history:
        lines.append("")
        lines.append("  STATUS HISTORY")
        for entry in status_history:
            arrow = f"{entry['from_status'] or 'new'} -> {entry['to_status']}"
            reason = f"  ({entry['reason']})" if entry.get("reason") else ""
            lines.append(f"    {entry['changed_at'][:16]}  {arrow}{reason}")

    if changes and changes.has_changes:
        lines.append("")
        lines.append("  CHANGES SINCE LAST RUN")
        for change in changes.changes:
            lines.append(f"    {change.description}")
        if changes.priority_bump:
            lines.append(f"    PRIORITY +{changes.priority_bump:.0f}")

    # --- ACTIVITY --------------------------------------------------------
    lines += _section("ACTIVITY HISTORY")
    if activities:
        for entry in activities:
            lines.append(
                f"  {entry['created_at'][:16]}  {entry['activity_type']:<22}"
                f"{entry['description']}"
            )
    else:
        lines.append(_wrap("No activity recorded.", indent="  "))

    # --- NOTES -----------------------------------------------------------
    lines += _section("NOTES")
    if notes:
        for note in notes:
            author = f" — {note['author']}" if note.get("author") else ""
            lines.append(f"  {note['created_at'][:16]}{author}")
            lines.append(_wrap(note["body"], indent="    "))
    else:
        lines.append(
            _wrap(
                "No notes. Add one with --note, and the engine never writes one "
                "for you.",
                indent="  ",
            )
        )

    lines.append("")
    lines.append(
        _wrap(
            "This is a screening tool built only from the data supplied. No deal is "
            "guaranteed profitable. The engine has no access to title, liens, "
            "mortgages, foreclosure filings or owner contact information unless a "
            "provider supplied them, and asserts none of it.",
            indent="  ",
        )
    )
    lines.append(_rule())
    return "\n".join(lines)
