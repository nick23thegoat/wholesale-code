"""The daily priority engine: what to do first, in the order that matters.

Eight bands, ranked by how much it costs to be late:

    1. Seller counters              their number is on the table
    2. Offers requiring response    you are the one holding it up
    3. Overdue follow-ups           you promised and did not
    4. Hot leads with contact       you can act right now
    5. Hot leads needing skip trace you cannot act yet
    6. New high-quality deals       worth looking at today
    7. Contracts near a deadline    inspection and closing dates do not move
    8. Buyer opportunities          a property tied up with no buyer

A contract deadline three days out beats a new lead, however good the new lead
looks — a missed inspection deadline costs earnest money, and a lead will
still be there tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..acquisitions import (
    AcquisitionWorkflow,
    NextAction,
    QueueEntry,
    is_closed,
    normalize_status,
)
from ..formatting import money

#: The eight bands, in priority order.
BANDS = (
    "1. SELLER COUNTERS",
    "2. OFFERS REQUIRING RESPONSE",
    "3. OVERDUE FOLLOW-UPS",
    "4. HOT LEADS WITH CONTACT",
    "5. HOT LEADS NEEDING SKIP TRACE",
    "6. NEW HIGH-QUALITY DEALS",
    "7. CONTRACTS APPROACHING DEADLINES",
    "8. BUYER OPPORTUNITIES",
)

#: A contract deadline this many days out or closer is urgent.
DEADLINE_WARNING_DAYS = 7
#: Deal score at or above this counts as high-quality for band 6.
HIGH_QUALITY_DEAL_SCORE = 70.0
#: Lead score at or above this counts as high-quality for band 6.
HIGH_QUALITY_LEAD_SCORE = 80.0


@dataclass
class PriorityItem:
    """One line of the daily plan."""

    band: str
    action: str
    property_id: str
    address: str
    reason: str = ""
    deal_score: Optional[float] = None
    lead_score: Optional[float] = None
    priority_score: Optional[float] = None
    next_deadline: Optional[date] = None
    days_to_deadline: Optional[int] = None

    @property
    def band_index(self) -> int:
        return BANDS.index(self.band) if self.band in BANDS else len(BANDS)

    def sort_key(self) -> tuple:
        """Band first, then how close the deadline is, then the priority score."""
        deadline = self.days_to_deadline if self.days_to_deadline is not None else 9_999
        return (self.band_index, deadline, -(self.priority_score or 0.0))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "band": self.band,
            "action": self.action,
            "property_id": self.property_id,
            "address": self.address,
            "reason": self.reason,
            "deal_score": self.deal_score,
            "lead_score": self.lead_score,
            "priority_score": self.priority_score,
            "next_deadline": self.next_deadline.isoformat() if self.next_deadline else None,
            "days_to_deadline": self.days_to_deadline,
        }


class DailyPriorityEngine:
    """Builds the ranked action list from everything the workflow knows."""

    def __init__(self, workflow: AcquisitionWorkflow) -> None:
        self.workflow = workflow
        self.store = workflow.store

    def build(
        self, today: Optional[date] = None, limit_per_band: int = 10
    ) -> List[PriorityItem]:
        today = today or date.today()
        entries = self.workflow.queue_entries(today=today)
        by_id = {e.row.dedupe_key: e for e in entries}
        items: List[PriorityItem] = []
        claimed: set = set()

        def add(band: str, action: str, entry: QueueEntry, reason: str, **kwargs: Any) -> None:
            key = entry.row.dedupe_key
            if key in claimed:
                return
            if sum(1 for i in items if i.band == band) >= limit_per_band:
                return
            claimed.add(key)
            row = entry.row
            items.append(
                PriorityItem(
                    band=band, action=action, property_id=key,
                    address=row.address or key, reason=reason,
                    deal_score=row.deal_score, lead_score=row.lead_score,
                    priority_score=row.priority_score, **kwargs
                )
            )

        # --- 1. seller counters -----------------------------------------
        for row, offer in self.workflow.open_counters():
            entry = by_id.get(row.dedupe_key)
            if entry is None:
                continue
            fee = offer.fee_at_current_price
            add(
                BANDS[0], "RESPOND TO COUNTER", entry,
                f"countered at {money(offer.seller_counter)}"
                + (f"; the deal supports {money(fee)} of fee there" if fee is not None else ""),
            )

        # --- 2. offers awaiting a response ------------------------------
        for entry in entries:
            if entry.priority.action in (
                NextAction.AWAIT_OFFER_RESPONSE, NextAction.PREPARE_OFFER
            ):
                add(BANDS[1], str(entry.priority.action), entry, entry.priority.reason)

        # --- 3. overdue follow-ups --------------------------------------
        for follow_up in self.workflow.follow_ups_by_bucket(today)["OVERDUE"]:
            entry = by_id.get(follow_up.row.dedupe_key)
            if entry is None:
                continue
            add(
                BANDS[2], "FOLLOW UP", entry,
                f"{follow_up.days} day(s) overdue"
                + (f" — {follow_up.reason}" if follow_up.reason else ""),
                next_deadline=follow_up.due, days_to_deadline=-follow_up.days,
            )

        # --- 4. hot leads you can call ----------------------------------
        for entry in entries:
            if entry.priority.is_callable:
                add(BANDS[3], "CALL", entry, entry.priority.reason)

        # --- 5. hot leads with no contact route -------------------------
        for entry in entries:
            if entry.priority.needs_skip_trace:
                add(BANDS[4], "SKIP TRACE", entry, entry.priority.reason)

        # --- 6. new high-quality deals ----------------------------------
        for entry in entries:
            row = entry.row
            if normalize_status(row.status) not in ("NEW", "RESEARCHING"):
                continue
            if (row.deal_score or 0) < HIGH_QUALITY_DEAL_SCORE and (
                row.lead_score or 0
            ) < HIGH_QUALITY_LEAD_SCORE:
                continue
            add(
                BANDS[5], "REVIEW", entry,
                f"new: deal {row.deal_score or 0:.0f} / lead {row.lead_score or 0:.0f}"
                + (f", fee {money(row.potential_fee)}" if row.potential_fee else ""),
            )

        # --- 7. contract deadlines --------------------------------------
        for contract in self.store.all_contracts(live_only=True):
            entry = by_id.get(contract.property_id)
            if entry is None:
                continue
            deadlines = [
                ("inspection", contract.inspection_deadline,
                 contract.inspection_days_left(today)),
                ("closing", contract.closing_date, contract.closing_days_left(today)),
            ]
            soonest = min(
                (d for d in deadlines if d[2] is not None),
                key=lambda d: d[2], default=None,
            )
            if soonest is None or soonest[2] > DEADLINE_WARNING_DAYS:
                continue
            label, when, days = soonest
            add(
                BANDS[6], "CONTRACT TASK", entry,
                f"{label} {'in ' + str(days) + ' day(s)' if days >= 0 else str(abs(days)) + ' day(s) PAST'}",
                next_deadline=when, days_to_deadline=days,
            )

        # --- 8. buyer opportunities -------------------------------------
        for entry in entries:
            if entry.priority.action is NextAction.FIND_BUYER:
                row = entry.row
                matches = self.store.matching_buyers(
                    state=row.state, property_type=row.property_type,
                    price=row.recommended_offer,
                )
                add(
                    BANDS[7], "FIND BUYER", entry,
                    f"{len(matches)} buyer(s) match this buy box"
                    if matches else "no buyer on file matches this buy box",
                )

        items.sort(key=lambda i: i.sort_key())
        return items


def render_priority(items: Sequence[PriorityItem], today: Optional[date] = None) -> str:
    """ACTION / PROPERTY / REASON / DEAL / LEAD / PRIORITY / NEXT DEADLINE."""
    today = today or date.today()
    width = 148
    lines = [
        "=" * width,
        f"DAILY PRIORITY — {today.isoformat()}",
        "=" * width,
    ]
    if not items:
        lines.append("  Nothing needs attention. Run --hunt to bring in new leads.")
        lines.append("=" * width)
        return "\n".join(lines)

    current = ""
    number = 0
    for item in items:
        if item.band != current:
            current = item.band
            lines.append("")
            lines.append(current)
            lines.append("-" * width)
            lines.append(
                f"{'':>4} {'ACTION':<20}{'PROPERTY':<30}{'DEAL':>6}{'LEAD':>6}"
                f"{'PRIO':>6}  {'DEADLINE':<12}REASON"
            )
        number += 1

        def score(value: Optional[float]) -> str:
            return "  —  " if value is None else f"{value:5.1f}"

        deadline = item.next_deadline.isoformat() if item.next_deadline else "—"
        lines.append(
            f"{number:>3}. {item.action:<20}{item.address[:29]:<30}"
            f"{score(item.deal_score)}{score(item.lead_score)}{score(item.priority_score)}  "
            f"{deadline:<12}{item.reason}"
        )

    lines.append("")
    lines.append("-" * width)
    lines.append(f"{number} item(s) need attention.")
    lines.append(
        "Nothing here has been sent. Calls, texts and emails are logged by you."
    )
    lines.append("=" * width)
    return "\n".join(lines)
