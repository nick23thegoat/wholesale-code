"""Google Sheets export. **NOT CONNECTED** — the adapter, not the connection.

Sheets needs a service account, a shared spreadsheet and the Google client
libraries, none of which this engine requires. So the adapter is written and
the connection is not made up.

One direction only: **ENGINE -> GOOGLE SHEETS**. Two-way sync is not
implemented and is not planned as a default, because a sheet edited by three
people and a database written by a nightly job disagree in ways nobody can
reconcile safely. If you want your edits to come back, that needs an explicit,
narrow design — not a generic "sync".

Rows are keyed on ``property_id``, so a re-export updates in place rather than
appending duplicates. That upsert logic is finished and tested here against
the local fallback, so wiring the real API is the only remaining work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import DeliveryResult, Integration, IntegrationNotConfigured

#: The tabs the engine knows how to publish.
SHEET_TABS = (
    "hot_leads",
    "contact_queue",
    "follow_ups",
    "offers",
    "pipeline",
)

#: The column every tab is keyed on. Stable across runs, so rows update.
KEY_COLUMN = "property_id"


def upsert_rows(
    existing: Sequence[Dict[str, Any]],
    incoming: Sequence[Dict[str, Any]],
    key: str = KEY_COLUMN,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Merge ``incoming`` into ``existing`` on ``key``.

    Returns ``(rows, updated, added)``. A row whose key already exists is
    replaced in place and keeps its position, so a sheet someone has sorted or
    annotated alongside does not shuffle underneath them. Rows without a key
    are appended and never deduplicated — there is nothing to match them on.
    """
    merged = list(existing)
    index = {
        str(row.get(key)): position
        for position, row in enumerate(merged)
        if row.get(key) not in (None, "")
    }
    updated = added = 0
    for row in incoming:
        identifier = str(row.get(key)) if row.get(key) not in (None, "") else None
        if identifier is not None and identifier in index:
            merged[index[identifier]] = dict(row)
            updated += 1
        else:
            if identifier is not None:
                index[identifier] = len(merged)
            merged.append(dict(row))
            added += 1
    return merged, updated, added


class SheetsAdapter(Integration):
    kind = "sheets"

    def publish(
        self, tab: str, rows: Sequence[Dict[str, Any]], columns: Sequence[str]
    ) -> DeliveryResult:
        raise NotImplementedError


class GoogleSheetsAdapter(SheetsAdapter):
    """**NOT CONNECTED.** Google Sheets, once you supply a service account.

    What is finished: the tab list, the column contracts, and the
    ``property_id`` upsert that keeps a re-export from duplicating rows.

    What is not: the API call. It needs ``google-api-python-client``, a service
    account JSON key, and the spreadsheet shared with that account's email.
    Adding those is the whole remaining job — nothing upstream changes.
    """

    name = "google-sheets"
    required_settings = ("GOOGLE_SHEETS_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

    def __init__(self, spreadsheet_id: Optional[str] = None) -> None:
        super().__init__()
        self.spreadsheet_id = spreadsheet_id

    def publish(
        self, tab: str, rows: Sequence[Dict[str, Any]], columns: Sequence[str]
    ) -> DeliveryResult:
        self.require_ready()
        raise IntegrationNotConfigured(
            "Google Sheets is BUILT but NOT CONNECTED. It needs the "
            "google-api-python-client package, a service-account key, and the "
            "spreadsheet shared with that account. CSV and JSON exports work "
            "today and import into Sheets directly."
        )


class LocalSheetsAdapter(SheetsAdapter):
    """The credential-free fallback: one CSV per tab, upserted on property_id.

    Behaves exactly as the Google adapter will — same tabs, same columns, same
    de-duplication — so the workflow is exercised end to end without an
    account, and swapping in the real adapter changes nothing but the
    destination.
    """

    name = "local-sheets"
    is_local = True

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = Path(directory)

    def path_for(self, tab: str) -> Path:
        return self.directory / f"sheet_{tab}.csv"

    def read(self, tab: str) -> List[Dict[str, Any]]:
        import csv

        path = self.path_for(tab)
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def publish(
        self, tab: str, rows: Sequence[Dict[str, Any]], columns: Sequence[str]
    ) -> DeliveryResult:
        import csv

        self.calls += 1
        merged, updated, added = upsert_rows(self.read(tab), rows)
        path = self.path_for(tab)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in merged:
                writer.writerow(row)
        return DeliveryResult(
            adapter=self.name, channel=tab, sent=True, dry_run=False,
            recipient=str(path),
            detail=f"{added} added, {updated} updated, {len(merged)} total",
        )


SHEETS_ADAPTERS = {
    "none": None,
    "local": LocalSheetsAdapter,
    "google": GoogleSheetsAdapter,
    "google-sheets": GoogleSheetsAdapter,
}


def get_sheets_adapter(
    name: str = "none", directory: Optional[Path] = None
) -> Optional[SheetsAdapter]:
    key = (name or "none").strip().lower()
    if key not in SHEETS_ADAPTERS:
        raise ValueError(
            f"unknown sheets adapter '{name}'. Available: {', '.join(SHEETS_ADAPTERS)}"
        )
    factory = SHEETS_ADAPTERS[key]
    if factory is None:
        return None
    if factory is LocalSheetsAdapter:
        return LocalSheetsAdapter(directory or Path("."))
    return factory()
