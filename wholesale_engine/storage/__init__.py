"""Local persistence: what we have seen before, and what changed."""

from __future__ import annotations

from .changes import Change, ChangeSet, detect_changes
from .database import (
    CLOSED_STATUSES,
    DEFAULT_DB_PATH,
    LEAD_STATUSES,
    STATUS_CONTACT,
    STATUS_DEAD,
    STATUS_HOT,
    STATUS_NEW,
    STATUS_PASSED,
    STATUS_RESEARCHED,
    STATUS_UNDER_CONTRACT,
    LeadStore,
    StoredLead,
    dedupe_key,
)

__all__ = [
    "CLOSED_STATUSES",
    "Change",
    "ChangeSet",
    "DEFAULT_DB_PATH",
    "LEAD_STATUSES",
    "LeadStore",
    "STATUS_CONTACT",
    "STATUS_DEAD",
    "STATUS_HOT",
    "STATUS_NEW",
    "STATUS_PASSED",
    "STATUS_RESEARCHED",
    "STATUS_UNDER_CONTRACT",
    "StoredLead",
    "dedupe_key",
    "detect_changes",
]
