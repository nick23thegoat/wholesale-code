"""Acquisition screens: contact queue, follow-ups, dashboard, daily plan.

The deal room lives in :mod:`wholesale_engine.reports.deal_room`.

One rule shows up everywhere in here: contact information that came from the
mock provider renders as ``TEST DATA``, never as a number you might dial.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence

from ..acquisitions import (
    ACQUISITION_STATUSES,
    CLOSED_STATUSES,
    Dashboard,
    DailyItem,
    FollowUp,
    QueueEntry,
    describe_status,
)
from ..formatting import money

WIDTH = 150


def _score(value: Optional[float]) -> str:
    return "  —  " if value is None else f"{value:5.1f}"


# ---------------------------------------------------------------------------
# Contact queue
# ---------------------------------------------------------------------------

CONTACT_QUEUE_COLUMNS: List[str] = [
    "rank",
    "property_id",
    "address",
    "city",
    "state",
    "owner",
    "phone_status",
    "email_status",
    "lead_score",
    "deal_score",
    "priority_score",
    "acquisition_priority",
    "arv",
    "mao",
    "recommended_offer",
    "potential_fee",
    "fee_status",
    "status",
    "next_action",
    "action_reason",
    "blockers",
]


def contact_queue_rows(entries: Sequence[QueueEntry]) -> List[Dict[str, object]]:
    """Flatten the queue for export. Contact fields stay blank when unknown."""
    rows: List[Dict[str, object]] = []
    for rank, entry in enumerate(entries, start=1):
        row = entry.row
        rows.append(
            {
                "rank": rank,
                "property_id": row.dedupe_key,
                "address": row.address,
                "city": row.city,
                "state": row.state,
                "owner": entry.owner_name,
                "phone_status": entry.phone_status,
                "email_status": entry.email_status,
                "lead_score": row.lead_score,
                "deal_score": row.deal_score,
                "priority_score": row.priority_score,
                "acquisition_priority": entry.priority.score,
                "arv": row.arv,
                "mao": row.mao,
                "recommended_offer": row.recommended_offer,
                "potential_fee": row.potential_fee,
                "fee_status": row.fee_status,
                "status": row.status,
                "next_action": str(entry.priority.action),
                "action_reason": entry.priority.reason,
                "blockers": " | ".join(entry.priority.blockers),
            }
        )
    return rows


def render_contact_queue(
    entries: Sequence[QueueEntry], target_wholesale_fee: float = 18_000.0
) -> str:
    """The call list, most urgent first."""
    lines = [
        "=" * WIDTH,
        "CONTACT QUEUE — who to work, in order",
        "=" * WIDTH,
        f"{'#':>3} {'ADDRESS':<26}{'OWNER':<22}{'PHONE':<16}{'EMAIL':<10}"
        f"{'LEAD':>6}{'DEAL':>6}{'PRIO':>6}{'ACQ':>6}"
        f"{'ARV':>10}{'MAO':>10}{'OFFER':>10}{'FEE':>10}  "
        f"{'STATUS':<16}{'NEXT ACTION':<20}",
        "-" * WIDTH,
    ]

    if not entries:
        lines.append("  Nothing in the queue. Run --hunt to populate the lead database.")
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    for rank, entry in enumerate(entries, start=1):
        row = entry.row
        lines.append(
            f"{rank:>3} {(row.address or row.display_id())[:25]:<26}"
            f"{entry.owner_name[:21]:<22}"
            f"{entry.phone_status[:15]:<16}{entry.email_status[:9]:<10}"
            f"{_score(row.lead_score)}{_score(row.deal_score)}"
            f"{_score(row.priority_score)}{_score(entry.priority.score)}"
            f"{money(row.arv, unknown='—'):>10}{money(row.mao, unknown='—'):>10}"
            f"{money(row.recommended_offer, unknown='—'):>10}"
            f"{money(row.potential_fee, unknown='—'):>10}  "
            f"{row.status[:15]:<16}{str(entry.priority.action)[:19]:<20}"
        )

    lines.append("-" * WIDTH)
    lines.append(f"{len(entries)} in the queue.")

    test_data = sum(1 for e in entries if e.contact and e.contact.is_test_data)
    if test_data:
        lines.append(
            f"{test_data} contact(s) shown as TEST DATA come from the mock skip-trace "
            "provider. They are fictional and must never be dialled or emailed."
        )
    lines.append(
        "ACQ = acquisition priority: the other scores plus whether you can actually "
        "reach anyone."
    )
    lines.append(
        f"FEE is the assignment fee at the price on the table, against a "
        f"{money(target_wholesale_fee)} target. BELOW TARGET is a label, not a rejection."
    )
    lines.append("=" * WIDTH)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

FOLLOW_UP_COLUMNS: List[str] = [
    "bucket",
    "due",
    "days_overdue",
    "property_id",
    "address",
    "state",
    "owner",
    "phone_status",
    "reason",
    "last_outcome",
    "last_contacted",
    "contact_attempts",
    "status",
    "deal_score",
    "potential_fee",
]


def follow_up_rows(follow_ups: Sequence[FollowUp]) -> List[Dict[str, object]]:
    return [
        {
            "bucket": f.bucket,
            "due": f.due.isoformat(),
            "days_overdue": f.days if f.days > 0 else 0,
            "property_id": f.row.dedupe_key,
            "address": f.row.address,
            "state": f.row.state,
            "owner": f.contact.owner_name,
            "phone_status": f.contact.phone_status,
            "reason": f.reason,
            "last_outcome": f.contact.last_outcome,
            "last_contacted": (
                f.contact.last_contacted.isoformat(timespec="seconds")
                if f.contact.last_contacted else None
            ),
            "contact_attempts": f.contact.contact_attempts,
            "status": f.row.status,
            "deal_score": f.row.deal_score,
            "potential_fee": f.row.potential_fee,
        }
        for f in follow_ups
    ]


def render_follow_ups(buckets: Dict[str, List[FollowUp]]) -> str:
    """Overdue first, then today, then what is coming."""
    lines = ["=" * WIDTH, "FOLLOW-UPS", "=" * WIDTH]
    total = sum(len(v) for v in buckets.values())
    if not total:
        lines.append(
            "  Nothing scheduled. Follow-ups are set when you log an outreach "
            "attempt with --follow-up YYYY-MM-DD."
        )
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    for name in ("OVERDUE", "TODAY", "UPCOMING"):
        items = buckets.get(name, [])
        lines.append("")
        lines.append(f"{name} ({len(items)})")
        lines.append("-" * WIDTH)
        if not items:
            lines.append("  none")
            continue
        lines.append(
            f"{'DUE':<12}{'LATE':>6}  {'ADDRESS':<28}{'ST':<4}{'OWNER':<20}"
            f"{'PHONE':<16}{'TRIES':>6}  {'LAST OUTCOME':<16}{'REASON':<30}"
        )
        for item in items:
            late = f"{item.days}d" if item.days > 0 else "—"
            lines.append(
                f"{item.due.isoformat():<12}{late:>6}  "
                f"{(item.row.address or item.row.display_id())[:27]:<28}"
                f"{item.row.state or '--':<4}"
                f"{(item.contact.owner_name or 'unknown')[:19]:<20}"
                f"{item.contact.phone_status[:15]:<16}"
                f"{item.contact.contact_attempts:>6}  "
                f"{(item.contact.last_outcome or '—')[:15]:<16}"
                f"{item.reason[:29]:<30}"
            )

    lines.append("=" * WIDTH)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def render_dashboard(board: Dashboard) -> str:
    """Pipeline counts and projected economics."""
    lines = [
        "=" * WIDTH,
        "ACQUISITION DASHBOARD",
        "=" * WIDTH,
        "",
        "PIPELINE",
        "-" * WIDTH,
    ]
    for status in ACQUISITION_STATUSES:
        count = board.counts.get(status, 0)
        bar = "#" * min(count, 40)
        lines.append(
            f"  {status:<18}{count:>5}  {bar:<40}  {describe_status(status)}"
        )

    lines.append("")
    lines.append("WORK OUTSTANDING")
    lines.append("-" * WIDTH)
    for label, value in (
        ("Follow-ups due today", board.follow_ups_today),
        ("Follow-ups OVERDUE", board.follow_ups_overdue),
        ("Offers open", board.offers_open),
        ("Seller counters waiting", board.counters_waiting),
        ("Contracts live", board.contracts_live),
        ("Assignments in progress", board.assignments_live),
        ("Contacts on file", board.contacts_on_file),
        ("Leads needing a skip trace", board.contacts_needing_skip_trace),
    ):
        lines.append(f"  {label:<32}{value:>6}")
    if board.test_data_contacts:
        lines.append(
            f"  {'of which FICTIONAL TEST DATA':<32}{board.test_data_contacts:>6}"
        )

    lines.append("")
    lines.append("PROJECTED ECONOMICS — NOT EARNED, NOT GUARANTEED")
    lines.append("-" * WIDTH)
    lines.append(
        f"  {'Total pipeline value':<32}{money(board.pipeline_value):>14}"
        "   (sum of recommended offers on live deals)"
    )
    lines.append(
        f"  {'Total potential fees':<32}{money(board.potential_fees):>14}"
        "   (if every live deal closed at today's numbers)"
    )
    lines.append(
        f"  {'Of which under contract':<32}{money(board.contracted_fees):>14}"
        "   (properties already tied up)"
    )
    lines.append(
        f"  {'Average deal score':<32}"
        f"{(f'{board.average_deal_score:.1f}' if board.average_deal_score is not None else '—'):>14}"
    )
    lines.append(
        f"  {'Average lead score':<32}"
        f"{(f'{board.average_lead_score:.1f}' if board.average_lead_score is not None else '—'):>14}"
    )
    lines.append(
        f"  {'Average priority score':<32}"
        f"{(f'{board.average_priority_score:.1f}' if board.average_priority_score is not None else '—'):>14}"
    )
    lines.append(f"  {'Leads tracked':<32}{board.total_leads:>14}")

    lines.append("")
    lines.append(
        "  These are PROJECTED figures from unverified inputs, not income. Most "
        "leads never close, fees move when the ARV or the rehab moves, and nothing"
    )
    lines.append(
        "  here is guaranteed. Treat the totals as a measure of pipeline size, not "
        "of money you are owed."
    )
    lines.append("=" * WIDTH)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Daily plan
# ---------------------------------------------------------------------------


def render_daily(items: Sequence[DailyItem], today: Optional[date] = None) -> str:
    """The numbered to-do list for the day."""
    today = today or date.today()
    lines = [
        "=" * WIDTH,
        f"DAILY ACQUISITIONS PLAN — {today.isoformat()}",
        "=" * WIDTH,
    ]
    if not items:
        lines.append("")
        lines.append("  Nothing needs attention. Run --hunt to bring in new leads.")
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    current_group = ""
    number = 0
    for item in items:
        if item.group != current_group:
            current_group = item.group
            lines.append("")
            lines.append(current_group)
            lines.append("-" * WIDTH)
        number += 1
        lines.append(
            f"{number:>3}. {item.action:<22}{item.address[:34]:<36}{item.detail}"
        )

    lines.append("")
    lines.append("-" * WIDTH)
    lines.append(f"{number} item(s) need attention today.")
    lines.append(
        "Nothing here has been sent. Calls, texts and emails are logged by you "
        "with --log-call / --log-text / --log-email."
    )
    lines.append("=" * WIDTH)
    return "\n".join(lines)
