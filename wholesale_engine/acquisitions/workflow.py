"""The acquisition workflow: queues, follow-ups, offers, dashboard, daily list.

This is the layer the CLI talks to. It reads the lead store and the
acquisition store, applies the contact-priority rules, and answers the four
questions an acquisitions day is made of:

* who do I call (``contact_queue``)
* what did I promise (``follow_ups``)
* where does everything stand (``dashboard``)
* what do I do first (``daily_plan``)

It also owns offer construction, because an offer has to be measured against
the underwriting that produced it — and because the warning when an offer
exceeds MAO belongs next to the arithmetic, not in the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import DEFAULT_CONFIG, EngineConfig
from ..formatting import money
from ..storage import LeadStore, SearchQuery, StoredLead
from .contact_priority import (
    ContactPriority,
    ContactPriorityEngine,
    NextAction,
)
from .models import (
    Channel,
    Contact,
    Direction,
    Offer,
    OfferStatus,
    Outcome,
    OutreachActivity,
)
from .pipeline import (
    ACQUISITION_STATUSES,
    CLOSED_STATUSES,
    CONTRACTED_STATUSES,
    STATUS_ASSIGNED,
    STATUS_BUYER_SEARCH,
    STATUS_CLOSED,
    STATUS_CONTACT_READY,
    STATUS_CONTACTED,
    STATUS_DEAD,
    STATUS_FOLLOW_UP,
    STATUS_HOT,
    STATUS_NEGOTIATING,
    STATUS_NEW,
    STATUS_OFFER_SENT,
    STATUS_PASSED,
    STATUS_UNDER_CONTRACT,
    is_closed,
    normalize_status,
)
from .store import AcquisitionStore


# ---------------------------------------------------------------------------
# Queue rows
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    """One line of the contact queue."""

    row: StoredLead
    contact: Optional[Contact]
    priority: ContactPriority
    open_offer: Optional[Offer] = None

    @property
    def owner_name(self) -> str:
        if self.contact and self.contact.owner_name:
            return self.contact.owner_name
        return "unknown"

    @property
    def phone_status(self) -> str:
        return self.contact.phone_status if self.contact else "NONE"

    @property
    def email_status(self) -> str:
        return self.contact.email_status if self.contact else "NONE"


@dataclass
class FollowUp:
    """A promise you made, and whether it is late."""

    row: StoredLead
    contact: Contact
    due: date
    days: int  # negative = upcoming, 0 = today, positive = overdue

    @property
    def bucket(self) -> str:
        if self.days > 0:
            return "OVERDUE"
        if self.days == 0:
            return "TODAY"
        return "UPCOMING"

    @property
    def reason(self) -> str:
        return self.contact.follow_up_reason or self.contact.last_outcome or ""


@dataclass
class DailyItem:
    """One line of the daily plan."""

    action: str
    address: str
    property_id: str
    detail: str
    group: str


@dataclass
class Dashboard:
    """Pipeline counts and projected economics.

    Every dollar figure here is **projected**, not earned. A potential fee is
    what a deal would pay if it closed at the numbers currently on file, and
    most leads never get there.
    """

    counts: Dict[str, int] = field(default_factory=dict)
    follow_ups_today: int = 0
    follow_ups_overdue: int = 0
    offers_open: int = 0
    counters_waiting: int = 0
    contracts_live: int = 0
    assignments_live: int = 0
    contacts_on_file: int = 0
    contacts_needing_skip_trace: int = 0
    test_data_contacts: int = 0

    pipeline_value: float = 0.0
    potential_fees: float = 0.0
    contracted_fees: float = 0.0
    average_deal_score: Optional[float] = None
    average_lead_score: Optional[float] = None
    average_priority_score: Optional[float] = None
    total_leads: int = 0

    @property
    def active_total(self) -> int:
        return sum(
            count for status, count in self.counts.items()
            if status not in CLOSED_STATUSES and status != STATUS_NEW
        )


# ---------------------------------------------------------------------------


class AcquisitionWorkflow:
    """Everything the acquisition CLI commands need."""

    def __init__(
        self,
        leads: LeadStore,
        acquisitions: Optional[AcquisitionStore] = None,
        config: EngineConfig = DEFAULT_CONFIG,
        engine: Optional[ContactPriorityEngine] = None,
    ) -> None:
        self.leads = leads
        self.store = acquisitions or AcquisitionStore(leads)
        self.config = config
        self.engine = engine or ContactPriorityEngine(
            target_wholesale_fee=config.target_wholesale_fee
        )

    # ==================================================================
    # Status
    # ==================================================================

    def set_status(self, property_id: str, status: str, reason: str = "") -> Tuple[bool, str]:
        """Move a property along the pipeline. Returns (changed, message)."""
        row = self.leads.find_one(property_id)
        if row is None:
            return False, f"No stored property matches '{property_id}'."
        normalized = normalize_status(status)
        if normalized not in ACQUISITION_STATUSES:
            return False, (
                f"Unknown status '{status}'. Valid: {', '.join(ACQUISITION_STATUSES)}"
            )
        previous = row.status
        if previous == normalized:
            return False, f"Already {normalized}."
        self.leads.set_status(row.lead_row_id, normalized, reason)
        return True, f"{previous} -> {normalized}" + (f" ({reason})" if reason else "")

    # ==================================================================
    # Contact queue
    # ==================================================================

    def queue_entries(
        self,
        limit: Optional[int] = None,
        include_closed: bool = False,
        today: Optional[date] = None,
    ) -> List[QueueEntry]:
        """Every live lead, ordered by what needs doing first."""
        today = today or date.today()
        entries: List[QueueEntry] = []
        for row in self.leads.search(SearchQuery(exclude_closed=not include_closed)):
            if not include_closed and is_closed(row.status):
                continue
            contact = self.store.best_contact(row.dedupe_key)
            offers = self.store.offers_for(row.dedupe_key)
            open_offer = next((o for o in offers if o.is_open), None)
            has_counter = any(
                o.seller_counter is not None and o.offer_status is OfferStatus.COUNTERED
                for o in offers
            )
            priority = self.engine.score(row, contact, has_counter, today)
            entries.append(QueueEntry(row, contact, priority, open_offer))

        entries.sort(key=lambda e: e.priority.sort_key())
        return entries[:limit] if limit else entries

    def skip_trace_candidates(self, limit: Optional[int] = None) -> List[QueueEntry]:
        """Leads worth paying to trace: no contact route, but a real deal."""
        candidates = [
            entry for entry in self.queue_entries()
            if entry.priority.needs_skip_trace
        ]
        return candidates[:limit] if limit else candidates

    # ==================================================================
    # Follow-ups
    # ==================================================================

    def follow_ups(self, today: Optional[date] = None) -> List[FollowUp]:
        """Every scheduled follow-up, most overdue first."""
        today = today or date.today()
        result: List[FollowUp] = []
        for contact in self.store.all_contacts():
            if contact.next_follow_up is None:
                continue
            row = self.leads.find_one(contact.property_id)
            if row is None or is_closed(row.status):
                continue
            result.append(
                FollowUp(
                    row=row,
                    contact=contact,
                    due=contact.next_follow_up,
                    days=(today - contact.next_follow_up).days,
                )
            )
        result.sort(key=lambda f: (-f.days, f.due))
        return result

    def follow_ups_by_bucket(
        self, today: Optional[date] = None
    ) -> Dict[str, List[FollowUp]]:
        buckets: Dict[str, List[FollowUp]] = {"OVERDUE": [], "TODAY": [], "UPCOMING": []}
        for follow_up in self.follow_ups(today):
            buckets[follow_up.bucket].append(follow_up)
        return buckets

    # ==================================================================
    # Outreach
    # ==================================================================

    def log_outreach(
        self,
        property_id: str,
        channel: Channel,
        outcome: Optional[Outcome] = None,
        notes: str = "",
        follow_up: Optional[date] = None,
        direction: Direction = Direction.OUTBOUND,
        advance_status: bool = True,
    ) -> Tuple[Optional[OutreachActivity], List[str]]:
        """Record an attempt. Returns the activity and any messages to print.

        A phone number is deliberately **not** required: you can log a call you
        made from a number you found elsewhere, and the log is still the record
        of what happened.
        """
        messages: List[str] = []
        row = self.leads.find_one(property_id)
        if row is None:
            return None, [f"No stored property matches '{property_id}'."]

        contact = self.store.best_contact(row.dedupe_key)
        if contact is None:
            # Create a bare contact so the attempt has something to hang off.
            contact = self.store.save_contact(
                Contact(
                    property_id=row.dedupe_key,
                    source="manual",
                    notes="Created to record an outreach attempt. No contact "
                          "details are known.",
                )
            )
            messages.append(
                "No contact record existed; created an empty one to hold the log. "
                "No phone number or email has been invented."
            )

        activity = self.store.log_outreach(
            OutreachActivity(
                property_id=row.dedupe_key,
                contact_id=contact.contact_id,
                channel=channel,
                direction=direction,
                outcome=outcome,
                notes=notes,
                next_follow_up=follow_up,
            )
        )

        if follow_up:
            messages.append(f"Follow-up set for {follow_up.isoformat()}.")
        elif activity.expects_follow_up:
            messages.append(
                f"Outcome {outcome} usually needs a follow-up, and none was set. "
                "Pass --follow-up YYYY-MM-DD so this does not fall through."
            )

        if advance_status:
            suggested = activity.suggested_status()
            if suggested and normalize_status(row.status) != suggested:
                changed, message = self.set_status(
                    row.dedupe_key, suggested, f"outreach outcome {outcome}"
                )
                if changed:
                    messages.append(f"Status {message}")
        return activity, messages

    # ==================================================================
    # Offers
    # ==================================================================

    def build_offer(
        self,
        property_id: str,
        amount: float,
        notes: str = "",
        status: OfferStatus = OfferStatus.SENT,
        when: Optional[date] = None,
    ) -> Tuple[Optional[Offer], List[str]]:
        """Construct and store an offer, measured against the underwriting.

        The offer is **never blocked**. If it exceeds MAO, or leaves less than
        the target fee, that is said plainly and recorded on the offer — the
        decision stays yours.
        """
        row = self.leads.find_one(property_id)
        if row is None:
            return None, [f"No stored property matches '{property_id}'."]

        ceiling = None
        if row.arv is not None and row.repair_estimate is not None:
            ceiling = row.arv * self.config.arv_percentage - row.repair_estimate

        offer = Offer(
            property_id=row.dedupe_key,
            offer_amount=amount,
            offer_date=when or date.today(),
            current_price=row.asking_price,
            mao=row.mao,
            arv=row.arv,
            repairs=row.repair_estimate,
            end_buyer_ceiling=ceiling,
            target_wholesale_fee=self.config.target_wholesale_fee,
            potential_wholesale_fee=(None if ceiling is None else ceiling - amount),
            offer_status=status,
            notes=notes,
        )

        warnings: List[str] = []
        if row.mao is None:
            warnings.append(
                "No MAO on file for this property — the offer has not been checked "
                "against any underwriting. Run --hunt first."
            )
        elif amount > row.mao:
            over = amount - row.mao
            warnings.append(
                f"OFFER EXCEEDS MAO: {money(amount)} is {money(over)} above the MAO "
                f"of {money(row.mao)}. At that price the deal leaves "
                f"{money(offer.potential_wholesale_fee)} of assignment fee. Recorded "
                "as entered — this is a warning, not a block."
            )
        if row.asking_price is not None and amount > row.asking_price:
            warnings.append(
                f"Offer is above the {money(row.asking_price)} asking price."
            )
        fee = offer.potential_wholesale_fee
        if fee is not None:
            if fee <= 0:
                warnings.append(
                    f"At {money(amount)} there is no assignment fee left "
                    f"({money(fee)}). No end buyer following the same rule can take it."
                )
            elif fee < self.config.target_wholesale_fee:
                short = self.config.target_wholesale_fee - fee
                warnings.append(
                    f"BELOW TARGET WHOLESALE FEE: {money(fee)} against a "
                    f"{money(self.config.target_wholesale_fee)} target, short by "
                    f"{money(short)}. A label, not a rejection — a deal below target "
                    "can still be worth doing."
                )
        if row.arv_confidence in ("USER-PROVIDED ARV (UNVERIFIED)", "INSUFFICIENT DATA"):
            warnings.append(
                f"The ARV behind these numbers is {row.arv_confidence}. Everything "
                "above rests on it."
            )

        offer.warnings = warnings
        self.store.save_offer(offer)

        if status is OfferStatus.SENT:
            self.set_status(row.dedupe_key, STATUS_OFFER_SENT, f"offer {money(amount)}")
        return offer, warnings

    def record_counter(
        self, property_id: str, amount: float, notes: str = ""
    ) -> Tuple[Optional[Offer], List[str]]:
        """Log a seller counter and move the deal to NEGOTIATING."""
        row = self.leads.find_one(property_id)
        if row is None:
            return None, [f"No stored property matches '{property_id}'."]
        offer = self.store.record_counter(row.dedupe_key, amount, notes=notes)
        if offer is None:
            return None, [
                "No offer on file to counter. Record the offer first with --make-offer."
            ]
        self.set_status(row.dedupe_key, STATUS_NEGOTIATING, f"counter {money(amount)}")

        messages = [f"Seller counter recorded at {money(amount)}."]
        distance = offer.distance_to_mao
        if distance is not None:
            if distance < 0:
                messages.append(
                    f"That is {money(abs(distance))} ABOVE your MAO of {money(offer.mao)}."
                )
            else:
                messages.append(
                    f"Still {money(distance)} below your MAO of {money(offer.mao)}."
                )
        fee = offer.fee_at_current_price
        if fee is not None:
            gap = offer.distance_to_target_fee
            messages.append(
                f"At their number the deal supports {money(fee)} of fee"
                + (
                    f", {money(abs(gap))} {'below' if gap < 0 else 'above'} your "
                    f"{money(offer.target_wholesale_fee)} target."
                    if gap is not None else "."
                )
            )
        return offer, messages

    def open_counters(self) -> List[Tuple[StoredLead, Offer]]:
        """Offers the seller has countered and you have not answered."""
        result: List[Tuple[StoredLead, Offer]] = []
        for offer in self.store.all_offers():
            if offer.offer_status is not OfferStatus.COUNTERED:
                continue
            row = self.leads.find_one(offer.property_id)
            if row is not None and not is_closed(row.status):
                result.append((row, offer))
        return result

    # ==================================================================
    # Dashboard
    # ==================================================================

    def dashboard(self, today: Optional[date] = None) -> Dashboard:
        """Pipeline counts and projected — not earned — economics."""
        today = today or date.today()
        board = Dashboard()
        board.counts = {status: 0 for status in ACQUISITION_STATUSES}

        rows = self.leads.search()
        board.total_leads = len(rows)

        deal_scores: List[float] = []
        lead_scores: List[float] = []
        priority_scores: List[float] = []

        for row in rows:
            status = normalize_status(row.status)
            if status in board.counts:
                board.counts[status] += 1
            if row.lead_score is not None:
                lead_scores.append(row.lead_score)
            if row.deal_score is not None:
                deal_scores.append(row.deal_score)
            if row.priority_score is not None:
                priority_scores.append(row.priority_score)

            if is_closed(status):
                continue
            # Pipeline value is what you would be paying, not what it is worth.
            if row.recommended_offer is not None:
                board.pipeline_value += row.recommended_offer
            if row.potential_fee is not None and row.potential_fee > 0:
                board.potential_fees += row.potential_fee
                if status in CONTRACTED_STATUSES:
                    board.contracted_fees += row.potential_fee

        buckets = self.follow_ups_by_bucket(today)
        board.follow_ups_today = len(buckets["TODAY"])
        board.follow_ups_overdue = len(buckets["OVERDUE"])

        contacts = self.store.all_contacts()
        board.contacts_on_file = sum(1 for c in contacts if c.is_reachable)
        board.test_data_contacts = sum(1 for c in contacts if c.is_test_data)
        board.contacts_needing_skip_trace = len(self.skip_trace_candidates())

        board.offers_open = len(self.store.all_offers(open_only=True))
        board.counters_waiting = len(self.open_counters())
        board.contracts_live = len(self.store.all_contracts(live_only=True))
        board.assignments_live = len(self.store.all_assignments(live_only=True))

        def average(values: Sequence[float]) -> Optional[float]:
            return round(sum(values) / len(values), 1) if values else None

        board.average_deal_score = average(deal_scores)
        board.average_lead_score = average(lead_scores)
        board.average_priority_score = average(priority_scores)
        return board

    # ==================================================================
    # Daily plan
    # ==================================================================

    def daily_plan(
        self, today: Optional[date] = None, limit_per_group: int = 10
    ) -> List[DailyItem]:
        """What needs attention, in the order the spec asks for.

        1. Overdue follow-ups
        2. Hot leads with contact information
        3. Hot leads needing a skip trace
        4. New high-score leads
        5. Seller counters
        6. Offers needing action
        7. Under-contract tasks
        """
        today = today or date.today()
        entries = self.queue_entries(today=today)
        by_id = {entry.row.dedupe_key: entry for entry in entries}
        plan: List[DailyItem] = []
        seen: set = set()

        def add(action: str, entry: QueueEntry, detail: str, group: str) -> None:
            key = entry.row.dedupe_key
            if key in seen:
                return
            seen.add(key)
            plan.append(
                DailyItem(
                    action=action,
                    address=entry.row.address or key,
                    property_id=entry.row.dedupe_key,
                    detail=detail,
                    group=group,
                )
            )

        # 5 and 7 first inside their own groups, but the spec's ordering puts
        # overdue follow-ups at the top of the day. Groups are emitted in the
        # documented order below.
        counters = {row.dedupe_key: offer for row, offer in self.open_counters()}

        # 1. Overdue follow-ups
        for follow_up in self.follow_ups_by_bucket(today)["OVERDUE"][:limit_per_group]:
            entry = by_id.get(follow_up.row.dedupe_key)
            if entry is None:
                continue
            add(
                "FOLLOW UP", entry,
                f"{follow_up.days} day(s) overdue"
                + (f" — {follow_up.reason}" if follow_up.reason else ""),
                "1. OVERDUE FOLLOW-UPS",
            )

        # 2. Hot leads you can actually call
        for entry in entries:
            if len([p for p in plan if p.group.startswith("2.")]) >= limit_per_group:
                break
            if entry.priority.is_callable:
                add("CALL", entry, entry.priority.reason, "2. HOT LEADS WITH CONTACT")

        # 3. Hot leads with no contact route
        for entry in entries:
            if len([p for p in plan if p.group.startswith("3.")]) >= limit_per_group:
                break
            if entry.priority.needs_skip_trace:
                add("SKIP TRACE", entry, entry.priority.reason, "3. NEEDS SKIP TRACE")

        # 4. New high-score leads that have not been worked at all
        for entry in entries:
            if len([p for p in plan if p.group.startswith("4.")]) >= limit_per_group:
                break
            if normalize_status(entry.row.status) != STATUS_NEW:
                continue
            if (entry.row.deal_score or 0) < 60 and (entry.row.lead_score or 0) < 70:
                continue
            add(
                "REVIEW", entry,
                f"new lead, deal {entry.row.deal_score or 0:.0f} / "
                f"lead {entry.row.lead_score or 0:.0f}",
                "4. NEW HIGH-SCORE LEADS",
            )

        # 5. Seller counters
        for key, offer in counters.items():
            entry = by_id.get(key)
            if entry is None:
                continue
            fee = offer.fee_at_current_price
            add(
                "RESPOND TO COUNTER", entry,
                f"countered at {money(offer.seller_counter)}"
                + (f", fee would be {money(fee)}" if fee is not None else ""),
                "5. SELLER COUNTERS",
            )

        # 6. Offers awaiting a response
        for entry in entries:
            if entry.priority.action in (
                NextAction.AWAIT_OFFER_RESPONSE, NextAction.PREPARE_OFFER
            ):
                add(str(entry.priority.action), entry, entry.priority.reason, "6. OFFERS")

        # 7. Under contract
        for entry in entries:
            if entry.priority.action in (NextAction.CONTRACT_TASKS, NextAction.FIND_BUYER):
                add(
                    str(entry.priority.action), entry, entry.priority.reason,
                    "7. UNDER CONTRACT",
                )

        plan.sort(key=lambda item: item.group)
        return plan
