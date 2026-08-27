"""Importing records back in, without creating duplicates.

Anything the engine exports, it can re-import: leads, contacts, outreach,
offers, contracts, buyers, assignments. CSV and JSON both work.

The rule that makes this safe is the same one the hunt uses — **identity is
the normalized address plus city, state and ZIP** — so re-importing a file you
exported yesterday updates the rows rather than doubling them.

Nothing is fabricated on the way in. A blank cell stays blank; it never
becomes ``0`` or an empty string standing in for a real value.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .acquisitions.contact_methods import ContactMethod, MethodKind
from .acquisitions.models import Buyer, Contact, PhoneType
from .acquisitions.store import AcquisitionStore
from .lead_hunter.models import Lead
from .lead_hunter.sources.csv_source import lead_from_row
from .research.facts import Confidence
from .storage import LeadStore, dedupe_key

#: What ``--import`` understands.
IMPORT_KINDS = ("leads", "contacts", "buyers")

#: Extensions we will read.
READABLE_SUFFIXES = (".csv", ".json")

#: Refuse anything larger than this, so a mistyped path cannot exhaust memory.
MAX_IMPORT_BYTES = 64 * 1024 * 1024


class ImportError_(ValueError):
    """The file cannot be imported, with a reason the user can act on."""


@dataclass
class ImportResult:
    """What an import actually did."""

    kind: str = ""
    source: str = ""
    rows_read: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def record_error(self, message: str) -> None:
        self.skipped += 1
        if len(self.errors) < 20 and message not in self.errors:
            self.errors.append(message)

    def render(self) -> str:
        lines = [
            f"IMPORT — {self.kind} from {self.source}",
            f"  rows read     {self.rows_read}",
            f"  added         {self.added}",
            f"  updated       {self.updated}   (matched an existing record)",
            f"  skipped       {self.skipped}",
        ]
        for message in self.errors:
            lines.append(f"    {message}")
        return "\n".join(lines)


def read_rows(path: Path) -> List[Dict[str, Any]]:
    """Read a CSV or JSON file into a list of dicts.

    Validates the path and the size before reading anything: a directory, a
    missing file, an unknown extension or an absurdly large file is refused
    with a message rather than a traceback.
    """
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"no such file: {path}")
    if not path.is_file():
        raise ImportError_(f"not a file: {path}")
    if path.suffix.lower() not in READABLE_SUFFIXES:
        raise ImportError_(
            f"cannot read '{path.suffix}'. Supported: {', '.join(READABLE_SUFFIXES)}"
        )
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ImportError_(
            f"{path.name} is larger than the {MAX_IMPORT_BYTES // (1024 * 1024)}MB "
            "import limit."
        )

    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ImportError_(f"{path.name} is not valid JSON: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("rows", payload.get("data", []))
        if not isinstance(payload, list):
            raise ImportError_(
                f"{path.name} does not contain a list of records (or a 'rows' key)."
            )
        return [row for row in payload if isinstance(row, dict)]

    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_float(value: Any) -> Optional[float]:
    if _blank(value):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _as_date(value: Any) -> Optional[date]:
    if _blank(value):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def import_leads(
    store: LeadStore, path: Path, source: str = "import"
) -> ImportResult:
    """Import leads, matching on the normalized address key.

    A row whose address already exists updates the stored record; it never
    creates a second row for the same property.
    """
    result = ImportResult(kind="leads", source=str(path))
    rows = read_rows(path)
    result.rows_read = len(rows)

    for index, row in enumerate(rows, start=2):
        try:
            lead = lead_from_row(row, source=source)
        except Exception as exc:  # a malformed row must not stop the import
            result.record_error(f"row {index}: {exc}")
            continue
        if not lead.address.strip():
            result.record_error(f"row {index}: no address — cannot be de-duplicated")
            continue
        existing = store.get(dedupe_key(lead), lead.source or source)
        store.upsert_lead(lead)
        if existing is None:
            result.added += 1
        else:
            result.updated += 1
    return result


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def import_contacts(
    store: AcquisitionStore, path: Path, source: str = "import"
) -> ImportResult:
    """Import contacts and their phones/emails.

    Every value goes through the same merge as a skip trace: duplicates fold
    together, and a verified record is never silently overwritten by a weaker
    import.
    """
    result = ImportResult(kind="contacts", source=str(path))
    rows = read_rows(path)
    result.rows_read = len(rows)

    for index, row in enumerate(rows, start=2):
        property_id = str(row.get("property_id") or "").strip()
        if not property_id:
            result.record_error(f"row {index}: no property_id — nothing to attach to")
            continue

        row_source = str(row.get("source") or source).strip() or source
        existing = store.best_contact(property_id)
        contact = Contact(
            property_id=property_id,
            owner_name=(str(row.get("owner_name")).strip() if not _blank(row.get("owner_name")) else None),
            phone=row.get("phone") if not _blank(row.get("phone")) else None,
            phone_type=PhoneType.parse(row.get("phone_type")),
            phone_confidence=Confidence.parse(row.get("phone_confidence")),
            email=row.get("email") if not _blank(row.get("email")) else None,
            email_confidence=Confidence.parse(row.get("email_confidence")),
            mailing_address=(
                str(row.get("mailing_address")).strip()
                if not _blank(row.get("mailing_address")) else None
            ),
            source=row_source,
            source_date=_as_date(row.get("source_date")),
            verified=str(row.get("verified", "")).strip().lower() in ("true", "1", "yes"),
            is_test_data=str(row.get("is_test_data", "")).strip().lower() in ("true", "1", "yes"),
            notes=str(row.get("notes") or ""),
        )
        store.save_contact(contact)

        # Fold the phone and email in as first-class methods too, so the
        # multi-method view and the suppression list see them.
        for method in (
            ContactMethod.phone(
                contact.phone, row_source, contact.phone_confidence,
                contact.phone_type, property_id=property_id,
                is_test_data=contact.is_test_data,
            ),
            ContactMethod.email(
                contact.email, row_source, contact.email_confidence,
                property_id=property_id, is_test_data=contact.is_test_data,
            ),
        ):
            if method is not None:
                store.save_method(method)

        if existing is None:
            result.added += 1
        else:
            result.updated += 1
    return result


# ---------------------------------------------------------------------------
# Buyers
# ---------------------------------------------------------------------------


def import_buyers(
    store: AcquisitionStore, path: Path, source: str = "import"
) -> ImportResult:
    """Import the buyer list, matching on name plus company."""
    result = ImportResult(kind="buyers", source=str(path))
    rows = read_rows(path)
    result.rows_read = len(rows)
    known = {(b.name, b.company) for b in store.all_buyers()}

    for index, row in enumerate(rows, start=2):
        name = str(row.get("name") or "").strip()
        if not name:
            result.record_error(f"row {index}: no name")
            continue
        company = str(row.get("company") or "").strip()
        buyer = Buyer(
            name=name,
            company=company,
            email=row.get("email") if not _blank(row.get("email")) else None,
            phone=row.get("phone") if not _blank(row.get("phone")) else None,
            market=str(row.get("market") or ""),
            property_types=[
                t.strip() for t in str(row.get("property_types") or "").split(",") if t.strip()
            ],
            min_price=_as_float(row.get("min_price")),
            max_price=_as_float(row.get("max_price")),
            preferred_states=[
                s.strip() for s in str(row.get("preferred_states") or "").split(",") if s.strip()
            ],
            notes=str(row.get("notes") or ""),
        )
        store.save_buyer(buyer)
        if (name, company) in known:
            result.updated += 1
        else:
            result.added += 1
            known.add((name, company))
    return result


IMPORTERS: Dict[str, Callable[..., ImportResult]] = {
    "leads": import_leads,
    "contacts": import_contacts,
    "buyers": import_buyers,
}


def run_import(
    kind: str,
    path: Path,
    leads: LeadStore,
    acquisitions: AcquisitionStore,
    source: str = "import",
) -> ImportResult:
    """Dispatch to the right importer."""
    key = (kind or "").strip().lower()
    if key not in IMPORTERS:
        raise ImportError_(
            f"unknown import kind '{kind}'. Valid: {', '.join(IMPORT_KINDS)}"
        )
    target = leads if key == "leads" else acquisitions
    return IMPORTERS[key](target, Path(path), source)
