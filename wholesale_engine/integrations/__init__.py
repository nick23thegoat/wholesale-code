"""Outbound adapters: Sheets, CRM, outreach, notifications, AI notes.

Everything here is an interface with a local, credential-free default. Nothing
in this package sends a message, posts to a CRM, or writes to a spreadsheet
you own unless you configure it and ask for it explicitly.
"""

from __future__ import annotations

from .ai_notes import (
    ADVISORY_PREFIX,
    LlmNoteWriter,
    NoteWriter,
    RuleBasedNoteWriter,
    Suggestion,
    get_note_writer,
)
from .base import (
    DeliveryResult,
    Integration,
    IntegrationNotConfigured,
    IntegrationState,
    SendBlocked,
)
from .crm import CrmAdapter, CrmRecord, LocalCrmAdapter, UnconfiguredCrmAdapter, get_crm_adapter
from .notifications import (
    DEFAULT_ENABLED_EVENTS,
    ConsoleNotifier,
    EmailNotifier,
    EventType,
    Notification,
    NotificationAdapter,
    NotificationCenter,
    WebhookNotifier,
)
from .outreach import (
    BULK_THRESHOLD,
    EmailOutreachAdapter,
    Message,
    OutreachAdapter,
    OutreachGate,
    SmsAdapter,
    VoiceAdapter,
    get_outreach_adapter,
)
from .sheets import (
    KEY_COLUMN,
    SHEET_TABS,
    GoogleSheetsAdapter,
    LocalSheetsAdapter,
    SheetsAdapter,
    get_sheets_adapter,
    upsert_rows,
)

__all__ = [
    "ADVISORY_PREFIX",
    "BULK_THRESHOLD",
    "ConsoleNotifier",
    "CrmAdapter",
    "CrmRecord",
    "DEFAULT_ENABLED_EVENTS",
    "DeliveryResult",
    "EmailNotifier",
    "EmailOutreachAdapter",
    "EventType",
    "GoogleSheetsAdapter",
    "Integration",
    "IntegrationNotConfigured",
    "IntegrationState",
    "KEY_COLUMN",
    "LlmNoteWriter",
    "LocalCrmAdapter",
    "LocalSheetsAdapter",
    "Message",
    "Notification",
    "NotificationAdapter",
    "NotificationCenter",
    "NoteWriter",
    "OutreachAdapter",
    "OutreachGate",
    "RuleBasedNoteWriter",
    "SHEET_TABS",
    "SendBlocked",
    "SheetsAdapter",
    "SmsAdapter",
    "Suggestion",
    "UnconfiguredCrmAdapter",
    "VoiceAdapter",
    "WebhookNotifier",
    "get_crm_adapter",
    "get_note_writer",
    "get_outreach_adapter",
    "get_sheets_adapter",
    "upsert_rows",
]


def integration_status() -> str:
    """The BUILT / CONFIGURED / CONNECTED / NOT CONNECTED table."""
    from pathlib import Path

    adapters = [
        ConsoleNotifier(), EmailNotifier(), WebhookNotifier(),
        GoogleSheetsAdapter(), LocalSheetsAdapter(Path(".")),
        UnconfiguredCrmAdapter(), LocalCrmAdapter(),
        SmsAdapter(), EmailOutreachAdapter(), VoiceAdapter(),
        RuleBasedNoteWriter(), LlmNoteWriter(),
    ]
    lines = ["INTEGRATIONS", ""]
    lines.append(f"  {'ADAPTER':<18}{'KIND':<16}{'STATE':<16}NEEDS")
    lines.append("  " + "-" * 74)
    for adapter in adapters:
        missing = ", ".join(adapter.missing_settings()) or "—"
        lines.append(
            f"  {adapter.name:<18}{adapter.kind:<16}{str(adapter.state()):<16}{missing}"
        )
    lines.append("")
    lines.append("  BUILT         the interface exists; nothing implemented behind it")
    lines.append("  CONFIGURED    credentials present, send path not wired")
    lines.append("  CONNECTED     usable right now")
    lines.append("  NOT CONNECTED credentials missing")
    return "\n".join(lines)
