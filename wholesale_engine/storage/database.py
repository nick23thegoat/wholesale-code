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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..lead_hunter.models import Lead
from ..lead_hunter.normalizer import normalize_lead

# --- the deal watchlist ----------------------------------------------------
#
# One lead moves through these in roughly this order. Nothing enforces the
# order — deals skip steps and go backwards — but every move is recorded, so
# the history answers "what happened to that one?" months later.
#
#   NEW -> WATCH -> HOT -> CONTACT -> OFFER_SENT -> UNDER_CONTRACT -> ASSIGNED
#                                  -> PASSED / DEAD at any point

STATUS_NEW = "NEW"
STATUS_WATCH = "WATCH"
STATUS_RESEARCHED = "RESEARCHED"
STATUS_HOT = "HOT"
STATUS_CONTACT = "CONTACT"
STATUS_OFFER_SENT = "OFFER_SENT"
STATUS_UNDER_CONTRACT = "UNDER_CONTRACT"
STATUS_ASSIGNED = "ASSIGNED"
STATUS_CLOSED = "CLOSED"
STATUS_PASSED = "PASSED"
STATUS_DEAD = "DEAD"

LEAD_STATUSES = (
    STATUS_NEW,
    STATUS_WATCH,
    STATUS_RESEARCHED,
    STATUS_HOT,
    STATUS_CONTACT,
    STATUS_OFFER_SENT,
    STATUS_UNDER_CONTRACT,
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_PASSED,
    STATUS_DEAD,
)

#: The analyzer's green light, compared exactly. "GO" is a substring of
#: "NEGOTIATE", so substring matching on a decision is always a bug.
DECISION_GO = "🔥 GO"

#: Statuses that mean "stop working this lead".
CLOSED_STATUSES = (STATUS_PASSED, STATUS_DEAD, STATUS_CLOSED)

#: Statuses that mean "this is live and being worked".
ACTIVE_STATUSES = (
    STATUS_WATCH,
    STATUS_HOT,
    STATUS_CONTACT,
    STATUS_OFFER_SENT,
    STATUS_UNDER_CONTRACT,
)

#: The default status ordering used when sorting a watchlist for display:
#: furthest along first.
STATUS_ORDER = {name: index for index, name in enumerate(LEAD_STATUSES)}


# --- activity types --------------------------------------------------------

ACTIVITY_LEAD_CREATED = "lead_created"
ACTIVITY_LEAD_UPDATED = "lead_updated"
ACTIVITY_SCORE_CHANGED = "score_changed"
ACTIVITY_PRICE_CHANGED = "price_changed"
ACTIVITY_STATUS_CHANGED = "status_changed"
ACTIVITY_NOTE_ADDED = "note_added"
ACTIVITY_RESEARCH_COMPLETED = "research_completed"
ACTIVITY_OFFER_CALCULATED = "offer_calculated"

ACTIVITY_TYPES = (
    ACTIVITY_LEAD_CREATED,
    ACTIVITY_LEAD_UPDATED,
    ACTIVITY_SCORE_CHANGED,
    ACTIVITY_PRICE_CHANGED,
    ACTIVITY_STATUS_CHANGED,
    ACTIVITY_NOTE_ADDED,
    ACTIVITY_RESEARCH_COMPLETED,
    ACTIVITY_OFFER_CALCULATED,
)

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
    priority_score      REAL,
    priority_band       TEXT NOT NULL DEFAULT '',
    arv                 REAL,
    repair_estimate     REAL,
    mao                 REAL,
    recommended_offer   REAL,
    potential_fee       REAL,
    fee_status          TEXT NOT NULL DEFAULT '',
    arv_confidence      TEXT NOT NULL DEFAULT '',
    comp_confidence     TEXT NOT NULL DEFAULT '',
    equity_amount       REAL,
    equity_percentage   REAL,
    equity_status       TEXT NOT NULL DEFAULT '',
    distress_count      INTEGER NOT NULL DEFAULT 0,
    days_on_market      INTEGER,
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

CREATE TABLE IF NOT EXISTS status_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_row_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    from_status         TEXT NOT NULL DEFAULT '',
    to_status           TEXT NOT NULL,
    changed_at          TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_row_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL,
    author              TEXT NOT NULL DEFAULT '',
    body                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_row_id         INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    property_id         TEXT NOT NULL DEFAULT '',
    activity_type       TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_properties_dedupe ON properties(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_priority ON leads(priority_score);
CREATE INDEX IF NOT EXISTS idx_history_lead ON lead_history(lead_row_id);
CREATE INDEX IF NOT EXISTS idx_status_history_lead ON status_history(lead_row_id);
CREATE INDEX IF NOT EXISTS idx_notes_lead ON notes(lead_row_id);
CREATE INDEX IF NOT EXISTS idx_activities_lead ON activities(lead_row_id);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(activity_type);
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
class LeadSnapshot:
    """The analysis + research figures for one sighting.

    Everything defaults to ``None`` so a raw, unanalyzed lead stores nothing
    rather than a zero that would later read as a real answer.
    """

    priority_score: Optional[float] = None
    priority_band: str = ""
    arv: Optional[float] = None
    repair_estimate: Optional[float] = None
    mao: Optional[float] = None
    recommended_offer: Optional[float] = None
    potential_fee: Optional[float] = None
    fee_status: str = ""
    arv_confidence: str = ""
    comp_confidence: str = ""
    equity_amount: Optional[float] = None
    equity_percentage: Optional[float] = None
    equity_status: str = ""
    distress_count: int = 0
    days_on_market: Optional[int] = None
    researched: bool = False
    research_note: str = ""


@dataclass
class SearchQuery:
    """Filters for a local database search. Every field is optional.

    A ``None`` filter is not applied. A record whose value is unknown is only
    excluded by a filter that explicitly targets it — the same rule the lead
    filters follow, so a blank cell never silently loses a lead.
    """

    # --- geography ------------------------------------------------------
    states: Tuple[str, ...] = ()
    counties: Tuple[str, ...] = ()
    cities: Tuple[str, ...] = ()
    zip_codes: Tuple[str, ...] = ()

    # --- shape ----------------------------------------------------------
    property_types: Tuple[str, ...] = ()

    # --- money ----------------------------------------------------------
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_arv: Optional[float] = None
    max_arv: Optional[float] = None
    min_equity: Optional[float] = None
    min_fee: Optional[float] = None

    # --- scores ---------------------------------------------------------
    min_lead_score: Optional[float] = None
    min_deal_score: Optional[float] = None
    min_priority_score: Optional[float] = None

    # --- signals (True = must be reported true) -------------------------
    vacant: Optional[bool] = None
    absentee_owner: Optional[bool] = None
    pre_foreclosure: Optional[bool] = None
    foreclosure: Optional[bool] = None
    tax_delinquent: Optional[bool] = None
    probate: Optional[bool] = None
    inherited: Optional[bool] = None
    code_violation: Optional[bool] = None
    high_equity: Optional[bool] = None
    tired_landlord: Optional[bool] = None

    # --- market / workflow ----------------------------------------------
    min_days_on_market: Optional[int] = None
    max_days_on_market: Optional[int] = None
    statuses: Tuple[str, ...] = ()
    exclude_closed: bool = False
    decisions: Tuple[str, ...] = ()

    #: Free-text match against address, city, county or owner name.
    text: str = ""

    limit: Optional[int] = None
    #: Column to sort by, best-first. One of the SORT_KEYS below.
    sort_by: str = "priority_score"

    def signal_filters(self) -> Dict[str, bool]:
        return {
            name: getattr(self, name)
            for name in SIGNAL_FILTER_FIELDS
            if getattr(self, name) is not None
        }

    def describe(self) -> str:
        parts: List[str] = []
        for label, values in (
            ("states", self.states), ("counties", self.counties),
            ("cities", self.cities), ("zips", self.zip_codes),
            ("types", self.property_types), ("status", self.statuses),
            ("decision", self.decisions),
        ):
            if values:
                parts.append(f"{label}: {', '.join(values)}")
        for label, value in (
            ("price >=", self.min_price), ("price <=", self.max_price),
            ("ARV >=", self.min_arv), ("ARV <=", self.max_arv),
            ("equity >=", self.min_equity), ("fee >=", self.min_fee),
            ("lead score >=", self.min_lead_score),
            ("deal score >=", self.min_deal_score),
            ("priority >=", self.min_priority_score),
            ("DOM >=", self.min_days_on_market),
            ("DOM <=", self.max_days_on_market),
        ):
            if value is not None:
                parts.append(f"{label} {value:,.0f}")
        for name, value in self.signal_filters().items():
            parts.append(f"{name}={'yes' if value else 'no'}")
        if self.text:
            parts.append(f'text "{self.text}"')
        if self.exclude_closed:
            parts.append("open only")
        return "; ".join(parts) or "no filters"


#: Signal fields a SearchQuery can filter on.
SIGNAL_FILTER_FIELDS: Tuple[str, ...] = (
    "vacant", "absentee_owner", "pre_foreclosure", "foreclosure",
    "tax_delinquent", "probate", "inherited", "code_violation",
    "high_equity", "tired_landlord",
)

#: Sortable columns, best-first in every case.
SORT_KEYS: Tuple[str, ...] = (
    "priority_score", "deal_score", "lead_score", "potential_fee",
    "asking_price", "arv", "equity_amount", "days_on_market", "last_seen",
)


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

    # --- Wave 4 research + priority snapshot ---------------------------
    priority_score: Optional[float] = None
    priority_band: str = ""
    arv: Optional[float] = None
    repair_estimate: Optional[float] = None
    mao: Optional[float] = None
    recommended_offer: Optional[float] = None
    potential_fee: Optional[float] = None
    fee_status: str = ""
    arv_confidence: str = ""
    comp_confidence: str = ""
    equity_amount: Optional[float] = None
    equity_percentage: Optional[float] = None
    equity_status: str = ""
    distress_count: int = 0
    days_on_market: Optional[int] = None
    county: str = ""
    property_type: str = ""

    @property
    def is_new(self) -> bool:
        return self.times_seen <= 1

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    def signal(self, name: str) -> Optional[bool]:
        return self.signals.get(name)

    def display_id(self) -> str:
        return self.dedupe_key.split("|")[0] or self.address


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
            priority_score=row["priority_score"],
            priority_band=row["priority_band"] or "",
            arv=row["arv"],
            repair_estimate=row["repair_estimate"],
            mao=row["mao"],
            recommended_offer=row["recommended_offer"],
            potential_fee=row["potential_fee"],
            fee_status=row["fee_status"] or "",
            arv_confidence=row["arv_confidence"] or "",
            comp_confidence=row["comp_confidence"] or "",
            equity_amount=row["equity_amount"],
            equity_percentage=row["equity_percentage"],
            equity_status=row["equity_status"] or "",
            distress_count=row["distress_count"] or 0,
            days_on_market=row["days_on_market"],
            county=row["county"] or "",
            property_type=row["property_type"] or "",
        )

    _SELECT = """
        SELECT l.id AS lead_row_id, p.id AS property_row_id, p.dedupe_key,
               p.address, p.city, p.state, p.zip_code,
               l.source, l.status, l.first_seen, l.last_seen, l.times_seen,
               l.lead_score, l.deal_score, l.asking_price, l.estimated_value,
               l.estimated_repairs, l.estimated_equity, l.signals_json,
               l.final_decision, l.priority_score, l.priority_band,
               l.arv, l.repair_estimate, l.mao, l.recommended_offer,
               l.potential_fee, l.fee_status, l.arv_confidence, l.comp_confidence,
               l.equity_amount, l.equity_percentage, l.equity_status,
               l.distress_count, l.days_on_market,
               p.county, p.property_type
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
        snapshot: Optional["LeadSnapshot"] = None,
    ) -> StoredLead:
        """Insert or update this property, appending a history snapshot.

        ``first_seen`` is never moved. ``status`` is only set on insert unless
        explicitly passed, so a lead you have moved to CONTACT does not get
        reset to NEW the next time the source lists it.

        ``snapshot`` carries the analysis and priority figures for this
        sighting. It is optional: a raw lead with no analysis stores None in
        every one of those columns rather than a misleading zero.
        """
        snap = snapshot or LeadSnapshot()
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
                self._log_activity(
                    cur,
                    lead_row_id,
                    key,
                    ACTIVITY_LEAD_CREATED,
                    f"Lead first seen from {source}"
                    + (f" at {lead.asking_price:,.0f}" if lead.asking_price else ""),
                    now,
                )
                cur.execute(
                    """INSERT INTO status_history
                       (lead_row_id, from_status, to_status, changed_at, reason)
                       VALUES (?,?,?,?,?)""",
                    (lead_row_id, "", status or STATUS_NEW, now, "first sighting"),
                )
            else:
                lead_row_id = existing["id"]
                self._log_activity(
                    cur, lead_row_id, key, ACTIVITY_LEAD_UPDATED,
                    change_summary or f"Seen again from {source}", now,
                )
                if change_summary:
                    if "PRICE" in change_summary:
                        self._log_activity(
                            cur, lead_row_id, key, ACTIVITY_PRICE_CHANGED,
                            change_summary, now,
                        )
                    if "SCORE" in change_summary:
                        self._log_activity(
                            cur, lead_row_id, key, ACTIVITY_SCORE_CHANGED,
                            change_summary, now,
                        )
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
                """UPDATE leads SET
                       priority_score = ?, priority_band = ?, arv = ?,
                       repair_estimate = ?, mao = ?, recommended_offer = ?,
                       potential_fee = ?, fee_status = ?, arv_confidence = ?,
                       comp_confidence = ?, equity_amount = ?, equity_percentage = ?,
                       equity_status = ?, distress_count = ?, days_on_market = ?
                   WHERE id = ?""",
                (
                    snap.priority_score, snap.priority_band, snap.arv,
                    snap.repair_estimate, snap.mao, snap.recommended_offer,
                    snap.potential_fee, snap.fee_status, snap.arv_confidence,
                    snap.comp_confidence, snap.equity_amount, snap.equity_percentage,
                    snap.equity_status, snap.distress_count,
                    snap.days_on_market if snap.days_on_market is not None
                    else lead.days_on_market,
                    lead_row_id,
                ),
            )
            if snap.researched:
                self._log_activity(
                    cur, lead_row_id, key, ACTIVITY_RESEARCH_COMPLETED,
                    snap.research_note or "Research pass completed", now,
                )
            if snap.mao is not None:
                self._log_activity(
                    cur, lead_row_id, key, ACTIVITY_OFFER_CALCULATED,
                    f"MAO {snap.mao:,.0f}"
                    + (f", offer {snap.recommended_offer:,.0f}" if snap.recommended_offer else "")
                    + (f", fee {snap.potential_fee:,.0f} ({snap.fee_status})"
                       if snap.potential_fee is not None else ""),
                    now,
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

    def set_status(self, lead_row_id: int, status: str, reason: str = "") -> None:
        """Move a lead through the watchlist, recording where it came from.

        Every move is appended to ``status_history`` and to the activity log,
        so the dossier can show the whole path months later. A no-op move
        (same status) is not recorded — it is not a change.
        """
        if status not in LEAD_STATUSES:
            raise ValueError(
                f"unknown status '{status}'. Valid: {', '.join(LEAD_STATUSES)}"
            )
        row = self.connection.execute(
            "SELECT status FROM leads WHERE id = ?", (lead_row_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no lead with row id {lead_row_id}")
        previous = row["status"]
        if previous == status:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self.connection.cursor()) as cur:
            cur.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_row_id))
            cur.execute(
                """INSERT INTO status_history
                   (lead_row_id, from_status, to_status, changed_at, reason)
                   VALUES (?,?,?,?,?)""",
                (lead_row_id, previous, status, now, reason),
            )
            self._log_activity(
                cur, lead_row_id, "", ACTIVITY_STATUS_CHANGED,
                f"{previous} -> {status}" + (f": {reason}" if reason else ""), now,
            )
        self.connection.commit()

    def status_history(self, lead_row_id: int) -> List[Dict[str, Any]]:
        """Every status this lead has been through, oldest first."""
        rows = self.connection.execute(
            "SELECT * FROM status_history WHERE lead_row_id = ? ORDER BY changed_at, id",
            (lead_row_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def add_note(self, lead_row_id: int, body: str, author: str = "") -> int:
        """Attach a note. Yours, free-text, never generated by the engine."""
        text = (body or "").strip()
        if not text:
            raise ValueError("a note needs some text")
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self.connection.cursor()) as cur:
            cur.execute(
                "INSERT INTO notes (lead_row_id, created_at, author, body) VALUES (?,?,?,?)",
                (lead_row_id, now, author, text),
            )
            note_id = cur.lastrowid
            self._log_activity(
                cur, lead_row_id, "", ACTIVITY_NOTE_ADDED,
                text if len(text) <= 80 else text[:77] + "...", now,
            )
        self.connection.commit()
        return note_id

    def notes(self, lead_row_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM notes WHERE lead_row_id = ? ORDER BY created_at, id",
            (lead_row_id,),
        )
        return [dict(r) for r in rows]

    def delete_note(self, note_id: int) -> bool:
        cur = self.connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.connection.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    @staticmethod
    def _log_activity(
        cur, lead_row_id: Optional[int], property_id: str,
        activity_type: str, description: str, when: str,
    ) -> None:
        cur.execute(
            """INSERT INTO activities
               (lead_row_id, property_id, activity_type, description, created_at)
               VALUES (?,?,?,?,?)""",
            (lead_row_id, property_id, activity_type, description, when),
        )

    def log_activity(
        self,
        lead_row_id: Optional[int],
        activity_type: str,
        description: str,
        property_id: str = "",
    ) -> None:
        """Record an activity by hand. Type must be a known one."""
        if activity_type not in ACTIVITY_TYPES:
            raise ValueError(
                f"unknown activity '{activity_type}'. Valid: {', '.join(ACTIVITY_TYPES)}"
            )
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self.connection.cursor()) as cur:
            self._log_activity(cur, lead_row_id, property_id, activity_type, description, now)
        self.connection.commit()

    def activities(
        self, lead_row_id: int, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Activity for one lead, newest first."""
        sql = "SELECT * FROM activities WHERE lead_row_id = ? ORDER BY created_at DESC, id DESC"
        params: List[Any] = [lead_row_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.connection.execute(sql, params)]

    def recent_activity(self, limit: int = 50) -> List[Dict[str, Any]]:
        """The whole log, newest first — what happened across every lead."""
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM activities ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        ]

    def bulk_status(self, lead_row_ids: Iterable[int], status: str, reason: str = "") -> int:
        ids = list(lead_row_ids)
        for lead_row_id in ids:
            self.set_status(lead_row_id, status, reason)
        return len(ids)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: Optional[SearchQuery] = None) -> List[StoredLead]:
        """Filter the stored leads. Everything the CLI can ask for lives here.

        Geography, price, scores and workflow status are pushed into SQL;
        signal filters run in Python because the signals are stored as one
        JSON blob (there are ten of them, they change together, and a lead
        list is small enough that this is never the slow part).
        """
        query = query or SearchQuery()
        sql = self._SELECT
        clauses: List[str] = []
        params: List[Any] = []

        def any_of(column: str, values: Tuple[str, ...], lower: bool = True) -> None:
            if not values:
                return
            placeholders = ", ".join("?" for _ in values)
            target = f"LOWER({column})" if lower else column
            clauses.append(f"{target} IN ({placeholders})")
            params.extend(v.strip().lower() if lower else v.strip() for v in values)

        any_of("p.state", query.states)
        any_of("p.county", query.counties)
        any_of("p.city", query.cities)
        any_of("p.zip_code", query.zip_codes, lower=False)
        any_of("p.property_type", query.property_types)
        any_of("l.status", query.statuses, lower=False)

        for column, value, op in (
            ("l.asking_price", query.min_price, ">="),
            ("l.asking_price", query.max_price, "<="),
            ("l.arv", query.min_arv, ">="),
            ("l.arv", query.max_arv, "<="),
            ("l.equity_amount", query.min_equity, ">="),
            ("l.potential_fee", query.min_fee, ">="),
            ("l.lead_score", query.min_lead_score, ">="),
            ("l.deal_score", query.min_deal_score, ">="),
            ("l.priority_score", query.min_priority_score, ">="),
            ("l.days_on_market", query.min_days_on_market, ">="),
            ("l.days_on_market", query.max_days_on_market, "<="),
        ):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)

        if query.exclude_closed:
            placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
            clauses.append(f"l.status NOT IN ({placeholders})")
            params.extend(CLOSED_STATUSES)

        if query.decisions:
            decision_clauses = []
            for decision in query.decisions:
                decision_clauses.append("l.final_decision LIKE ?")
                params.append(f"%{decision}%")
            clauses.append("(" + " OR ".join(decision_clauses) + ")")

        if query.text:
            needle = f"%{query.text.strip().lower()}%"
            clauses.append(
                "(LOWER(p.address) LIKE ? OR LOWER(p.city) LIKE ? "
                "OR LOWER(p.county) LIKE ? OR LOWER(l.notes) LIKE ?)"
            )
            params.extend([needle] * 4)

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        sort = query.sort_by if query.sort_by in SORT_KEYS else "priority_score"
        column = "l." + sort if sort != "last_seen" else "l.last_seen"
        # NULLs last, then best-first, with a stable tiebreak.
        sql += (
            f" ORDER BY ({column} IS NULL), {column} DESC, "
            "l.deal_score DESC, l.lead_score DESC, p.address ASC"
        )

        rows = [self._row_to_stored(r) for r in self.connection.execute(sql, params)]

        signal_filters = query.signal_filters()
        if signal_filters:
            rows = [
                row for row in rows
                if all(row.signal(name) is wanted for name, wanted in signal_filters.items())
            ]
        if query.limit:
            rows = rows[: query.limit]
        return rows

    def top_deals(self, limit: int = 20, exclude_closed: bool = True) -> List[StoredLead]:
        """Best deals by priority, then deal score. Analyzed leads only."""
        rows = self.search(
            SearchQuery(sort_by="priority_score", exclude_closed=exclude_closed)
        )
        rows = [r for r in rows if r.deal_score is not None]
        return rows[:limit]

    def hot_leads(self, limit: Optional[int] = None) -> List[StoredLead]:
        """Leads worth calling today.

        Sorted by priority, deal score, lead score, then the fee — the order
        the spec asks for. A lead you have already marked PASSED or DEAD is
        excluded; one you have moved forward to CONTACT or beyond is not,
        because those are exactly the ones still in play.
        """
        rows = [
            row
            for row in self.search(SearchQuery(exclude_closed=True))
            if self._is_hot(row)
        ]
        rows.sort(
            key=lambda r: (
                -(r.priority_score if r.priority_score is not None else -1.0),
                -(r.deal_score if r.deal_score is not None else -1.0),
                -(r.lead_score if r.lead_score is not None else -1.0),
                -(r.potential_fee if r.potential_fee is not None else -1e12),
            )
        )
        return rows[:limit] if limit else rows

    @staticmethod
    def _is_hot(row: StoredLead) -> bool:
        """Hot means the workflow says so, or the numbers do.

        The decision is matched exactly, never as a substring: "GO" is a
        substring of "NEGOTIATE", which would quietly promote every
        negotiation into the call list.
        """
        if row.status in (STATUS_HOT, STATUS_CONTACT, STATUS_OFFER_SENT):
            return True
        if row.priority_band in ("🔥 PRIORITY", "🟠 HIGH"):
            return True
        return (row.final_decision or "").strip() == DECISION_GO

    def watchlist(self, limit: Optional[int] = None) -> List[StoredLead]:
        """Everything you have actively moved into the pipeline."""
        rows = self.search(SearchQuery(statuses=ACTIVE_STATUSES))
        rows.sort(
            key=lambda r: (
                -STATUS_ORDER.get(r.status, 0),
                -(r.priority_score if r.priority_score is not None else -1.0),
            )
        )
        return rows[:limit] if limit else rows

    def find_one(self, identifier: str) -> Optional[StoredLead]:
        """Look a property up by lead id, property id, dedupe key or address."""
        needle = (identifier or "").strip()
        if not needle:
            return None
        row = self.connection.execute(
            self._SELECT + " WHERE l.lead_id = ? ORDER BY l.last_seen DESC LIMIT 1",
            (needle,),
        ).fetchone()
        if row is not None:
            return self._row_to_stored(row)
        row = self.connection.execute(
            self._SELECT + " WHERE p.dedupe_key = ? ORDER BY l.last_seen DESC LIMIT 1",
            (needle.lower(),),
        ).fetchone()
        if row is not None:
            return self._row_to_stored(row)
        matches = self.search(SearchQuery(text=needle))
        return matches[0] if matches else None

    def status_counts(self) -> Dict[str, int]:
        """How many leads sit at each watchlist status."""
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        )
        counts = {name: 0 for name in LEAD_STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts
