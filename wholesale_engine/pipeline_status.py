"""The acquisition pipeline: the statuses a deal actually moves through.

Lives at the top of the package, not inside :mod:`wholesale_engine.acquisitions`,
because the store writes these statuses and the acquisitions layer reads them
back — putting the vocabulary in either one would make them import each other.
It has no dependencies of its own, which is what lets both sides share it.

Wave 4 tracked a short watchlist. Wave 5 replaces it with the full
acquisitions path, from a lead you have never looked at to a closed
assignment::

    NEW -> RESEARCHING -> HOT -> CONTACT_READY -> CONTACTED -> CONVERSATION
        -> FOLLOW_UP -> OFFER_PREPARING -> OFFER_SENT -> NEGOTIATING
        -> UNDER_CONTRACT -> BUYER_SEARCH -> ASSIGNED -> CLOSED
                                          -> DEAD / PASSED at any point

Nothing enforces the order. Deals skip steps, go backwards, and die at every
stage; the pipeline records where each one is and how it got there, it does
not police the sequence.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# --- the pipeline, in order -------------------------------------------------

STATUS_NEW = "NEW"
STATUS_RESEARCHING = "RESEARCHING"
STATUS_HOT = "HOT"
STATUS_CONTACT_READY = "CONTACT_READY"
STATUS_CONTACTED = "CONTACTED"
STATUS_CONVERSATION = "CONVERSATION"
STATUS_FOLLOW_UP = "FOLLOW_UP"
STATUS_OFFER_PREPARING = "OFFER_PREPARING"
STATUS_OFFER_SENT = "OFFER_SENT"
STATUS_NEGOTIATING = "NEGOTIATING"
STATUS_UNDER_CONTRACT = "UNDER_CONTRACT"
STATUS_BUYER_SEARCH = "BUYER_SEARCH"
STATUS_ASSIGNED = "ASSIGNED"
STATUS_CLOSED = "CLOSED"
STATUS_DEAD = "DEAD"
STATUS_PASSED = "PASSED"

#: Every status, in pipeline order. Position matters: it is how the dashboard
#: and the watchlist sort, and how "furthest along" is decided.
ACQUISITION_STATUSES: Tuple[str, ...] = (
    STATUS_NEW,
    STATUS_RESEARCHING,
    STATUS_HOT,
    STATUS_CONTACT_READY,
    STATUS_CONTACTED,
    STATUS_CONVERSATION,
    STATUS_FOLLOW_UP,
    STATUS_OFFER_PREPARING,
    STATUS_OFFER_SENT,
    STATUS_NEGOTIATING,
    STATUS_UNDER_CONTRACT,
    STATUS_BUYER_SEARCH,
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_DEAD,
    STATUS_PASSED,
)

#: Wave 4 status names, mapped onto the Wave 5 pipeline. Applied on write so
#: an existing database keeps working instead of raising on its own history.
LEGACY_STATUS_ALIASES: Dict[str, str] = {
    "WATCH": STATUS_RESEARCHING,
    "RESEARCHED": STATUS_RESEARCHING,
    "CONTACT": STATUS_CONTACTED,
}

#: The deal is over, one way or the other.
CLOSED_STATUSES: Tuple[str, ...] = (STATUS_CLOSED, STATUS_DEAD, STATUS_PASSED)

#: The deal is live and someone should be doing something about it.
ACTIVE_STATUSES: Tuple[str, ...] = tuple(
    s for s in ACQUISITION_STATUSES
    if s not in CLOSED_STATUSES and s != STATUS_NEW
)

#: Statuses where the seller conversation has started.
IN_CONVERSATION_STATUSES: Tuple[str, ...] = (
    STATUS_CONTACTED,
    STATUS_CONVERSATION,
    STATUS_FOLLOW_UP,
    STATUS_OFFER_PREPARING,
    STATUS_OFFER_SENT,
    STATUS_NEGOTIATING,
)

#: Statuses where a property is tied up and the work is on the buy side.
CONTRACTED_STATUSES: Tuple[str, ...] = (
    STATUS_UNDER_CONTRACT,
    STATUS_BUYER_SEARCH,
    STATUS_ASSIGNED,
)

#: Sort order for "furthest along first".
STATUS_ORDER: Dict[str, int] = {
    name: index for index, name in enumerate(ACQUISITION_STATUSES)
}

#: What each status means, shown by ``--dashboard`` and the CLI error message.
STATUS_DESCRIPTIONS: Dict[str, str] = {
    STATUS_NEW: "found, not looked at yet",
    STATUS_RESEARCHING: "research in progress",
    STATUS_HOT: "worth pursuing, no contact route yet",
    STATUS_CONTACT_READY: "contact information in hand — make the call",
    STATUS_CONTACTED: "reached out, no conversation yet",
    STATUS_CONVERSATION: "talking to the seller",
    STATUS_FOLLOW_UP: "waiting on a callback or a decision",
    STATUS_OFFER_PREPARING: "working out the number",
    STATUS_OFFER_SENT: "offer with the seller",
    STATUS_NEGOTIATING: "countering back and forth",
    STATUS_UNDER_CONTRACT: "signed — inspection and title work",
    STATUS_BUYER_SEARCH: "finding an end buyer",
    STATUS_ASSIGNED: "assignment signed",
    STATUS_CLOSED: "closed and paid",
    STATUS_DEAD: "not happening",
    STATUS_PASSED: "we walked away",
}


def normalize_status(status: str) -> str:
    """Upper-case, and fold a Wave 4 name onto its Wave 5 equivalent."""
    value = (status or "").strip().upper().replace("-", "_").replace(" ", "_")
    return LEGACY_STATUS_ALIASES.get(value, value)


def is_valid_status(status: str) -> bool:
    return normalize_status(status) in ACQUISITION_STATUSES


def status_index(status: str) -> int:
    return STATUS_ORDER.get(normalize_status(status), -1)


def is_closed(status: str) -> bool:
    return normalize_status(status) in CLOSED_STATUSES


def is_active(status: str) -> bool:
    return normalize_status(status) in ACTIVE_STATUSES


def describe_status(status: str) -> str:
    return STATUS_DESCRIPTIONS.get(normalize_status(status), "")


def next_suggested_status(status: str) -> Optional[str]:
    """The natural next step, for the daily list. Never enforced."""
    normalized = normalize_status(status)
    if normalized in CLOSED_STATUSES or normalized == STATUS_ASSIGNED:
        return None
    index = STATUS_ORDER.get(normalized)
    if index is None or index + 1 >= len(ACQUISITION_STATUSES):
        return None
    following = ACQUISITION_STATUSES[index + 1]
    return None if following in CLOSED_STATUSES else following
