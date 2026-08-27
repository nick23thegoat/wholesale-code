"""Where finished results go.

CSV and JSON are implemented and are the working outputs. Google Sheets is a
declared seam with no credentials and no implementation — it raises rather than
silently doing nothing, so a missing integration can never be mistaken for a
successful publish.

Adding a destination (Sheets, a CRM, a webhook) means implementing
:class:`OutputAdapter` and registering it. Nothing upstream changes.
"""

from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


class OutputAdapter(ABC):
    """Publishes rows somewhere."""

    name: str = "unnamed"
    #: False when the destination needs credentials that are not configured.
    available: bool = True

    @abstractmethod
    def publish(
        self, rows: Sequence[Dict[str, Any]], columns: Sequence[str], label: str
    ) -> Optional[Path]:
        """Write ``rows``. Returns a path when the destination is a file."""


class CsvAdapter(OutputAdapter):
    """One CSV file per named output."""

    name = "csv"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path_for(self, label: str) -> Path:
        return self.directory / f"{label}.csv"

    def publish(
        self, rows: Sequence[Dict[str, Any]], columns: Sequence[str], label: str
    ) -> Path:
        destination = self.path_for(label)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return destination


class JsonAdapter(OutputAdapter):
    """One JSON document per named output, with a small run header."""

    name = "json"

    def __init__(self, directory: Path, meta: Optional[Dict[str, Any]] = None) -> None:
        self.directory = Path(directory)
        self.meta = meta or {}

    def path_for(self, label: str) -> Path:
        return self.directory / f"{label}.json"

    def publish(
        self, rows: Sequence[Dict[str, Any]], columns: Sequence[str], label: str
    ) -> Path:
        destination = self.path_for(label)
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "generated_on": date.today().isoformat(),
            "output": label,
            "columns": list(columns),
            "count": len(rows),
            **self.meta,
            "rows": [{c: row.get(c) for c in columns} for row in rows],
        }
        destination.write_text(
            json.dumps(document, indent=2, default=str), encoding="utf-8"
        )
        return destination


class GoogleSheetsAdapter(OutputAdapter):
    """Seam for a future Google Sheets push. **Not implemented.**

    Deliberately unimplemented rather than stubbed to a no-op: a silent
    success would be worse than an error. Wiring this up needs a service
    account, a shared sheet, and the ``google-api-python-client`` dependency —
    none of which this engine requires today.
    """

    name = "google-sheets"
    available = False

    def __init__(self, spreadsheet_id: Optional[str] = None) -> None:
        self.spreadsheet_id = spreadsheet_id

    def publish(
        self, rows: Sequence[Dict[str, Any]], columns: Sequence[str], label: str
    ) -> Optional[Path]:
        raise NotImplementedError(
            "Google Sheets output is not connected. It needs a service account and "
            "a shared spreadsheet. CSV and JSON outputs are fully functional in the "
            "meantime and import into Sheets directly."
        )


def publish_all(
    adapters: Iterable[OutputAdapter],
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    label: str,
) -> List[Path]:
    """Publish one dataset through every available adapter."""
    written: List[Path] = []
    for adapter in adapters:
        if not adapter.available:
            continue
        result = adapter.publish(rows, columns, label)
        if result is not None:
            written.append(result)
    return written
