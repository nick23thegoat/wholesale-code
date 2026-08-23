"""Who to call next, and what to do about the ones you can't call yet.

A fourth score, and like the other three it answers its own question:

===================  ==================================================
LEAD SCORE           is this worth a phone call?
DEAL SCORE           is this worth a contract?
PRIORITY SCORE       what do I work on first?
ACQUISITION PRIORITY what is my next physical action, right now?
===================  ==================================================

The difference from the priority score is contact availability. A brilliant
deal with no phone number is not a call — it is a skip trace. This engine
turns a stored lead plus whatever contact record exists into one of a handful
of concrete next actions, so the queue reads as a to-do list rather than a
leaderboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import List, Optional

from ..storage import StoredLead
from .models import Contact, Outcome
from .pipeline import (
    CONTRACTED_STATUSES,
    STATUS_ASSIGNED,
    STATUS_BUYER_SEARCH,
    STATUS_NEGOTIATING,
    STATUS_OFFER_PREPARING,
    STATUS_OFFER_SENT,
    STATUS_UNDER_CONTRACT,
    is_closed,
    normalize_status,
)


class NextAction(str, Enum):
    """The physical thing to do next."""

    CALL_NOW = "CALL NOW"
    CALL = "CALL"
    FOLLOW_UP = "FOLLOW UP"
    FOLLOW_UP_OVERDUE = "FOLLOW UP (OVERDUE)"
    SKIP_TRACE = "SKIP TRACE"
    MAIL = "SEND MAIL"
    RESEARCH_FIRST = "RESEARCH FIRST"
    PREPARE_OFFER = "PREPARE OFFER"
    AWAIT_OFFER_RESPONSE = "AWAIT OFFER RESPONSE"
    RESPOND_TO_COUNTER = "RESPOND TO COUNTER"
    CONTRACT_TASKS = "CONTRACT TASKS"
    FIND_BUYER = "FIND BUYER"
    NOTHING = "NO ACTION"

    def __str__(self) -> str:
        return self.value


#: Rough ordering: the lower the number, the sooner it should happen.
ACTION_URGENCY = {
    NextAction.RESPOND_TO_COUNTER: 0,
    NextAction.FOLLOW_UP_OVERDUE: 1,
    NextAction.CONTRACT_TASKS: 2,
    NextAction.CALL_NOW: 3,
    NextAction.FIND_BUYER: 4,
    NextAction.PREPARE_OFFER: 5,
    NextAction.CALL: 6,
    NextAction.FOLLOW_UP: 7,
    NextAction.SKIP_TRACE: 8,
    NextAction.AWAIT_OFFER_RESPONSE: 9,
    NextAction.RESEARCH_FIRST: 10,
    NextAction.MAIL: 11,
    NextAction.NOTHING: 99,
}

#: Weights for the acquisition priority score. Sums to 100.
ACQUISITION_WEIGHTS = {
    "priority_score": 30.0,
    "deal_score": 22.0,
    "lead_score": 14.0,
    "wholesale_fee": 12.0,
    "contact_availability": 12.0,
    "distress": 5.0,
    "equity": 5.0,
}

#: Deal score below which the deal itself needs work before the phone does.
RESEARCH_FIRST_DEAL_SCORE = 55.0

#: ARV confidence values that mean the number has not been corroborated.
UNVERIFIED_ARV = ("USER-PROVIDED ARV (UNVERIFIED)", "INSUFFICIENT DATA")


@dataclass
class ContactPriority:
    """One lead's place in the call queue."""

    property_id: str = ""
    score: float = 0.0
    action: NextAction = NextAction.NOTHING
    reason: str = ""
    blockers: List[str] = field(default_factory=list)
    days_overdue: Optional[int] = None

    @property
    def urgency(self) -> int:
        return ACTION_URGENCY.get(self.action, 50)

    @property
    def needs_skip_trace(self) -> bool:
        return self.action is NextAction.SKIP_TRACE

    @property
    def is_callable(self) -> bool:
        return self.action in (NextAction.CALL_NOW, NextAction.CALL)

    def sort_key(self) -> tuple:
        """Urgency first, then how overdue, then the score."""
        return (self.urgency, -(self.days_overdue or 0), -self.score)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class ContactPriorityEngine:
    """Turns a lead plus its contact record into a next action and a rank."""

    def __init__(
        self,
        weights: Optional[dict] = None,
        target_wholesale_fee: float = 18_000.0,
    ) -> None:
        self.weights = dict(weights or ACQUISITION_WEIGHTS)
        self.target_wholesale_fee = target_wholesale_fee

    # ------------------------------------------------------------------

    def score(
        self,
        row: StoredLead,
        contact: Optional[Contact] = None,
        has_open_counter: bool = False,
        today: Optional[date] = None,
    ) -> ContactPriority:
        """Rank one lead and decide what to do about it."""
        today = today or date.today()
        result = ContactPriority(property_id=row.dedupe_key)
        result.score = round(self._numeric_score(row, contact), 1)
        self._decide_action(result, row, contact, has_open_counter, today)
        return result

    # ------------------------------------------------------------------

    def _numeric_score(self, row: StoredLead, contact: Optional[Contact]) -> float:
        weights = self.weights
        total_weight = sum(weights.values())
        if not total_weight:
            return 0.0

        parts = 0.0
        parts += weights.get("priority_score", 0) * _clamp((row.priority_score or 35.0) / 100.0)
        parts += weights.get("deal_score", 0) * _clamp((row.deal_score or 35.0) / 100.0)
        parts += weights.get("lead_score", 0) * _clamp((row.lead_score or 35.0) / 100.0)

        # The fee is a target, not a gate: credit is proportional, and a
        # below-target fee still earns most of the way there.
        fee = row.potential_fee
        if fee is None:
            fee_share = 0.35
        elif fee <= 0:
            fee_share = 0.0
        else:
            fee_share = _clamp(fee / max(self.target_wholesale_fee * 1.6, 1.0))
        parts += weights.get("wholesale_fee", 0) * fee_share

        # Contact availability — the whole reason this differs from priority.
        if contact is None or not contact.is_reachable:
            reach = 0.0
        elif contact.has_phone and contact.verified:
            reach = 1.0
        elif contact.has_phone:
            reach = 0.85
        elif contact.has_email:
            reach = 0.5
        else:
            reach = 0.3
        parts += weights.get("contact_availability", 0) * reach

        parts += weights.get("distress", 0) * _clamp((row.distress_count or 0) / 4.0)
        equity_share = (
            _clamp((row.equity_percentage or 0.0) / 0.60)
            if row.equity_percentage is not None else 0.3
        )
        parts += weights.get("equity", 0) * equity_share

        return _clamp(parts / total_weight * 100.0, 0.0, 100.0)

    # ------------------------------------------------------------------

    def _decide_action(
        self,
        result: ContactPriority,
        row: StoredLead,
        contact: Optional[Contact],
        has_open_counter: bool,
        today: date,
    ) -> None:
        """Pick the single next physical action for this lead."""
        status = normalize_status(row.status)

        if is_closed(status):
            result.action = NextAction.NOTHING
            result.reason = f"{status} — nothing to do."
            return

        # Later stages of the pipeline outrank anything on the seller side.
        # An assigned deal has no seller action left, whatever the offer
        # history still says.
        if status == STATUS_ASSIGNED:
            result.action = NextAction.NOTHING
            result.reason = "Assigned — waiting on the closing."
            return
        if status == STATUS_UNDER_CONTRACT:
            result.action = NextAction.CONTRACT_TASKS
            result.reason = "Under contract — work inspection, title and the closing date."
            return
        if status == STATUS_BUYER_SEARCH:
            result.action = NextAction.FIND_BUYER
            result.reason = "Under contract with no buyer yet — work the buyer list."
            return
        if has_open_counter or status == STATUS_NEGOTIATING:
            result.action = NextAction.RESPOND_TO_COUNTER
            result.reason = "The seller has countered. Their number is on the table."
            return
        if status == STATUS_OFFER_SENT:
            result.action = NextAction.AWAIT_OFFER_RESPONSE
            result.reason = "Offer is with the seller."
            return
        if status == STATUS_OFFER_PREPARING:
            result.action = NextAction.PREPARE_OFFER
            result.reason = "Seller wants a number — put the offer together."
            return

        # Follow-ups outrank fresh calls: a promise made is a promise owed.
        if contact is not None and contact.next_follow_up is not None:
            days = (today - contact.next_follow_up).days
            if days > 0:
                result.action = NextAction.FOLLOW_UP_OVERDUE
                result.days_overdue = days
                result.reason = (
                    f"Follow-up was due {contact.next_follow_up.isoformat()} "
                    f"({days} day{'s' if days != 1 else ''} ago)"
                    + (f": {contact.follow_up_reason}" if contact.follow_up_reason else ".")
                )
                return
            if days == 0:
                result.action = NextAction.FOLLOW_UP
                result.days_overdue = 0
                result.reason = "Follow-up due today" + (
                    f": {contact.follow_up_reason}" if contact.follow_up_reason else "."
                )
                return

        # No contact route at all.
        if contact is None or not contact.is_reachable:
            if row.deal_score is not None and row.deal_score < RESEARCH_FIRST_DEAL_SCORE:
                result.action = NextAction.RESEARCH_FIRST
                result.reason = (
                    f"Deal score {row.deal_score:.0f} — not worth paying to skip trace "
                    "until the numbers hold up."
                )
                result.blockers.append("no contact information")
                return
            result.action = NextAction.SKIP_TRACE
            result.reason = "No phone, email or mailing address on file."
            result.blockers.append("no contact information")
            return

        # Reachable, but the underwriting is not there yet.
        if row.arv_confidence in UNVERIFIED_ARV:
            result.action = NextAction.RESEARCH_FIRST
            result.reason = (
                f"Contact is in hand, but the ARV is {row.arv_confidence}. "
                "Pull comps before you talk price."
            )
            result.blockers.append(f"ARV {row.arv_confidence}")
            return
        if row.deal_score is not None and row.deal_score < RESEARCH_FIRST_DEAL_SCORE:
            result.action = NextAction.RESEARCH_FIRST
            result.reason = (
                f"Contact is in hand, but the deal scores {row.deal_score:.0f}. "
                "Work out whether it is worth the call."
            )
            return

        if not contact.has_phone:
            if contact.has_email:
                result.action = NextAction.CALL
                result.reason = "No phone on file — email is the only route."
                result.blockers.append("no phone number")
                return
            result.action = NextAction.MAIL
            result.reason = "Mailing address only — no phone or email."
            result.blockers.append("no phone or email")
            return

        # A phone, a verified deal, and something worth saying.
        hot = (
            (row.priority_band or "").startswith("🔥")
            or (row.deal_score or 0) >= 75.0
            or normalize_status(row.status) == "HOT"
        )
        result.action = NextAction.CALL_NOW if hot else NextAction.CALL
        fee = f"${row.potential_fee:,.0f}" if row.potential_fee is not None else "unknown"
        result.reason = (
            f"Phone on file, deal scores {row.deal_score:.0f}"
            if row.deal_score is not None
            else "Phone on file"
        ) + f", potential fee {fee}."
        if contact.is_test_data:
            result.blockers.append(
                "contact is FICTIONAL TEST DATA from the mock provider — do not dial"
            )


DEFAULT_CONTACT_PRIORITY_ENGINE = ContactPriorityEngine()
