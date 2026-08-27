"""Persistence for the acquisition side, on the same SQLite file as the leads.

Shares :class:`~wholesale_engine.storage.LeadStore`'s connection so a property
and everything it accumulated — contacts, outreach, offers, contracts, buyers,
assignments — read back together and stay consistent.

Every write that matters also lands in the lead activity log, so the dossier
and ``--activity`` show the whole story in one place.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from ..research.facts import Confidence
from ..storage import LeadStore, StoredLead
from .models import (
    AssignmentStatus,
    Assignment,
    Buyer,
    Channel,
    Contact,
    Contract,
    ContractStatus,
    Direction,
    Offer,
    OfferStatus,
    Outcome,
    OutreachActivity,
    PhoneType,
)
from .pipeline import normalize_status

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    owner_name          TEXT,
    phone               TEXT,
    phone_type          TEXT NOT NULL DEFAULT 'UNKNOWN',
    phone_confidence    TEXT NOT NULL DEFAULT 'UNKNOWN',
    email               TEXT,
    email_confidence    TEXT NOT NULL DEFAULT 'UNKNOWN',
    mailing_address     TEXT,
    source              TEXT NOT NULL DEFAULT '',
    source_date         TEXT,
    verified            INTEGER NOT NULL DEFAULT 0,
    is_test_data        INTEGER NOT NULL DEFAULT 0,
    notes               TEXT NOT NULL DEFAULT '',
    next_follow_up      TEXT,
    follow_up_reason    TEXT NOT NULL DEFAULT '',
    last_contacted      TEXT,
    contact_attempts    INTEGER NOT NULL DEFAULT 0,
    last_outcome        TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(property_id, source)
);

CREATE TABLE IF NOT EXISTS outreach (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    contact_id          INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    timestamp           TEXT NOT NULL,
    channel             TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'OUTBOUND',
    outcome             TEXT,
    notes               TEXT NOT NULL DEFAULT '',
    next_follow_up      TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id             TEXT NOT NULL,
    offer_amount            REAL,
    offer_date              TEXT,
    seller_counter          REAL,
    counter_date            TEXT,
    current_price           REAL,
    mao                     REAL,
    arv                     REAL,
    repairs                 REAL,
    end_buyer_ceiling       REAL,
    target_wholesale_fee    REAL,
    potential_wholesale_fee REAL,
    offer_status            TEXT NOT NULL DEFAULT 'DRAFT',
    notes                   TEXT NOT NULL DEFAULT '',
    warnings                TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    contract_date       TEXT,
    purchase_price      REAL,
    inspection_deadline TEXT,
    closing_date        TEXT,
    earnest_money       REAL,
    assignment_allowed  INTEGER,
    seller              TEXT NOT NULL DEFAULT '',
    buyer               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'PENDING',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buyers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    company             TEXT NOT NULL DEFAULT '',
    email               TEXT,
    phone               TEXT,
    market              TEXT NOT NULL DEFAULT '',
    property_types      TEXT NOT NULL DEFAULT '[]',
    min_price           REAL,
    max_price           REAL,
    preferred_states    TEXT NOT NULL DEFAULT '[]',
    is_test_data        INTEGER NOT NULL DEFAULT 0,
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    UNIQUE(name, company)
);

CREATE TABLE IF NOT EXISTS assignments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    buyer_id            INTEGER REFERENCES buyers(id) ON DELETE SET NULL,
    buyer_name          TEXT NOT NULL DEFAULT '',
    purchase_price      REAL,
    assignment_price    REAL,
    assignment_date     TEXT,
    status              TEXT NOT NULL DEFAULT 'BUYER_SEARCH',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_methods (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    contact_id          INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    value               TEXT NOT NULL,
    phone_type          TEXT NOT NULL DEFAULT 'UNKNOWN',
    confidence          TEXT NOT NULL DEFAULT 'UNKNOWN',
    status              TEXT NOT NULL DEFAULT 'UNVERIFIED',
    source              TEXT NOT NULL DEFAULT '',
    source_date         TEXT,
    last_verified       TEXT,
    is_test_data        INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_outcome        TEXT,
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    UNIQUE(property_id, kind, value)
);

CREATE TABLE IF NOT EXISTS seller_responses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         TEXT NOT NULL,
    response            TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    notes               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_methods_property ON contact_methods(property_id);
CREATE INDEX IF NOT EXISTS idx_methods_kind ON contact_methods(kind);
CREATE INDEX IF NOT EXISTS idx_responses_property ON seller_responses(property_id);
CREATE INDEX IF NOT EXISTS idx_contacts_property ON contacts(property_id);
CREATE INDEX IF NOT EXISTS idx_contacts_follow_up ON contacts(next_follow_up);
CREATE INDEX IF NOT EXISTS idx_outreach_property ON outreach(property_id);
CREATE INDEX IF NOT EXISTS idx_offers_property ON offers(property_id);
CREATE INDEX IF NOT EXISTS idx_contracts_property ON contracts(property_id);
CREATE INDEX IF NOT EXISTS idx_assignments_property ON assignments(property_id);
"""


def _as_date(raw: Any) -> Optional[date]:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _as_datetime(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class AcquisitionStore:
    """Contacts, outreach, offers, contracts, buyers and assignments."""

    def __init__(self, lead_store: LeadStore) -> None:
        self.leads = lead_store
        self.connection: sqlite3.Connection = lead_store.connection
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _lead_row_id(self, property_id: str) -> Optional[int]:
        row = self.leads.find_one(property_id)
        return row.lead_row_id if row else None

    def _log(self, property_id: str, activity_type: str, description: str) -> None:
        """Mirror an acquisition event into the lead activity log."""
        lead_row_id = self._lead_row_id(property_id)
        try:
            self.leads.log_activity(lead_row_id, activity_type, description, property_id)
        except ValueError:
            # An unknown activity type is a programming error, not a data one;
            # never let it lose the record that prompted it.
            self.leads.log_activity(
                lead_row_id, "lead_updated", f"{activity_type}: {description}", property_id
            )

    # ==================================================================
    # Contacts
    # ==================================================================

    def _row_to_contact(self, row: sqlite3.Row) -> Contact:
        return Contact(
            contact_id=row["id"],
            property_id=row["property_id"],
            owner_name=row["owner_name"],
            phone=row["phone"],
            phone_type=PhoneType.parse(row["phone_type"]),
            phone_confidence=Confidence.parse(row["phone_confidence"]),
            email=row["email"],
            email_confidence=Confidence.parse(row["email_confidence"]),
            mailing_address=row["mailing_address"],
            source=row["source"],
            source_date=_as_date(row["source_date"]),
            verified=bool(row["verified"]),
            is_test_data=bool(row["is_test_data"]),
            notes=row["notes"],
            next_follow_up=_as_date(row["next_follow_up"]),
            follow_up_reason=row["follow_up_reason"],
            last_contacted=_as_datetime(row["last_contacted"]),
            contact_attempts=row["contact_attempts"],
            last_outcome=row["last_outcome"],
        )

    def save_contact(self, contact: Contact) -> Contact:
        """Insert or update a contact, keyed on property plus source.

        The phone and email are also written as :class:`ContactMethod` rows.
        That is what puts them on the suppression list, so a later
        DO_NOT_CONTACT actually suppresses them — every producer of contacts
        (skip trace, manual entry, import) goes through here, so none of them
        can skip that step.
        """
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            cur.execute(
                "SELECT id FROM contacts WHERE property_id = ? AND source = ?",
                (contact.property_id, contact.source),
            )
            existing = cur.fetchone()
            values = (
                contact.owner_name, contact.phone, str(contact.phone_type),
                str(contact.phone_confidence), contact.email,
                str(contact.email_confidence), contact.mailing_address,
                _iso(contact.source_date), int(contact.verified),
                int(contact.is_test_data), contact.notes,
                _iso(contact.next_follow_up), contact.follow_up_reason,
                _iso(contact.last_contacted), contact.contact_attempts,
                contact.last_outcome,
            )
            if existing is None:
                cur.execute(
                    """INSERT INTO contacts
                       (property_id, source, owner_name, phone, phone_type,
                        phone_confidence, email, email_confidence, mailing_address,
                        source_date, verified, is_test_data, notes, next_follow_up,
                        follow_up_reason, last_contacted, contact_attempts,
                        last_outcome, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (contact.property_id, contact.source) + values + (now,),
                )
                contact.contact_id = cur.lastrowid
                found = "contact details found" if contact.is_reachable else "no contact found"
                self._log(
                    contact.property_id, "contact_added",
                    f"{found} via {contact.source or 'manual entry'}"
                    + (" (TEST DATA)" if contact.is_test_data else ""),
                )
            else:
                contact.contact_id = existing["id"]
                cur.execute(
                    """UPDATE contacts SET owner_name=?, phone=?, phone_type=?,
                           phone_confidence=?, email=?, email_confidence=?,
                           mailing_address=?, source_date=?, verified=?,
                           is_test_data=?, notes=?, next_follow_up=?,
                           follow_up_reason=?, last_contacted=?, contact_attempts=?,
                           last_outcome=?
                       WHERE id = ?""",
                    values + (contact.contact_id,),
                )
        self.connection.commit()
        self._sync_methods(contact)
        return contact

    def _sync_methods(self, contact: Contact) -> None:
        """Mirror a contact's phone and email into the contact_methods table.

        Merging, so this is idempotent and never overwrites a verified record
        with a weaker one.
        """
        from .contact_methods import ContactMethod

        for method in (
            ContactMethod.phone(
                contact.phone, contact.source or "manual", contact.phone_confidence,
                contact.phone_type, property_id=contact.property_id,
                contact_id=contact.contact_id, is_test_data=contact.is_test_data,
                source_date=contact.source_date,
            ),
            ContactMethod.email(
                contact.email, contact.source or "manual", contact.email_confidence,
                property_id=contact.property_id, contact_id=contact.contact_id,
                is_test_data=contact.is_test_data, source_date=contact.source_date,
            ),
            ContactMethod.address(
                contact.mailing_address, contact.source or "manual",
                property_id=contact.property_id, contact_id=contact.contact_id,
                is_test_data=contact.is_test_data,
            ),
        ):
            if method is not None:
                self.save_method(method)

    def contacts_for(self, property_id: str) -> List[Contact]:
        rows = self.connection.execute(
            "SELECT * FROM contacts WHERE property_id = ? ORDER BY verified DESC, id",
            (property_id,),
        )
        return [self._row_to_contact(r) for r in rows]

    def best_contact(self, property_id: str) -> Optional[Contact]:
        """The most usable contact: verified first, then reachable, then any."""
        contacts = self.contacts_for(property_id)
        if not contacts:
            return None
        return max(
            contacts,
            key=lambda c: (
                int(c.verified),
                int(c.has_phone),
                int(c.has_email),
                int(c.has_mailing_address),
                c.phone_confidence.rank,
            ),
        )

    def all_contacts(self) -> List[Contact]:
        return [
            self._row_to_contact(r)
            for r in self.connection.execute("SELECT * FROM contacts ORDER BY property_id, id")
        ]

    def set_follow_up(
        self,
        property_id: str,
        when: Optional[date],
        reason: str = "",
        contact_id: Optional[int] = None,
    ) -> bool:
        """Schedule (or clear) the next follow-up for a property."""
        target = contact_id
        if target is None:
            contact = self.best_contact(property_id)
            target = contact.contact_id if contact else None
        if target is None:
            return False
        self.connection.execute(
            "UPDATE contacts SET next_follow_up = ?, follow_up_reason = ? WHERE id = ?",
            (_iso(when), reason, target),
        )
        self.connection.commit()
        return True

    # ==================================================================
    # Contact methods — multiple phones and emails per owner
    # ==================================================================

    def _row_to_method(self, row: sqlite3.Row) -> "ContactMethod":
        from .contact_methods import ContactMethod, MethodKind, MethodStatus

        return ContactMethod(
            method_id=row["id"],
            property_id=row["property_id"],
            contact_id=row["contact_id"],
            kind=MethodKind(row["kind"]),
            value=row["value"],
            phone_type=PhoneType.parse(row["phone_type"]),
            confidence=Confidence.parse(row["confidence"]),
            status=MethodStatus(row["status"]),
            source=row["source"],
            source_date=_as_date(row["source_date"]),
            last_verified=_as_date(row["last_verified"]),
            is_test_data=bool(row["is_test_data"]),
            attempts=row["attempts"],
            last_outcome=row["last_outcome"],
            notes=row["notes"],
        )

    def methods_for(
        self, property_id: str, kind: Optional[object] = None
    ) -> List["ContactMethod"]:
        """Every way to reach this owner, best first."""
        sql = "SELECT * FROM contact_methods WHERE property_id = ?"
        params: List[Any] = [property_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        rows = [self._row_to_method(r) for r in self.connection.execute(sql, params)]
        return sorted(rows, key=lambda m: m.rank(), reverse=True)

    def save_method(self, method: "ContactMethod") -> "MergeOutcome":
        """Add or merge one contact method.

        Never blindly overwrites: a verified record beats a weaker update, and
        the disagreement is recorded rather than applied. Returns the outcome
        so the caller can report what actually happened.
        """
        from .contact_methods import merge_method

        existing = next(
            (
                m for m in self.methods_for(method.property_id, method.kind)
                if m.value.lower() == method.value.lower()
            ),
            None,
        )
        outcome = merge_method(existing, method)
        merged = outcome.method
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            if merged.method_id is None:
                cur.execute(
                    """INSERT INTO contact_methods
                       (property_id, contact_id, kind, value, phone_type, confidence,
                        status, source, source_date, last_verified, is_test_data,
                        attempts, last_outcome, notes, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        merged.property_id, merged.contact_id, str(merged.kind),
                        merged.value, str(merged.phone_type), str(merged.confidence),
                        str(merged.status), merged.source, _iso(merged.source_date),
                        _iso(merged.last_verified), int(merged.is_test_data),
                        merged.attempts, merged.last_outcome, merged.notes, now,
                    ),
                )
                merged.method_id = cur.lastrowid
            else:
                cur.execute(
                    """UPDATE contact_methods SET contact_id=?, phone_type=?,
                           confidence=?, status=?, source=?, source_date=?,
                           last_verified=?, is_test_data=?, attempts=?,
                           last_outcome=?, notes=?
                       WHERE id = ?""",
                    (
                        merged.contact_id, str(merged.phone_type), str(merged.confidence),
                        str(merged.status), merged.source, _iso(merged.source_date),
                        _iso(merged.last_verified), int(merged.is_test_data),
                        merged.attempts, merged.last_outcome, merged.notes,
                        merged.method_id,
                    ),
                )
        self.connection.commit()
        if outcome.action in ("added", "improved"):
            self._log(
                merged.property_id, "contact_added",
                f"{merged.kind} {outcome.action}: {merged.display()} [{merged.label()}]",
            )
        elif outcome.action == "conflict":
            self._log(
                merged.property_id, "contact_added",
                f"CONFLICT kept verified {merged.kind}: {outcome.detail}",
            )
        return outcome

    def set_method_status(
        self, method_id: int, status: object, notes: str = ""
    ) -> bool:
        """Mark a method verified, wrong, invalid or do-not-contact."""
        from .contact_methods import MethodStatus

        value = MethodStatus(str(status))
        verified = _iso(date.today()) if value is MethodStatus.VERIFIED else None
        cur = self.connection.execute(
            """UPDATE contact_methods
               SET status = ?, last_verified = COALESCE(?, last_verified),
                   notes = TRIM(notes || ? )
               WHERE id = ?""",
            (str(value), verified, f"\n{notes}" if notes else "", method_id),
        )
        self.connection.commit()
        return cur.rowcount > 0

    def all_methods(self) -> List["ContactMethod"]:
        return [
            self._row_to_method(r)
            for r in self.connection.execute(
                "SELECT * FROM contact_methods ORDER BY property_id, kind, id"
            )
        ]

    def suppressed_values(self) -> List[str]:
        """Everything marked DO_NOT_CONTACT, INVALID or WRONG.

        The suppression list. Nothing here may ever be dialled, texted or
        emailed again, whatever a later skip trace says.
        """
        from .contact_methods import SUPPRESSED_STATUSES

        names = tuple(str(s) for s in SUPPRESSED_STATUSES)
        placeholders = ", ".join("?" for _ in names)
        return [
            row["value"]
            for row in self.connection.execute(
                f"SELECT value FROM contact_methods WHERE status IN ({placeholders})",
                names,
            )
        ]

    # ==================================================================
    # Seller responses
    # ==================================================================

    def record_seller_response(
        self, property_id: str, response: object, notes: str = ""
    ) -> str:
        """Log how the seller replied. Free text lives in ``notes``."""
        from .models import SellerResponse

        value = SellerResponse.parse(response)
        self.connection.execute(
            """INSERT INTO seller_responses (property_id, response, recorded_at, notes)
               VALUES (?,?,?,?)""",
            (property_id, str(value), self._now(), notes),
        )
        self.connection.commit()
        self._log(
            property_id, "seller_response",
            f"{value}" + (f": {notes[:60]}" if notes else ""),
        )
        if value is SellerResponse.DO_NOT_CONTACT:
            from .contact_methods import MethodStatus

            for method in self.methods_for(property_id):
                self.set_method_status(
                    method.method_id, MethodStatus.DO_NOT_CONTACT,
                    "seller asked not to be contacted",
                )
        elif value is SellerResponse.WRONG_NUMBER:
            from .contact_methods import MethodKind, MethodStatus

            for method in self.methods_for(property_id, MethodKind.PHONE):
                self.set_method_status(
                    method.method_id, MethodStatus.WRONG, "reached the wrong person"
                )
        return str(value)

    def seller_responses(self, property_id: str) -> List[Dict[str, Any]]:
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM seller_responses WHERE property_id = ? "
                "ORDER BY recorded_at DESC, id DESC",
                (property_id,),
            )
        ]

    def latest_seller_response(self, property_id: str) -> Optional[str]:
        rows = self.seller_responses(property_id)
        return rows[0]["response"] if rows else None

    # ==================================================================
    # Outreach
    # ==================================================================

    def _row_to_outreach(self, row: sqlite3.Row) -> OutreachActivity:
        return OutreachActivity(
            activity_id=row["id"],
            property_id=row["property_id"],
            contact_id=row["contact_id"],
            timestamp=_as_datetime(row["timestamp"]),
            channel=Channel.parse(row["channel"]),
            direction=Direction(row["direction"]),
            outcome=Outcome.parse(row["outcome"]) if row["outcome"] else None,
            notes=row["notes"],
            next_follow_up=_as_date(row["next_follow_up"]),
        )

    def log_outreach(self, activity: OutreachActivity) -> OutreachActivity:
        """Record an outreach attempt and roll the contact's state forward.

        This logs what you did. It does not place a call, send a text, or send
        an email — nothing in this engine communicates with anyone.
        """
        timestamp = activity.timestamp or datetime.now()
        contact = None
        if activity.contact_id is None:
            contact = self.best_contact(activity.property_id)
            activity.contact_id = contact.contact_id if contact else None

        with closing(self.connection.cursor()) as cur:
            cur.execute(
                """INSERT INTO outreach
                   (property_id, contact_id, timestamp, channel, direction,
                    outcome, notes, next_follow_up)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    activity.property_id, activity.contact_id,
                    _iso(timestamp), str(activity.channel), str(activity.direction),
                    str(activity.outcome) if activity.outcome else None,
                    activity.notes, _iso(activity.next_follow_up),
                ),
            )
            activity.activity_id = cur.lastrowid
            activity.timestamp = timestamp

            if activity.contact_id is not None:
                cur.execute(
                    """UPDATE contacts
                       SET last_contacted = ?, contact_attempts = contact_attempts + 1,
                           last_outcome = ?,
                           next_follow_up = COALESCE(?, next_follow_up)
                       WHERE id = ?""",
                    (
                        _iso(timestamp),
                        str(activity.outcome) if activity.outcome else None,
                        _iso(activity.next_follow_up),
                        activity.contact_id,
                    ),
                )
        self.connection.commit()

        detail = f"{activity.channel}"
        if activity.outcome:
            detail += f" — {activity.outcome}"
        if activity.notes:
            detail += f": {activity.notes[:60]}"
        self._log(activity.property_id, "outreach_logged", detail)
        return activity

    def outreach_for(self, property_id: str) -> List[OutreachActivity]:
        """Every logged attempt, newest first."""
        rows = self.connection.execute(
            "SELECT * FROM outreach WHERE property_id = ? ORDER BY timestamp DESC, id DESC",
            (property_id,),
        )
        return [self._row_to_outreach(r) for r in rows]

    def all_outreach(self, limit: Optional[int] = None) -> List[OutreachActivity]:
        sql = "SELECT * FROM outreach ORDER BY timestamp DESC, id DESC"
        params: List[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_outreach(r) for r in self.connection.execute(sql, params)]

    # ==================================================================
    # Offers
    # ==================================================================

    def _row_to_offer(self, row: sqlite3.Row) -> Offer:
        return Offer(
            offer_id=row["id"],
            property_id=row["property_id"],
            offer_amount=row["offer_amount"],
            offer_date=_as_date(row["offer_date"]),
            seller_counter=row["seller_counter"],
            counter_date=_as_date(row["counter_date"]),
            current_price=row["current_price"],
            mao=row["mao"],
            arv=row["arv"],
            repairs=row["repairs"],
            end_buyer_ceiling=row["end_buyer_ceiling"],
            target_wholesale_fee=row["target_wholesale_fee"],
            potential_wholesale_fee=row["potential_wholesale_fee"],
            offer_status=OfferStatus.parse(row["offer_status"]),
            notes=row["notes"],
            warnings=[w for w in (row["warnings"] or "").split(" | ") if w],
        )

    def save_offer(self, offer: Offer) -> Offer:
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            if offer.offer_id is None:
                cur.execute(
                    """INSERT INTO offers
                       (property_id, offer_amount, offer_date, seller_counter,
                        counter_date, current_price, mao, arv, repairs,
                        end_buyer_ceiling, target_wholesale_fee,
                        potential_wholesale_fee, offer_status, notes, warnings,
                        created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        offer.property_id, offer.offer_amount, _iso(offer.offer_date),
                        offer.seller_counter, _iso(offer.counter_date),
                        offer.current_price, offer.mao, offer.arv, offer.repairs,
                        offer.end_buyer_ceiling, offer.target_wholesale_fee,
                        offer.potential_wholesale_fee, str(offer.offer_status),
                        offer.notes, " | ".join(offer.warnings), now,
                    ),
                )
                offer.offer_id = cur.lastrowid
                self._log(
                    offer.property_id, "offer_made",
                    f"{offer.offer_status} at "
                    + (f"${offer.offer_amount:,.0f}" if offer.offer_amount else "no amount"),
                )
            else:
                cur.execute(
                    """UPDATE offers SET offer_amount=?, offer_date=?, seller_counter=?,
                           counter_date=?, current_price=?, mao=?, arv=?, repairs=?,
                           end_buyer_ceiling=?, target_wholesale_fee=?,
                           potential_wholesale_fee=?, offer_status=?, notes=?, warnings=?
                       WHERE id = ?""",
                    (
                        offer.offer_amount, _iso(offer.offer_date), offer.seller_counter,
                        _iso(offer.counter_date), offer.current_price, offer.mao,
                        offer.arv, offer.repairs, offer.end_buyer_ceiling,
                        offer.target_wholesale_fee, offer.potential_wholesale_fee,
                        str(offer.offer_status), offer.notes,
                        " | ".join(offer.warnings), offer.offer_id,
                    ),
                )
        self.connection.commit()
        return offer

    def offers_for(self, property_id: str) -> List[Offer]:
        """Offer history, newest first."""
        rows = self.connection.execute(
            "SELECT * FROM offers WHERE property_id = ? ORDER BY created_at DESC, id DESC",
            (property_id,),
        )
        return [self._row_to_offer(r) for r in rows]

    def latest_offer(self, property_id: str) -> Optional[Offer]:
        offers = self.offers_for(property_id)
        return offers[0] if offers else None

    def all_offers(self, open_only: bool = False) -> List[Offer]:
        offers = [
            self._row_to_offer(r)
            for r in self.connection.execute("SELECT * FROM offers ORDER BY created_at DESC, id DESC")
        ]
        return [o for o in offers if o.is_open] if open_only else offers

    def record_counter(
        self,
        property_id: str,
        amount: float,
        when: Optional[date] = None,
        notes: str = "",
    ) -> Optional[Offer]:
        """Log the seller's counter against the most recent offer."""
        offer = self.latest_offer(property_id)
        if offer is None:
            return None
        offer.seller_counter = amount
        offer.counter_date = when or date.today()
        offer.offer_status = OfferStatus.COUNTERED
        if notes:
            offer.notes = f"{offer.notes}\n{notes}".strip()
        self.save_offer(offer)
        self._log(property_id, "counter_received", f"seller countered at ${amount:,.0f}")
        return offer

    # ==================================================================
    # Contracts
    # ==================================================================

    def _row_to_contract(self, row: sqlite3.Row) -> Contract:
        return Contract(
            contract_id=row["id"],
            property_id=row["property_id"],
            contract_date=_as_date(row["contract_date"]),
            purchase_price=row["purchase_price"],
            inspection_deadline=_as_date(row["inspection_deadline"]),
            closing_date=_as_date(row["closing_date"]),
            earnest_money=row["earnest_money"],
            assignment_allowed=(
                None if row["assignment_allowed"] is None else bool(row["assignment_allowed"])
            ),
            seller=row["seller"],
            buyer=row["buyer"],
            status=ContractStatus.parse(row["status"]),
            notes=row["notes"],
        )

    def save_contract(self, contract: Contract) -> Contract:
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            if contract.contract_id is None:
                cur.execute(
                    """INSERT INTO contracts
                       (property_id, contract_date, purchase_price, inspection_deadline,
                        closing_date, earnest_money, assignment_allowed, seller, buyer,
                        status, notes, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        contract.property_id, _iso(contract.contract_date),
                        contract.purchase_price, _iso(contract.inspection_deadline),
                        _iso(contract.closing_date), contract.earnest_money,
                        None if contract.assignment_allowed is None
                        else int(contract.assignment_allowed),
                        contract.seller, contract.buyer, str(contract.status),
                        contract.notes, now,
                    ),
                )
                contract.contract_id = cur.lastrowid
                self._log(
                    contract.property_id, "contract_recorded",
                    f"{contract.status} at "
                    + (f"${contract.purchase_price:,.0f}" if contract.purchase_price else "no price"),
                )
            else:
                cur.execute(
                    """UPDATE contracts SET contract_date=?, purchase_price=?,
                           inspection_deadline=?, closing_date=?, earnest_money=?,
                           assignment_allowed=?, seller=?, buyer=?, status=?, notes=?
                       WHERE id = ?""",
                    (
                        _iso(contract.contract_date), contract.purchase_price,
                        _iso(contract.inspection_deadline), _iso(contract.closing_date),
                        contract.earnest_money,
                        None if contract.assignment_allowed is None
                        else int(contract.assignment_allowed),
                        contract.seller, contract.buyer, str(contract.status),
                        contract.notes, contract.contract_id,
                    ),
                )
        self.connection.commit()
        return contract

    def contract_for(self, property_id: str) -> Optional[Contract]:
        row = self.connection.execute(
            "SELECT * FROM contracts WHERE property_id = ? ORDER BY created_at DESC LIMIT 1",
            (property_id,),
        ).fetchone()
        return self._row_to_contract(row) if row else None

    def all_contracts(self, live_only: bool = False) -> List[Contract]:
        contracts = [
            self._row_to_contract(r)
            for r in self.connection.execute("SELECT * FROM contracts ORDER BY closing_date, id")
        ]
        return [c for c in contracts if c.is_live] if live_only else contracts

    # ==================================================================
    # Buyers
    # ==================================================================

    def _row_to_buyer(self, row: sqlite3.Row) -> Buyer:
        return Buyer(
            buyer_id=row["id"],
            name=row["name"],
            company=row["company"],
            email=row["email"],
            phone=row["phone"],
            market=row["market"],
            property_types=json.loads(row["property_types"] or "[]"),
            min_price=row["min_price"],
            max_price=row["max_price"],
            preferred_states=json.loads(row["preferred_states"] or "[]"),
            is_test_data=bool(row["is_test_data"]),
            notes=row["notes"],
        )

    def save_buyer(self, buyer: Buyer) -> Buyer:
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            cur.execute(
                "SELECT id FROM buyers WHERE name = ? AND company = ?",
                (buyer.name, buyer.company),
            )
            existing = cur.fetchone()
            values = (
                buyer.email, buyer.phone, buyer.market,
                json.dumps(buyer.property_types), buyer.min_price, buyer.max_price,
                json.dumps(buyer.preferred_states), int(buyer.is_test_data), buyer.notes,
            )
            if existing is None:
                cur.execute(
                    """INSERT INTO buyers
                       (name, company, email, phone, market, property_types,
                        min_price, max_price, preferred_states, is_test_data,
                        notes, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (buyer.name, buyer.company) + values + (now,),
                )
                buyer.buyer_id = cur.lastrowid
            else:
                buyer.buyer_id = existing["id"]
                cur.execute(
                    """UPDATE buyers SET email=?, phone=?, market=?, property_types=?,
                           min_price=?, max_price=?, preferred_states=?, is_test_data=?,
                           notes=? WHERE id = ?""",
                    values + (buyer.buyer_id,),
                )
        self.connection.commit()
        return buyer

    def all_buyers(self) -> List[Buyer]:
        return [
            self._row_to_buyer(r)
            for r in self.connection.execute("SELECT * FROM buyers ORDER BY name, company")
        ]

    def matching_buyers(
        self, state: str = "", property_type: str = "", price: Optional[float] = None
    ) -> List[Buyer]:
        """Buyers whose buy box fits this property."""
        return [
            b for b in self.all_buyers()
            if b.matches(state=state, property_type=property_type, price=price)
        ]

    # ==================================================================
    # Assignments
    # ==================================================================

    def _row_to_assignment(self, row: sqlite3.Row) -> Assignment:
        return Assignment(
            assignment_id=row["id"],
            property_id=row["property_id"],
            buyer_id=row["buyer_id"],
            buyer_name=row["buyer_name"],
            purchase_price=row["purchase_price"],
            assignment_price=row["assignment_price"],
            assignment_date=_as_date(row["assignment_date"]),
            status=AssignmentStatus.parse(row["status"]),
            notes=row["notes"],
        )

    def save_assignment(self, assignment: Assignment) -> Assignment:
        now = self._now()
        with closing(self.connection.cursor()) as cur:
            if assignment.assignment_id is None:
                cur.execute(
                    """INSERT INTO assignments
                       (property_id, buyer_id, buyer_name, purchase_price,
                        assignment_price, assignment_date, status, notes, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        assignment.property_id, assignment.buyer_id,
                        assignment.buyer_name, assignment.purchase_price,
                        assignment.assignment_price, _iso(assignment.assignment_date),
                        str(assignment.status), assignment.notes, now,
                    ),
                )
                assignment.assignment_id = cur.lastrowid
            else:
                cur.execute(
                    """UPDATE assignments SET buyer_id=?, buyer_name=?, purchase_price=?,
                           assignment_price=?, assignment_date=?, status=?, notes=?
                       WHERE id = ?""",
                    (
                        assignment.buyer_id, assignment.buyer_name,
                        assignment.purchase_price, assignment.assignment_price,
                        _iso(assignment.assignment_date), str(assignment.status),
                        assignment.notes, assignment.assignment_id,
                    ),
                )
        self.connection.commit()
        fee = assignment.gross_assignment_fee
        self._log(
            assignment.property_id, "assignment_updated",
            f"{assignment.status}"
            + (f", fee ${fee:,.0f}" if fee is not None else "")
            + (f" to {assignment.buyer_name}" if assignment.buyer_name else ""),
        )
        return assignment

    def assignment_for(self, property_id: str) -> Optional[Assignment]:
        row = self.connection.execute(
            "SELECT * FROM assignments WHERE property_id = ? ORDER BY created_at DESC LIMIT 1",
            (property_id,),
        ).fetchone()
        return self._row_to_assignment(row) if row else None

    def all_assignments(self, live_only: bool = False) -> List[Assignment]:
        rows = [
            self._row_to_assignment(r)
            for r in self.connection.execute("SELECT * FROM assignments ORDER BY created_at DESC, id DESC")
        ]
        return [a for a in rows if a.is_live] if live_only else rows
