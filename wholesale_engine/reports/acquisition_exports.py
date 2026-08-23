"""Export column sets for the acquisition records.

One place that decides what a contacts CSV, an offers CSV or a pipeline CSV
contains, so the CSV and JSON adapters always agree and a spreadsheet built
against one of these keeps working.

Contact exports carry an ``is_test_data`` column. A row that came from the
mock provider is marked in the file itself, not only on screen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from ..acquisitions import (
    Assignment,
    Buyer,
    Contact,
    Contract,
    Offer,
    OutreachActivity,
    QueueEntry,
)
from ..storage import StoredLead

CONTACT_COLUMNS: List[str] = [
    "contact_id", "property_id", "owner_name", "phone", "phone_type",
    "phone_confidence", "email", "email_confidence", "mailing_address",
    "source", "source_date", "verified", "is_test_data", "provenance",
    "next_follow_up", "follow_up_reason", "last_contacted", "contact_attempts",
    "last_outcome", "notes",
]

OUTREACH_COLUMNS: List[str] = [
    "activity_id", "property_id", "contact_id", "timestamp", "channel",
    "direction", "outcome", "notes", "next_follow_up",
]

OFFER_COLUMNS: List[str] = [
    "offer_id", "property_id", "offer_amount", "offer_date", "seller_counter",
    "counter_date", "current_price", "current_proposed_price", "mao", "arv",
    "repairs", "end_buyer_ceiling", "target_wholesale_fee",
    "potential_wholesale_fee", "fee_at_current_price", "distance_to_mao",
    "distance_to_target_fee", "offer_status", "warnings", "notes",
]

CONTRACT_COLUMNS: List[str] = [
    "contract_id", "property_id", "contract_date", "purchase_price",
    "inspection_deadline", "closing_date", "earnest_money",
    "assignment_allowed", "seller", "buyer", "status", "notes",
]

BUYER_COLUMNS: List[str] = [
    "buyer_id", "name", "company", "email", "phone", "market",
    "property_types", "min_price", "max_price", "price_range",
    "preferred_states", "is_test_data", "notes",
]

ASSIGNMENT_COLUMNS: List[str] = [
    "assignment_id", "property_id", "buyer_id", "buyer_name", "purchase_price",
    "assignment_price", "gross_assignment_fee", "assignment_date", "status",
    "notes",
]

#: The whole acquisition picture: one row per property, everything joined.
PIPELINE_COLUMNS: List[str] = [
    "property_id", "address", "city", "state", "county", "zip",
    "property_type", "status", "status_description",
    "lead_score", "deal_score", "priority_score", "priority_band",
    "acquisition_priority", "next_action", "action_reason", "blockers",
    "arv", "arv_confidence", "comp_confidence", "repair_estimate",
    "asking_price", "end_buyer_ceiling", "mao", "recommended_offer",
    "target_wholesale_fee", "potential_fee", "fee_status",
    "equity_amount", "equity_percentage", "equity_status", "distress_count",
    "days_on_market",
    "owner_name", "phone_status", "email_status", "contact_provenance",
    "contact_attempts", "last_outcome", "last_contacted", "next_follow_up",
    "current_offer", "offer_status", "seller_counter", "price_on_the_table",
    "fee_at_current_price",
    "contract_status", "contract_purchase_price", "closing_date",
    "assignment_status", "assignment_buyer", "gross_assignment_fee",
    "first_seen", "last_seen", "times_seen", "final_decision",
]


def contact_rows(contacts: Sequence[Contact]) -> List[Dict[str, Any]]:
    return [c.as_dict() for c in contacts]


def outreach_rows(activities: Sequence[OutreachActivity]) -> List[Dict[str, Any]]:
    return [a.as_dict() for a in activities]


def offer_rows(offers: Sequence[Offer]) -> List[Dict[str, Any]]:
    return [o.as_dict() for o in offers]


def contract_rows(contracts: Sequence[Contract]) -> List[Dict[str, Any]]:
    return [c.as_dict() for c in contracts]


def buyer_rows(buyers: Sequence[Buyer]) -> List[Dict[str, Any]]:
    return [b.as_dict() for b in buyers]


def assignment_rows(assignments: Sequence[Assignment]) -> List[Dict[str, Any]]:
    return [a.as_dict() for a in assignments]


def pipeline_rows(
    entries: Sequence[QueueEntry],
    store,
    target_wholesale_fee: float = 18_000.0,
) -> List[Dict[str, Any]]:
    """One row per property with everything the acquisition side knows.

    Unknowns stay ``None``. A blank cell here means nobody has found out yet,
    which is a different thing from a zero.
    """
    from ..acquisitions import describe_status

    rows: List[Dict[str, Any]] = []
    for entry in entries:
        row: StoredLead = entry.row
        contact = entry.contact
        offers = store.offers_for(row.dedupe_key)
        latest = offers[0] if offers else None
        contract = store.contract_for(row.dedupe_key)
        assignment = store.assignment_for(row.dedupe_key)

        ceiling = None
        if row.arv is not None and row.repair_estimate is not None:
            # Recomputed here only for the export; the analyzer owns the rule.
            ceiling = latest.end_buyer_ceiling if latest else None

        rows.append(
            {
                "property_id": row.dedupe_key,
                "address": row.address,
                "city": row.city,
                "state": row.state,
                "county": row.county,
                "zip": row.zip_code,
                "property_type": row.property_type,
                "status": row.status,
                "status_description": describe_status(row.status),
                "lead_score": row.lead_score,
                "deal_score": row.deal_score,
                "priority_score": row.priority_score,
                "priority_band": row.priority_band,
                "acquisition_priority": entry.priority.score,
                "next_action": str(entry.priority.action),
                "action_reason": entry.priority.reason,
                "blockers": " | ".join(entry.priority.blockers),
                "arv": row.arv,
                "arv_confidence": row.arv_confidence,
                "comp_confidence": row.comp_confidence,
                "repair_estimate": row.repair_estimate,
                "asking_price": row.asking_price,
                "end_buyer_ceiling": ceiling,
                "mao": row.mao,
                "recommended_offer": row.recommended_offer,
                "target_wholesale_fee": target_wholesale_fee,
                "potential_fee": row.potential_fee,
                "fee_status": row.fee_status,
                "equity_amount": row.equity_amount,
                "equity_percentage": row.equity_percentage,
                "equity_status": row.equity_status,
                "distress_count": row.distress_count,
                "days_on_market": row.days_on_market,
                "owner_name": contact.owner_name if contact else None,
                "phone_status": entry.phone_status,
                "email_status": entry.email_status,
                "contact_provenance": contact.provenance if contact else "UNKNOWN",
                "contact_attempts": contact.contact_attempts if contact else 0,
                "last_outcome": contact.last_outcome if contact else None,
                "last_contacted": (
                    contact.last_contacted.isoformat(timespec="seconds")
                    if contact and contact.last_contacted else None
                ),
                "next_follow_up": (
                    contact.next_follow_up.isoformat()
                    if contact and contact.next_follow_up else None
                ),
                "current_offer": latest.offer_amount if latest else None,
                "offer_status": str(latest.offer_status) if latest else None,
                "seller_counter": latest.seller_counter if latest else None,
                "price_on_the_table": latest.current_proposed_price if latest else None,
                "fee_at_current_price": latest.fee_at_current_price if latest else None,
                "contract_status": str(contract.status) if contract else None,
                "contract_purchase_price": contract.purchase_price if contract else None,
                "closing_date": (
                    contract.closing_date.isoformat()
                    if contract and contract.closing_date else None
                ),
                "assignment_status": str(assignment.status) if assignment else None,
                "assignment_buyer": assignment.buyer_name if assignment else None,
                "gross_assignment_fee": (
                    assignment.gross_assignment_fee if assignment else None
                ),
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
                "times_seen": row.times_seen,
                "final_decision": row.final_decision,
            }
        )
    return rows
