"""The deal room: everything about one deal, on one screen.

The dossier (``--property``) answers "what is this property?" The deal room
answers "where does this deal stand?" — the same underwriting, plus the
contact, the conversation, the offers, the contract and the buyer.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from ..acquisitions import (
    Assignment,
    Contact,
    Contract,
    Offer,
    OutreachActivity,
    describe_status,
)
from ..acquisitions.contact_priority import ContactPriority
from ..config import DEFAULT_CONFIG, EngineConfig
from ..formatting import money
from ..priority import PriorityScore
from ..research import PropertyResearch
from ..storage import StoredLead
from .text_report import _kv, _rule, _wrap

WIDTH = 78


def _section(title: str) -> List[str]:
    return ["", title, "-" * WIDTH]


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_deal_room(
    row: StoredLead,
    contact: Optional[Contact] = None,
    outreach: Optional[Sequence[OutreachActivity]] = None,
    offers: Optional[Sequence[Offer]] = None,
    contract: Optional[Contract] = None,
    assignment: Optional[Assignment] = None,
    research: Optional[PropertyResearch] = None,
    priority: Optional[PriorityScore] = None,
    acquisition_priority: Optional[ContactPriority] = None,
    analysis=None,
    notes: Optional[Sequence[dict]] = None,
    config: EngineConfig = DEFAULT_CONFIG,
    today: Optional[date] = None,
) -> str:
    """The whole deal on one screen. Renders whatever is available."""
    today = today or date.today()
    lines = [
        _rule(),
        f"DEAL ROOM — {row.address or row.display_id()}",
        _rule(),
        _kv("Status:", f"{row.status}  ({describe_status(row.status)})", label_width=26),
    ]
    if acquisition_priority is not None:
        lines.append(
            _kv("Next action:", str(acquisition_priority.action), label_width=26)
        )
        lines.append(_wrap(acquisition_priority.reason, indent="    "))
        for blocker in acquisition_priority.blockers:
            lines.append(_wrap(f"BLOCKER: {blocker}", indent="    "))

    # --- PROPERTY --------------------------------------------------------
    lines += _section("PROPERTY")
    for label, value in (
        ("Address:", row.address),
        ("City:", row.city),
        ("State:", row.state),
        ("County:", row.county),
        ("ZIP:", row.zip_code),
        ("Type:", row.property_type),
    ):
        if value:
            lines.append(_kv(label, value, label_width=26))
    lines.append(_kv("Asking Price:", money(row.asking_price), label_width=26))
    if row.days_on_market is not None:
        lines.append(_kv("Days on Market:", f"{row.days_on_market:,}", label_width=26))
    lines.append(_kv("First seen / last seen:", f"{row.first_seen} / {row.last_seen}", label_width=26))

    # --- OWNER -----------------------------------------------------------
    lines += _section("OWNER")
    owner_name = (contact.owner_name if contact else None) or (
        research.owner_name if research else None
    )
    lines.append(_kv("Owner of record:", owner_name or "unknown", label_width=26))
    if research is not None and research.owner.is_entity.is_true:
        lines.append(
            _kv("Entity:", str(research.owner.entity_type.value or "yes"), label_width=26)
        )
    if research is not None:
        lines.append(
            _kv(
                "Absentee:",
                {True: "yes", False: "no", None: "unknown"}[research.absentee_owner],
                label_width=26,
            )
        )

    # --- CONTACT ---------------------------------------------------------
    lines += _section("CONTACT")
    if contact is None or not contact.is_reachable:
        lines.append(
            _wrap(
                "No contact information on file. Nothing has been invented — a "
                "phone number or email appears here only when a source supplied "
                "one.",
                indent="  ",
            )
        )
    else:
        lines.append(_kv("Phone:", f"{contact.display_phone()}  [{contact.phone_status}]", label_width=26))
        lines.append(_kv("Email:", f"{contact.display_email()}  [{contact.email_status}]", label_width=26))
        lines.append(_kv("Mailing address:", contact.mailing_address or "unknown", label_width=26))
        lines.append(_kv("Source:", contact.source or "unknown", label_width=26))
        lines.append(_kv("Provenance:", contact.provenance, label_width=26))
        if contact.is_test_data:
            lines.append(
                _wrap(
                    "FICTIONAL TEST DATA from the mock skip-trace provider. These "
                    "are reserved 555-01xx numbers and .invalid addresses. They "
                    "belong to nobody. Do not dial or email them.",
                    indent="    ",
                )
            )
        lines.append(
            _kv("Attempts / last outcome:",
                f"{contact.contact_attempts} / {contact.last_outcome or '—'}",
                label_width=26)
        )

    # --- SCORES ----------------------------------------------------------
    lines += _section("SCORES")
    lines.append(
        _kv("LEAD SCORE:", f"{row.lead_score if row.lead_score is not None else '—'}"
            "   (worth a call?)", label_width=26)
    )
    lines.append(
        _kv("DEAL SCORE:", f"{row.deal_score if row.deal_score is not None else '—'}"
            "   (worth a contract?)", label_width=26)
    )
    lines.append(
        _kv("PRIORITY SCORE:",
            f"{row.priority_score if row.priority_score is not None else '—'} "
            f"{row.priority_band}   (work first?)", label_width=26)
    )
    if acquisition_priority is not None:
        lines.append(
            _kv("ACQUISITION PRIORITY:",
                f"{acquisition_priority.score:.1f}   (call first?)", label_width=26)
        )

    # --- DISTRESS / EQUITY ----------------------------------------------
    lines += _section("DISTRESS")
    if research is not None and research.distress.count:
        for label in research.distress.labelled():
            lines.append(f"  [confirmed] {label}")
    else:
        lines.append(
            _kv("Signals confirmed:", str(row.distress_count or 0), label_width=26)
        )
        if not row.distress_count:
            lines.append(_wrap("None confirmed.", indent="  "))

    lines += _section("EQUITY")
    if row.equity_amount is not None:
        lines.append(
            _kv("Estimated equity:",
                f"{money(row.equity_amount)} ({_pct(row.equity_percentage)})",
                label_width=26)
        )
        lines.append(_kv("Basis:", row.equity_status or "UNKNOWN", label_width=26))
        if research is not None:
            for caveat in research.equity.caveats:
                lines.append(_wrap(caveat, indent="    "))
    else:
        lines.append(
            _wrap(
                "Equity unknown — no mortgage balance is available and none has "
                "been assumed.",
                indent="  ",
            )
        )

    # --- ECONOMICS -------------------------------------------------------
    lines += _section("ECONOMICS")
    ceiling = None
    if row.arv is not None and row.repair_estimate is not None:
        ceiling = row.arv * config.arv_percentage - row.repair_estimate
    lines.append(
        _kv("ARV:", f"{money(row.arv)}  [{row.arv_confidence or 'UNKNOWN'}]", label_width=26)
    )
    lines.append(_kv("Comp confidence:", row.comp_confidence or "UNKNOWN", label_width=26))
    lines.append(_kv("Repairs:", money(row.repair_estimate), label_width=26))
    lines.append(_kv("End-buyer ceiling:", money(ceiling), label_width=26))
    lines.append(_kv("MAO:", money(row.mao), label_width=26))
    lines.append(_kv("Recommended offer:", money(row.recommended_offer), label_width=26))

    latest = offers[0] if offers else None
    if latest is not None:
        lines.append(
            _kv("Current offer:",
                f"{money(latest.offer_amount)}  [{latest.offer_status}]", label_width=26)
        )
        if latest.seller_counter is not None:
            lines.append(
                _kv("Seller counter:", money(latest.seller_counter), label_width=26)
            )
            lines.append(
                _kv("Price on the table:", money(latest.current_proposed_price), label_width=26)
            )
    else:
        lines.append(_kv("Current offer:", "none yet", label_width=26))

    lines.append("")
    lines.append("  WHOLESALE FEE")
    lines.append(
        _kv("Target Wholesale Fee:", money(config.target_wholesale_fee),
            indent="    ", label_width=26)
    )
    lines.append(
        _kv("Potential Wholesale Fee:", money(row.potential_fee),
            indent="    ", label_width=26)
    )
    if latest is not None and latest.fee_at_current_price is not None:
        lines.append(
            _kv("  at the price on the table:", money(latest.fee_at_current_price),
                indent="    ", label_width=26)
        )
    lines.append(
        _kv("Wholesale Fee Status:", row.fee_status or "UNKNOWN",
            indent="    ", label_width=26)
    )
    if latest is not None:
        if latest.distance_to_mao is not None:
            lines.append(
                _kv("Distance to MAO:", money(latest.distance_to_mao),
                    indent="    ", label_width=26)
            )
        if latest.distance_to_target_fee is not None:
            lines.append(
                _kv("Distance to target fee:", money(latest.distance_to_target_fee),
                    indent="    ", label_width=26)
            )
    lines.append(
        _wrap(
            "The target is a target. BELOW TARGET lowers the deal score and raises "
            "a flag; it never rejects a deal on its own.",
            indent="      ",
        )
    )

    # --- CONTACT HISTORY -------------------------------------------------
    lines += _section("CONTACT HISTORY")
    if outreach:
        for activity in outreach[:12]:
            stamp = activity.timestamp.isoformat(timespec="minutes") if activity.timestamp else "—"
            lines.append(
                f"  {stamp:<18}{str(activity.channel):<11}"
                f"{str(activity.outcome or '—'):<16}{activity.notes[:30]}"
            )
    else:
        lines.append(_wrap("No outreach logged yet.", indent="  "))

    # --- FOLLOW-UP -------------------------------------------------------
    lines += _section("FOLLOW-UP")
    if contact is not None and contact.next_follow_up is not None:
        days = (today - contact.next_follow_up).days
        state = (
            f"OVERDUE by {days} day(s)" if days > 0
            else "due TODAY" if days == 0
            else f"in {abs(days)} day(s)"
        )
        lines.append(
            _kv("Next follow-up:", f"{contact.next_follow_up.isoformat()}  ({state})",
                label_width=26)
        )
        if contact.follow_up_reason:
            lines.append(_kv("Reason:", contact.follow_up_reason, label_width=26))
    else:
        lines.append(_wrap("Nothing scheduled.", indent="  "))

    # --- OFFER HISTORY ---------------------------------------------------
    lines += _section("OFFER HISTORY")
    if offers:
        for offer in offers:
            when = offer.offer_date.isoformat() if offer.offer_date else "—"
            lines.append(
                f"  {when:<12}{money(offer.offer_amount):>12}  {str(offer.offer_status):<12}"
                + (f"counter {money(offer.seller_counter)}" if offer.seller_counter else "")
            )
            for warning in offer.warnings:
                lines.append(_wrap(warning, indent="      "))
    else:
        lines.append(_wrap("No offers recorded.", indent="  "))

    # --- CONTRACT --------------------------------------------------------
    lines += _section("CONTRACT STATUS")
    if contract is not None:
        lines.append(_kv("Status:", str(contract.status), label_width=26))
        lines.append(_kv("Purchase price:", money(contract.purchase_price), label_width=26))
        lines.append(
            _kv("Contract date:",
                contract.contract_date.isoformat() if contract.contract_date else "—",
                label_width=26)
        )
        for label, deadline, days in (
            ("Inspection deadline:", contract.inspection_deadline,
             contract.inspection_days_left(today)),
            ("Closing date:", contract.closing_date, contract.closing_days_left(today)),
        ):
            if deadline is not None:
                marker = (
                    f"{days} day(s) away" if days is not None and days >= 0
                    else f"{abs(days)} day(s) PAST" if days is not None else ""
                )
                lines.append(_kv(label, f"{deadline.isoformat()}  ({marker})", label_width=26))
        lines.append(_kv("Earnest money:", money(contract.earnest_money), label_width=26))
        lines.append(
            _kv("Assignment allowed:",
                {True: "yes", False: "NO", None: "unknown"}[contract.assignment_allowed],
                label_width=26)
        )
        if contract.assignment_allowed is False:
            lines.append(
                _wrap(
                    "The contract as recorded does not permit assignment. That "
                    "changes the exit — confirm with your attorney before you "
                    "market it to a buyer.",
                    indent="    ",
                )
            )
        lines.append(
            _wrap(
                "Tracking only. This engine drafts no documents and gives no legal "
                "advice — use your own attorney and title company.",
                indent="    ",
            )
        )
    else:
        lines.append(_wrap("Not under contract.", indent="  "))

    # --- BUYER -----------------------------------------------------------
    lines += _section("BUYER STATUS")
    if assignment is not None:
        lines.append(_kv("Status:", str(assignment.status), label_width=26))
        lines.append(_kv("Buyer:", assignment.buyer_name or "not identified", label_width=26))
        lines.append(_kv("Purchase price:", money(assignment.purchase_price), label_width=26))
        lines.append(_kv("Assignment price:", money(assignment.assignment_price), label_width=26))
        lines.append(
            _kv("Gross assignment fee:", money(assignment.gross_assignment_fee), label_width=26)
        )
    else:
        lines.append(_wrap("No buyer process started.", indent="  "))

    # --- RISK / GAPS -----------------------------------------------------
    lines += _section("RISK FLAGS")
    if analysis is not None and analysis.risk_flags:
        for flag in analysis.flags_by_severity():
            lines.append(_wrap(f"- [{flag.severity}] {flag.message}", indent="    "))
    elif row.final_decision:
        lines.append(
            _wrap(
                f"Stored decision: {row.final_decision}. Run --property "
                f"{row.dedupe_key} for the full flag list.",
                indent="  ",
            )
        )
    else:
        lines.append(_wrap("None on file.", indent="  "))

    lines += _section("MISSING DATA")
    gaps: List[str] = []
    if analysis is not None:
        gaps += list(analysis.missing_data)
    if research is not None:
        gaps += [f"research: {name}" for name in research.missing_fields[:10]]
    if contact is None or not contact.has_phone:
        gaps.append("owner phone number — needs a skip trace")
    if contact is None or not contact.has_email:
        gaps.append("owner email address")
    if gaps:
        for gap in gaps:
            lines.append(_wrap(f"- {gap}", indent="    "))
    else:
        lines.append(_wrap("Nothing outstanding.", indent="  "))

    if notes:
        lines += _section("NOTES")
        for note in notes:
            lines.append(f"  {note['created_at'][:16]}")
            lines.append(_wrap(note["body"], indent="    "))

    lines.append("")
    lines.append(
        _wrap(
            "Screening and tracking only. No deal is guaranteed profitable, nothing "
            "here is legal advice, and this engine sends no calls, texts or emails.",
            indent="  ",
        )
    )
    lines.append(_rule())
    return "\n".join(lines)
