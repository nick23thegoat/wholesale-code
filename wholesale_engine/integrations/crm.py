"""A generic CRM interface. **NOT CONNECTED** to any specific CRM.

Six operations, which is what every CRM has in common:

    create_contact · update_contact · create_deal · update_deal
    create_note    · create_task

No vendor is hard-coded. Connecting one means implementing :class:`CrmAdapter`
against that CRM's published API, registering it, and setting ``CRM_PROVIDER``.

Until then :class:`LocalCrmAdapter` writes the same records to JSON, so the
call sites are exercised and the payload shapes are pinned by tests. CSV and
JSON exports remain the fallback and always work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import DeliveryResult, Integration, IntegrationNotConfigured


@dataclass
class CrmRecord:
    """One record handed to a CRM. Deliberately CRM-agnostic."""

    kind: str  # contact | deal | note | task
    external_id: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.created_at = self.created_at or datetime.now()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "external_id": self.external_id,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            **self.fields,
        }


class CrmAdapter(Integration):
    """The six operations a CRM integration must provide."""

    kind = "crm"

    def create_contact(self, **fields: Any) -> CrmRecord:
        raise NotImplementedError

    def update_contact(self, external_id: str, **fields: Any) -> CrmRecord:
        raise NotImplementedError

    def create_deal(self, **fields: Any) -> CrmRecord:
        raise NotImplementedError

    def update_deal(self, external_id: str, **fields: Any) -> CrmRecord:
        raise NotImplementedError

    def create_note(self, **fields: Any) -> CrmRecord:
        raise NotImplementedError

    def create_task(self, **fields: Any) -> CrmRecord:
        raise NotImplementedError


class UnconfiguredCrmAdapter(CrmAdapter):
    """The default. Refuses every operation and says what is missing."""

    name = "none"
    required_settings = ("CRM_API_KEY", "CRM_BASE_URL")

    def _refuse(self, operation: str) -> CrmRecord:
        raise IntegrationNotConfigured(
            f"No CRM is connected, so {operation} did nothing. Set CRM_PROVIDER "
            "and its credentials, or use the CSV/JSON exports, which work today."
        )

    def create_contact(self, **fields: Any) -> CrmRecord:
        return self._refuse("create_contact")

    def update_contact(self, external_id: str, **fields: Any) -> CrmRecord:
        return self._refuse("update_contact")

    def create_deal(self, **fields: Any) -> CrmRecord:
        return self._refuse("create_deal")

    def update_deal(self, external_id: str, **fields: Any) -> CrmRecord:
        return self._refuse("update_deal")

    def create_note(self, **fields: Any) -> CrmRecord:
        return self._refuse("create_note")

    def create_task(self, **fields: Any) -> CrmRecord:
        return self._refuse("create_task")


class LocalCrmAdapter(CrmAdapter):
    """Writes CRM-shaped records to a local JSON file.

    Not a CRM. It exists so the call sites, payload shapes and de-duplication
    are exercised before a real CRM is chosen, and so nothing is lost in the
    meantime.
    """

    name = "local-crm"
    is_local = True

    def __init__(self, path: Optional[Path] = None) -> None:
        super().__init__()
        self.path = Path(path) if path else None
        self.records: List[CrmRecord] = []

    def _record(self, kind: str, external_id: str, fields: Dict[str, Any]) -> CrmRecord:
        self.calls += 1
        record = CrmRecord(kind=kind, external_id=external_id, fields=fields)
        # Same kind + same external id updates rather than appending.
        for position, existing in enumerate(self.records):
            if existing.kind == kind and existing.external_id == external_id and external_id:
                merged = dict(existing.fields)
                merged.update(fields)
                record.fields = merged
                self.records[position] = record
                self._flush()
                return record
        self.records.append(record)
        self._flush()
        return record

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([r.as_dict() for r in self.records], indent=2, default=str),
            encoding="utf-8",
        )

    def create_contact(self, **fields: Any) -> CrmRecord:
        return self._record("contact", str(fields.get("property_id", "")), fields)

    def update_contact(self, external_id: str, **fields: Any) -> CrmRecord:
        return self._record("contact", external_id, fields)

    def create_deal(self, **fields: Any) -> CrmRecord:
        return self._record("deal", str(fields.get("property_id", "")), fields)

    def update_deal(self, external_id: str, **fields: Any) -> CrmRecord:
        return self._record("deal", external_id, fields)

    def create_note(self, **fields: Any) -> CrmRecord:
        return self._record("note", "", fields)

    def create_task(self, **fields: Any) -> CrmRecord:
        return self._record("task", "", fields)


CRM_ADAPTERS = {
    "none": UnconfiguredCrmAdapter,
    "local": LocalCrmAdapter,
    "local-crm": LocalCrmAdapter,
}


def get_crm_adapter(name: str = "none", path: Optional[Path] = None) -> CrmAdapter:
    key = (name or "none").strip().lower()
    factory = CRM_ADAPTERS.get(key)
    if factory is None:
        raise ValueError(
            f"unknown CRM adapter '{name}'. Available: {', '.join(CRM_ADAPTERS)}. "
            "No CRM is hard-coded — implement CrmAdapter against your CRM's "
            "published API and register it."
        )
    return LocalCrmAdapter(path) if factory is LocalCrmAdapter else factory()
