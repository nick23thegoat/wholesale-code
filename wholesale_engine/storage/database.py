"""Local SQLite store for leads seen across runs.

Wave 3 had no database — leads existed only for the length of a run, so the
same property found on Monday and Friday was two unrelated rows. This is that
memory: first_seen, last_seen, the scores each time, and a working status you
move a lead through by hand.

Identity is the normalized address plus city, state and ZIP — the same key the
Wave 2 deduplicator uses within a run, so within-run and across-run duplicate
detection agree. Unit numbers are preserved, because #1 and #2 are two
properties.

Uses the stdlib ``sqlite3`` only. The file is yours; nothing is uploaded.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..lead_hunter.models import Lead
from ..lead_hunter.normalizer import normalize_lead

# --- lead workflow statuses -------------------------------------------------

STATUS_NEW = "NEW"
STATUS_RESEARCHED = "RESEARCHED"
STATUS_HOT = "HOT"
STATUS_CONTACT = "CONTACT"
STATUS_UNDER_CONTRACT = "UNDER_CONTRACT"
STATUS_PASSED = "PASSED"
STATUS_DEAD = "DEAD"

LEAD_STATUSES = (
    STATUS_NEW,
    STATUS_RESEARCHED,
    STATUS_HOT,
    STATUS_CONTACT,
    STATUS_UNDER_CONTRACT,
    STATUS_PASSED,
    STATUS_DEAD,
)

#: Statuses that mean "stop working this lead".
CLOSED_STATUSES = (STATUS_PASSED, STATUS_DEAD)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key          TEXT NOT NULL UNIQUE,
    address             TEXT NOT NULL DEFAULT '',
    city                TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL DEFAULT '',
    zip_code            TEXT NOT NULL DEFAULT '',
    county              TEXT NOT NULL DEFAULT '',
    property_type       TEXT NOT NULL DEFAULT '',
    beds                REAL,
    baths               REAL,
    sqft                INTEGER,
    year_built          INTEGER,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id         INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    lead_id             TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'NEW',
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    times_seen          INTEGER NOT NULL DEFAULT 1,
    lead_score          REAL,
    deal_score          REAL,
    asking_price        REAL,
    estimated_value     REAL,
    estimated_repairs   REAL,
    estimated_equity    REAL,
    occupancy           TEXT NOT NULL DEFAULT '',
    condition           TEXT NOT NULL DEFAULT '',
    signals_json        TEXT NOT NULL DEFAULT '{}',
    final_decision      TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    UNIQUE(property_id, source)
);

CREATE TABLE IF NOT EXISTS lead_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_row_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    seen_at             TEXT NOT NULL,
    lead_score          REAL,
    deal_score          REAL,
    asking_price        REAL,
    estimated_value     REAL,
    estimated_repairs   REAL,
    signals_json        TEXT NOT NULL DEFAULT '{}',
    change_summary      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_properties_dedupe ON properties(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_history_lead ON lead_history(lead_row_id);
"""

#: Signal fields snapshotted for change detection.
TRACKED_SIGNALS = (
    "absentee_owner",
    "vacant",
    "high_equity",
    "pre_foreclosure",
    "foreclosure",
    "tax_delinquent",
    "probate",
    "inherited",
    "code_violation",
    "tired_landlord",
)


def dedupe_key(lead: Lead) -> str:
    """Normalized address + city + state + ZIP: the cross-run identity.

    Normalization is applied on the way in when the lead has not already been
    through the pipeline, so a store used directly behaves the same as one fed
    by the funnel.
    """
    if not lead.normalized_address:
        normalize_lead(lead)
    parts = (
        lead.normalized_address,
        lead.normalized_city,
        lead.normalized_state,
        (lead.normalized_zip or lead.zip_code or "").strip(),
    )
    return "|".join(p.strip().lower() for p in parts)


def _signals_of(lead: Lead) -> Dict[str, Optional[bool]]:
    return {name: getattr(lead, name, None) for name in TRACKED_SIGNALS}


@dataclass
class StoredLead:
    """One row of the ``leads`` table joined to its property."""

    lead_row_id: int
    property_row_id: int
    dedupe_key: str
    address: str
    city: str
    state: str
    zip_code: str
    source: str
    status: str
    first_seen: str
    last_seen: str
    times_seen: int
    lead_score: Optional[float]
    deal_score: Optional[float]
    asking_price: Optional[float]
    estimated_value: Optional[float]
    estimated_repairs: Optional[float]
    estimated_equity: Optional[float]
    signals: Dict[str, Optional[bool]]
    final_decision: str

    @property
    def is_new(self) -> bool:
        return self.times_seen <= 1


class LeadStore:
    """Open a SQLite lead database, creating it if needed.

    Usable as a context manager::

        with LeadStore(path) as store:
            store.upsert_lead(lead, lead_score=80.0)
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def __enter__(self) -> "LeadStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _row_to_stored(self, row: sqlite3.Row) -> StoredLead:
        return StoredLead(
            lead_row_id=row["lead_row_id"],
            property_row_id=row["property_row_id"],
            dedupe_key=row["dedupe_key"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
            zip_code=row["zip_code"],
            source=row["source"],
            status=row["status"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            times_seen=row["times_seen"],
            lead_score=row["lead_score"],
            deal_score=row["deal_score"],
            asking_price=row["asking_price"],
            estimated_value=row["estimated_value"],
            estimated_repairs=row["estimated_repairs"],
            estimated_equity=row["estimated_equity"],
            signals=json.loads(row["signals_json"] or "{}"),
            final_decision=row["final_decision"],
        )

    _SELECT = """
        SELECT l.id AS lead_row_id, p.id AS property_row_id, p.dedupe_key,
               p.address, p.city, p.state, p.zip_code,
               l.source, l.status, l.first_seen, l.last_seen, l.times_seen,
               l.lead_score, l.deal_score, l.asking_price, l.estimated_value,
               l.estimated_repairs, l.estimated_equity, l.signals_json,
               l.final_decision
        FROM leads l JOIN properties p ON p.id = l.property_id
    """

    def get(self, key: str, source: str = "") -> Optional[StoredLead]:
        """The most recently seen lead for this address, optionally by source."""
        sql = self._SELECT + " WHERE p.dedupe_key = ?"
        params: List[Any] = [key]
        if source:
            sql += " AND l.source = ?"
            params.append(source)
        sql += " ORDER BY l.last_seen DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        return self._row_to_stored(row) if row else None

    def get_for_lead(self, lead: Lead) -> Optional[StoredLead]:
        return self.get(dedupe_key(lead), lead.source or "")

    def all_leads(self, status: Optional[str] = None) -> List[StoredLead]:
        sql = self._SELECT
        params: List[Any] = []
        if status:
            sql += " WHERE l.status = ?"
            params.append(status)
        sql += " ORDER BY l.last_seen DESC, l.deal_score DESC"
        return [self._row_to_stored(r) for r in self.connection.execute(sql, params)]

    def history(self, lead_row_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM lead_history WHERE lead_row_id = ? ORDER BY seen_at, id",
            (lead_row_id,),
        )
        return [dict(r) for r in rows]

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_lead(
        self,
        lead: Lead,
        lead_score: Optional[float] = None,
        deal_score: Optional[float] = None,
        final_decision: str = "",
        status: Optional[str] = None,
        seen_at: Optional[date] = None,
        change_summary: str = "",
    ) -> StoredLead:
        """Insert or update this property, appending a history snapshot.

        ``first_seen`` is never moved. ``status`` is only set on insert unless
        explicitly passed, so a lead you have moved to CONTACT does not get
        reset to NEW the next time the source lists it.
        """
        key = dedupe_key(lead)
        stamp = (seen_at or date.today()).isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        signals = json.dumps(_signals_of(lead))
        source = lead.source or "unknown"

        with closing(self.connection.cursor()) as cur:
            cur.execute("SELECT id, first_seen FROM properties WHERE dedupe_key = ?", (key,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """INSERT INTO properties
                       (dedupe_key, address, city, state, zip_code, county,
                        property_type, beds, baths, sqft, year_built,
                        first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, lead.address, lead.city, lead.state, lead.zip_code,
                        lead.county, str(lead.property_type), lead.beds, lead.baths,
                        lead.sqft, lead.year_built, stamp, stamp,
                    ),
                )
                property_id = cur.lastrowid
            else:
                property_id = row["id"]
                # Backfill attributes a later, richer sighting supplied.
                cur.execute(
                    """UPDATE properties SET last_seen = ?,
                           address = COALESCE(NULLIF(?, ''), address),
                           city = COALESCE(NULLIF(?, ''), city),
                           county = COALESCE(NULLIF(?, ''), county),
                           zip_code = COALESCE(NULLIF(?, ''), zip_code),
                           beds = COALESCE(?, beds), baths = COALESCE(?, baths),
                           sqft = COALESCE(?, sqft),
                           year_built = COALESCE(?, year_built)
                       WHERE id = ?""",
                    (
                        stamp, lead.address, lead.city, lead.county, lead.zip_code,
                        lead.beds, lead.baths, lead.sqft, lead.year_built, property_id,
                    ),
                )

            cur.execute(
                "SELECT id, times_seen, status FROM leads WHERE property_id = ? AND source = ?",
                (property_id, source),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    """INSERT INTO leads
                       (property_id, lead_id, source, status, first_seen, last_seen,
                        times_seen, lead_score, deal_score, asking_price,
                        estimated_value, estimated_repairs, estimated_equity,
                        occupancy, condition, signals_json, final_decision, notes)
                       VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        property_id, lead.lead_id, source, status or STATUS_NEW,
                        stamp, stamp, lead_score, deal_score, lead.asking_price,
                        lead.estimated_value, lead.estimated_repairs,
                        lead.estimated_equity, str(lead.occupancy),
                        str(lead.condition), signals, final_decision, lead.notes,
                    ),
                )
                lead_row_id = cur.lastrowid
            else:
                lead_row_id = existing["id"]
                cur.execute(
                    """UPDATE leads SET last_seen = ?, times_seen = times_seen + 1,
                           lead_id = COALESCE(NULLIF(?, ''), lead_id),
                           status = ?, lead_score = ?, deal_score = ?,
                           asking_price = ?, estimated_value = ?,
                           estimated_repairs = ?, estimated_equity = ?,
                           occupancy = ?, condition = ?, signals_json = ?,
                           final_decision = ?
                       WHERE id = ?""",
                    (
                        stamp, lead.lead_id, status or existing["status"], lead_score,
                        deal_score, lead.asking_price, lead.estimated_value,
                        lead.estimated_repairs, lead.estimated_equity,
                        str(lead.occupancy), str(lead.condition), signals,
                        final_decision, lead_row_id,
                    ),
                )

            cur.execute(
                """INSERT INTO lead_history
                   (lead_row_id, seen_at, lead_score, deal_score, asking_price,
                    estimated_value, estimated_repairs, signals_json, change_summary)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    lead_row_id, now, lead_score, deal_score, lead.asking_price,
                    lead.estimated_value, lead.estimated_repairs, signals, change_summary,
                ),
            )
        self.connection.commit()
        stored = self.get(key, source)
        assert stored is not None  # just written
        return stored

    def set_status(self, lead_row_id: int, status: str) -> None:
        """Move a lead through the workflow."""
        if status not in LEAD_STATUSES:
            raise ValueError(
                f"unknown status '{status}'. Valid: {', '.join(LEAD_STATUSES)}"
            )
        self.connection.execute(
            "UPDATE leads SET status = ? WHERE id = ?", (status, lead_row_id)
        )
        self.connection.commit()

    def bulk_status(self, lead_row_ids: Iterable[int], status: str) -> int:
        ids = list(lead_row_ids)
        for lead_row_id in ids:
            self.set_status(lead_row_id, status)
        return len(ids)
