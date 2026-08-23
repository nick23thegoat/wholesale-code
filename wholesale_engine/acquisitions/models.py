"""Acquisition-side records: contacts, outreach, offers, contracts, buyers.

Everything a deal accumulates once it stops being a row in a lead list and
starts being a conversation with a person.

One rule runs through all of it: **contact information is never invented.**
A phone number the engine does not have is ``None``, and no code path anywhere
in this package generates, formats, or guesses one. The same goes for emails
and mailing addresses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from ..research.facts import Confidence

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class PhoneType(str, Enum):
    MOBILE = "MOBILE"
    LANDLINE = "LANDLINE"
    VOIP = "VOIP"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "PhoneType":
        text = str(raw or "").strip().upper()
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


class Channel(str, Enum):
    """How an outreach attempt was made."""

    CALL = "CALL"
    TEXT = "TEXT"
    EMAIL = "EMAIL"
    VOICEMAIL = "VOICEMAIL"
    MAIL = "MAIL"
    OTHER = "OTHER"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "Channel":
        text = str(raw or "").strip().upper()
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"unknown channel '{raw}'. Valid: {', '.join(m.value for m in cls)}"
        )


class Direction(str, Enum):
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"

    def __str__(self) -> str:
        return self.value


class Outcome(str, Enum):
    """What came of an outreach attempt."""

    NO_ANSWER = "NO_ANSWER"
    LEFT_VOICEMAIL = "LEFT_VOICEMAIL"
    CONNECTED = "CONNECTED"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    CALL_BACK = "CALL_BACK"
    WANTS_OFFER = "WANTS_OFFER"
    OFFER_SENT = "OFFER_SENT"
    NEGOTIATING = "NEGOTIATING"
    DEAD = "DEAD"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "Outcome":
        text = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"unknown outcome '{raw}'. Valid: {', '.join(m.value for m in cls)}"
        )


#: Outcomes that mean the seller conversation is over.
DEAD_OUTCOMES = (Outcome.NOT_INTERESTED, Outcome.DEAD)

#: Outcomes that mean a follow-up is expected. Logging one of these without a
#: date is what puts a lead on the "no follow-up scheduled" list.
FOLLOW_UP_OUTCOMES = (
    Outcome.NO_ANSWER,
    Outcome.LEFT_VOICEMAIL,
    Outcome.CALL_BACK,
    Outcome.INTERESTED,
    Outcome.WANTS_OFFER,
    Outcome.NEGOTIATING,
)

#: Outcome -> the pipeline status it implies. Applied as a suggestion, never
#: silently: the CLI reports the move it made.
OUTCOME_STATUS: Dict[str, str] = {
    Outcome.CONNECTED.value: "CONVERSATION",
    Outcome.INTERESTED.value: "CONVERSATION",
    Outcome.CALL_BACK.value: "FOLLOW_UP",
    Outcome.NO_ANSWER.value: "CONTACTED",
    Outcome.LEFT_VOICEMAIL.value: "CONTACTED",
    Outcome.WANTS_OFFER.value: "OFFER_PREPARING",
    Outcome.OFFER_SENT.value: "OFFER_SENT",
    Outcome.NEGOTIATING.value: "NEGOTIATING",
    Outcome.NOT_INTERESTED.value: "DEAD",
    Outcome.DEAD.value: "DEAD",
}


class OfferStatus(str, Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    COUNTERED = "COUNTERED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "OfferStatus":
        text = str(raw or "").strip().upper()
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"unknown offer status '{raw}'. Valid: {', '.join(m.value for m in cls)}"
        )


#: Offer statuses that are still live.
OPEN_OFFER_STATUSES = (OfferStatus.DRAFT, OfferStatus.SENT, OfferStatus.COUNTERED)


class ContractStatus(str, Enum):
    PENDING = "PENDING"
    INSPECTION = "INSPECTION"
    CLEAR_TO_CLOSE = "CLEAR_TO_CLOSE"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "ContractStatus":
        text = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"unknown contract status '{raw}'. Valid: {', '.join(m.value for m in cls)}"
        )


class AssignmentStatus(str, Enum):
    BUYER_SEARCH = "BUYER_SEARCH"
    BUYER_INTERESTED = "BUYER_INTERESTED"
    BUYER_OFFER = "BUYER_OFFER"
    ASSIGNMENT_SIGNED = "ASSIGNMENT_SIGNED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: Any) -> "AssignmentStatus":
        text = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"unknown assignment status '{raw}'. "
            f"Valid: {', '.join(m.value for m in cls)}"
        )


# ---------------------------------------------------------------------------
# Provenance labels — required on anything the engine did not observe directly
# ---------------------------------------------------------------------------

PROVENANCE_SOURCE = "SOURCE-PROVIDED"
PROVENANCE_CALCULATED = "CALCULATED"
PROVENANCE_USER = "USER-PROVIDED"
PROVENANCE_UNVERIFIED = "UNVERIFIED"
PROVENANCE_UNKNOWN = "UNKNOWN"

PROVENANCE_LABELS = (
    PROVENANCE_SOURCE,
    PROVENANCE_CALCULATED,
    PROVENANCE_USER,
    PROVENANCE_UNVERIFIED,
    PROVENANCE_UNKNOWN,
)


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Strip a phone number to digits, or return None.

    Returns ``None`` for anything that is not a plausible US number. This
    never *builds* a number — it only tidies one that was supplied.
    """
    if not raw:
        return None
    digits = _DIGITS.sub("", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def format_phone(raw: Optional[str]) -> Optional[str]:
    digits = normalize_phone(raw)
    if digits is None:
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip().lower()
    return text if "@" in text and "." in text.split("@")[-1] else None


@dataclass
class Contact:
    """How to reach the owner of one property — when it is actually known.

    Every contact field defaults to ``None``. There is no code path in this
    package that fills one in without a named source, and a mock source is
    labelled as fictional in the record itself.
    """

    contact_id: Optional[int] = None
    property_id: str = ""
    owner_name: Optional[str] = None

    phone: Optional[str] = None
    phone_type: PhoneType = PhoneType.UNKNOWN
    phone_confidence: Confidence = Confidence.UNKNOWN

    email: Optional[str] = None
    email_confidence: Confidence = Confidence.UNKNOWN

    mailing_address: Optional[str] = None

    source: str = ""
    source_date: Optional[date] = None
    verified: bool = False
    is_test_data: bool = False
    notes: str = ""

    # --- follow-up state -------------------------------------------------
    next_follow_up: Optional[date] = None
    follow_up_reason: str = ""
    last_contacted: Optional[datetime] = None
    contact_attempts: int = 0
    last_outcome: Optional[str] = None

    def __post_init__(self) -> None:
        self.phone = normalize_phone(self.phone)
        self.email = normalize_email(self.email)
        if self.phone is None:
            self.phone_type = PhoneType.UNKNOWN
            self.phone_confidence = Confidence.UNKNOWN
        if self.email is None:
            self.email_confidence = Confidence.UNKNOWN

    # -- reading ---------------------------------------------------------

    @property
    def has_phone(self) -> bool:
        return bool(self.phone)

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    @property
    def has_mailing_address(self) -> bool:
        return bool(self.mailing_address)

    @property
    def is_reachable(self) -> bool:
        """Any route to the owner at all, including direct mail."""
        return self.has_phone or self.has_email or self.has_mailing_address

    @property
    def phone_status(self) -> str:
        if not self.has_phone:
            return "NONE"
        if self.is_test_data:
            return "TEST DATA"
        return f"{self.phone_type} ({self.phone_confidence})"

    @property
    def email_status(self) -> str:
        if not self.has_email:
            return "NONE"
        if self.is_test_data:
            return "TEST DATA"
        return str(self.email_confidence)

    @property
    def provenance(self) -> str:
        if not self.is_reachable:
            return PROVENANCE_UNKNOWN
        if self.is_test_data:
            return PROVENANCE_UNVERIFIED
        if self.verified:
            return PROVENANCE_SOURCE
        return PROVENANCE_UNVERIFIED

    def display_phone(self) -> str:
        return format_phone(self.phone) or "—"

    def display_email(self) -> str:
        return self.email or "—"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "property_id": self.property_id,
            "owner_name": self.owner_name,
            "phone": self.display_phone() if self.has_phone else None,
            "phone_type": str(self.phone_type),
            "phone_confidence": str(self.phone_confidence),
            "email": self.email,
            "email_confidence": str(self.email_confidence),
            "mailing_address": self.mailing_address,
            "source": self.source,
            "source_date": self.source_date.isoformat() if self.source_date else None,
            "verified": self.verified,
            "is_test_data": self.is_test_data,
            "provenance": self.provenance,
            "next_follow_up": (
                self.next_follow_up.isoformat() if self.next_follow_up else None
            ),
            "follow_up_reason": self.follow_up_reason,
            "last_contacted": (
                self.last_contacted.isoformat(timespec="seconds")
                if self.last_contacted else None
            ),
            "contact_attempts": self.contact_attempts,
            "last_outcome": self.last_outcome,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Outreach
# ---------------------------------------------------------------------------


@dataclass
class OutreachActivity:
    """One logged attempt to reach a seller. Nothing is sent by this engine."""

    activity_id: Optional[int] = None
    property_id: str = ""
    contact_id: Optional[int] = None
    timestamp: Optional[datetime] = None
    channel: Channel = Channel.OTHER
    direction: Direction = Direction.OUTBOUND
    outcome: Optional[Outcome] = None
    notes: str = ""
    next_follow_up: Optional[date] = None

    @property
    def implies_dead(self) -> bool:
        return self.outcome in DEAD_OUTCOMES

    @property
    def expects_follow_up(self) -> bool:
        return self.outcome in FOLLOW_UP_OUTCOMES

    def suggested_status(self) -> Optional[str]:
        return OUTCOME_STATUS.get(self.outcome.value) if self.outcome else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "property_id": self.property_id,
            "contact_id": self.contact_id,
            "timestamp": (
                self.timestamp.isoformat(timespec="seconds") if self.timestamp else None
            ),
            "channel": str(self.channel),
            "direction": str(self.direction),
            "outcome": str(self.outcome) if self.outcome else None,
            "notes": self.notes,
            "next_follow_up": (
                self.next_follow_up.isoformat() if self.next_follow_up else None
            ),
        }


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------


@dataclass
class Offer:
    """An offer, with the underwriting it was measured against.

    The economics are stored alongside the number so the record still means
    something when the ARV is revised later — an offer judged against a
    $265,000 ARV should not silently re-grade itself.
    """

    offer_id: Optional[int] = None
    property_id: str = ""
    offer_amount: Optional[float] = None
    offer_date: Optional[date] = None
    seller_counter: Optional[float] = None
    counter_date: Optional[date] = None
    current_price: Optional[float] = None
    mao: Optional[float] = None
    arv: Optional[float] = None
    repairs: Optional[float] = None
    target_wholesale_fee: Optional[float] = None
    potential_wholesale_fee: Optional[float] = None
    end_buyer_ceiling: Optional[float] = None
    offer_status: OfferStatus = OfferStatus.DRAFT
    notes: str = ""
    warnings: List[str] = field(default_factory=list)

    # -- negotiation ------------------------------------------------------

    @property
    def current_proposed_price(self) -> Optional[float]:
        """The number actually on the table: the seller's counter, else ours."""
        return self.seller_counter if self.seller_counter is not None else self.offer_amount

    @property
    def distance_to_mao(self) -> Optional[float]:
        """MAO minus the price on the table. Negative means it is above MAO."""
        price = self.current_proposed_price
        if price is None or self.mao is None:
            return None
        return self.mao - price

    @property
    def exceeds_mao(self) -> bool:
        distance = self.distance_to_mao
        return distance is not None and distance < 0

    @property
    def fee_at_current_price(self) -> Optional[float]:
        """The assignment fee the deal supports at the price on the table."""
        price = self.current_proposed_price
        if price is None or self.end_buyer_ceiling is None:
            return None
        return self.end_buyer_ceiling - price

    @property
    def distance_to_target_fee(self) -> Optional[float]:
        """How far the achievable fee sits from the target. Negative = short.

        A negative number is information, not a verdict. The target is a
        target, and a deal that clears the deal score can be worth doing at a
        fee below it.
        """
        fee = self.fee_at_current_price
        if fee is None or self.target_wholesale_fee is None:
            return None
        return fee - self.target_wholesale_fee

    @property
    def is_open(self) -> bool:
        return self.offer_status in OPEN_OFFER_STATUSES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "property_id": self.property_id,
            "offer_amount": self.offer_amount,
            "offer_date": self.offer_date.isoformat() if self.offer_date else None,
            "seller_counter": self.seller_counter,
            "counter_date": self.counter_date.isoformat() if self.counter_date else None,
            "current_price": self.current_price,
            "current_proposed_price": self.current_proposed_price,
            "mao": self.mao,
            "arv": self.arv,
            "repairs": self.repairs,
            "end_buyer_ceiling": self.end_buyer_ceiling,
            "target_wholesale_fee": self.target_wholesale_fee,
            "potential_wholesale_fee": self.potential_wholesale_fee,
            "fee_at_current_price": self.fee_at_current_price,
            "distance_to_mao": self.distance_to_mao,
            "distance_to_target_fee": self.distance_to_target_fee,
            "offer_status": str(self.offer_status),
            "notes": self.notes,
            "warnings": " | ".join(self.warnings),
        }


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@dataclass
class Contract:
    """Contract tracking. Not a legal document and not legal advice.

    This records dates and numbers so nothing gets missed. It does not draft,
    review, or interpret anything — use a real attorney and a real title
    company for that.
    """

    contract_id: Optional[int] = None
    property_id: str = ""
    contract_date: Optional[date] = None
    purchase_price: Optional[float] = None
    inspection_deadline: Optional[date] = None
    closing_date: Optional[date] = None
    earnest_money: Optional[float] = None
    assignment_allowed: Optional[bool] = None
    seller: str = ""
    buyer: str = ""
    notes: str = ""
    status: ContractStatus = ContractStatus.PENDING

    def days_to(self, target: Optional[date], today: Optional[date] = None) -> Optional[int]:
        if target is None:
            return None
        return (target - (today or date.today())).days

    def inspection_days_left(self, today: Optional[date] = None) -> Optional[int]:
        return self.days_to(self.inspection_deadline, today)

    def closing_days_left(self, today: Optional[date] = None) -> Optional[int]:
        return self.days_to(self.closing_date, today)

    @property
    def is_live(self) -> bool:
        return self.status in (
            ContractStatus.PENDING,
            ContractStatus.INSPECTION,
            ContractStatus.CLEAR_TO_CLOSE,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "property_id": self.property_id,
            "contract_date": self.contract_date.isoformat() if self.contract_date else None,
            "purchase_price": self.purchase_price,
            "inspection_deadline": (
                self.inspection_deadline.isoformat() if self.inspection_deadline else None
            ),
            "closing_date": self.closing_date.isoformat() if self.closing_date else None,
            "earnest_money": self.earnest_money,
            "assignment_allowed": self.assignment_allowed,
            "seller": self.seller,
            "buyer": self.buyer,
            "status": str(self.status),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Buyers and assignments
# ---------------------------------------------------------------------------


@dataclass
class Buyer:
    """An end buyer's buy box. Contact details here are ones you entered."""

    buyer_id: Optional[int] = None
    name: str = ""
    company: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    market: str = ""
    property_types: List[str] = field(default_factory=list)
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    preferred_states: List[str] = field(default_factory=list)
    notes: str = ""
    is_test_data: bool = False

    def __post_init__(self) -> None:
        self.phone = normalize_phone(self.phone)
        self.email = normalize_email(self.email)
        self.preferred_states = [s.strip().upper() for s in self.preferred_states if s.strip()]
        self.property_types = [t.strip().lower() for t in self.property_types if t.strip()]

    def matches(
        self,
        state: str = "",
        property_type: str = "",
        price: Optional[float] = None,
    ) -> bool:
        """Does this property fit the buy box?

        An unspecified preference matches everything, and an unknown property
        attribute never rules a buyer out — the point is to shortlist people
        to call, not to filter them away on a blank field.
        """
        if self.preferred_states and state:
            if state.strip().upper() not in self.preferred_states:
                return False
        if self.property_types and property_type:
            if property_type.strip().lower() not in self.property_types:
                return False
        if price is not None:
            if self.min_price is not None and price < self.min_price:
                return False
            if self.max_price is not None and price > self.max_price:
                return False
        return True

    def price_range(self) -> str:
        if self.min_price is None and self.max_price is None:
            return "any"
        low = f"${self.min_price:,.0f}" if self.min_price is not None else "any"
        high = f"${self.max_price:,.0f}" if self.max_price is not None else "any"
        return f"{low}-{high}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "buyer_id": self.buyer_id,
            "name": self.name,
            "company": self.company,
            "email": self.email,
            "phone": format_phone(self.phone),
            "market": self.market,
            "property_types": ", ".join(self.property_types),
            "min_price": self.min_price,
            "max_price": self.max_price,
            "price_range": self.price_range(),
            "preferred_states": ", ".join(self.preferred_states),
            "is_test_data": self.is_test_data,
            "notes": self.notes,
        }


@dataclass
class Assignment:
    """Assigning a contract to an end buyer, and what it pays."""

    assignment_id: Optional[int] = None
    property_id: str = ""
    buyer_id: Optional[int] = None
    buyer_name: str = ""
    purchase_price: Optional[float] = None
    assignment_price: Optional[float] = None
    assignment_date: Optional[date] = None
    status: AssignmentStatus = AssignmentStatus.BUYER_SEARCH
    notes: str = ""

    @property
    def gross_assignment_fee(self) -> Optional[float]:
        """Assignment price minus what you are paying. The actual payday."""
        if self.assignment_price is None or self.purchase_price is None:
            return None
        return self.assignment_price - self.purchase_price

    @property
    def is_live(self) -> bool:
        return self.status not in (AssignmentStatus.CLOSED, AssignmentStatus.FAILED)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "property_id": self.property_id,
            "buyer_id": self.buyer_id,
            "buyer_name": self.buyer_name,
            "purchase_price": self.purchase_price,
            "assignment_price": self.assignment_price,
            "gross_assignment_fee": self.gross_assignment_fee,
            "assignment_date": (
                self.assignment_date.isoformat() if self.assignment_date else None
            ),
            "status": str(self.status),
            "notes": self.notes,
        }
