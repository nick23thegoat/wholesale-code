"""Output adapters: CSV and JSON today, Sheets/CRM later."""

from __future__ import annotations

from .adapters import (
    CsvAdapter,
    GoogleSheetsAdapter,
    JsonAdapter,
    OutputAdapter,
    publish_all,
)

__all__ = [
    "CsvAdapter",
    "GoogleSheetsAdapter",
    "JsonAdapter",
    "OutputAdapter",
    "publish_all",
]
