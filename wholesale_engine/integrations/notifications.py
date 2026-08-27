"""Notifications: tell me when something worth knowing happens.

Three adapters. ``console`` needs nothing and is the default; ``email`` and
``webhook`` are interfaces with no credentials wired to them.

Every notification is also written to the activity log, so the record survives
whether or not a channel was configured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .base import DeliveryResult, Integration, IntegrationNotConfigured


class EventType(str, Enum):
    """What is worth interrupting someone for."""

    NEW_HOT_LEAD = "new_hot_lead"
    PRICE_REDUCTION = "price_reduction"
    DEAL_SCORE_UP = "deal_score_increase"
    SELLER_RESPONSE = "seller_response"
    SELLER_COUNTER = "seller_counter"
    OFFER_ACCEPTED = "offer_accepted"
    CONTRACT_DEADLINE = "contract_deadline"
    BUYER_INTEREST = "buyer_interest"

    def __str__(self) -> str:
        return self.value

    @property
    def icon(self) -> str:
        return {
            "new_hot_lead": "🔥",
            "price_reduction": "💰",
            "deal_score_increase": "📈",
            "seller_response": "📞",
            "seller_counter": "💵",
            "offer_accepted": "📝",
            "contract_deadline": "🏠",
            "buyer_interest": "👤",
        }[self.value]

    @classmethod
    def parse(cls, raw: Any) -> "EventType":
        text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        raise ValueError(
            f"unknown event '{raw}'. Valid: {', '.join(m.value for m in cls)}"
        )


#: Events on by default. Everything is configurable — nothing is mandatory.
DEFAULT_ENABLED_EVENTS = (
    EventType.NEW_HOT_LEAD,
    EventType.PRICE_REDUCTION,
    EventType.SELLER_RESPONSE,
    EventType.SELLER_COUNTER,
    EventType.OFFER_ACCEPTED,
    EventType.CONTRACT_DEADLINE,
)


@dataclass
class Notification:
    """One thing worth telling you about."""

    event: EventType
    title: str
    detail: str = ""
    property_id: str = ""
    address: str = ""
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.created_at = self.created_at or datetime.now()

    def render(self) -> str:
        head = f"{self.event.icon}  {self.title}"
        if self.address:
            head += f" — {self.address}"
        return head + (f"\n    {self.detail}" if self.detail else "")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event": str(self.event),
            "title": self.title,
            "detail": self.detail,
            "property_id": self.property_id,
            "address": self.address,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


class NotificationAdapter(Integration):
    kind = "notification"

    def notify(self, notification: Notification) -> DeliveryResult:
        raise NotImplementedError


class ConsoleNotifier(NotificationAdapter):
    """Prints to the terminal. Always available, never fails."""

    name = "console"
    is_local = True

    def __init__(self) -> None:
        super().__init__()
        self.sent: List[Notification] = []

    def notify(self, notification: Notification) -> DeliveryResult:
        self.calls += 1
        self.sent.append(notification)
        return DeliveryResult(
            adapter=self.name, channel="console", sent=True, dry_run=False,
            recipient="terminal", detail=notification.title,
        )


class EmailNotifier(NotificationAdapter):
    """**NOT CONNECTED.** Interface for an email notification service."""

    name = "email"
    required_settings = ("NOTIFY_EMAIL_TO", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")

    def notify(self, notification: Notification) -> DeliveryResult:
        self.require_ready()
        raise IntegrationNotConfigured(
            "Email notifications are BUILT but not implemented. The credentials "
            "are read; wiring the SMTP send is the remaining step."
        )


class WebhookNotifier(NotificationAdapter):
    """**NOT CONNECTED.** Interface for posting notifications to a URL."""

    name = "webhook"
    required_settings = ("NOTIFY_WEBHOOK_URL",)

    def notify(self, notification: Notification) -> DeliveryResult:
        self.require_ready()
        raise IntegrationNotConfigured(
            "Webhook notifications are BUILT but not implemented. Set "
            "NOTIFY_WEBHOOK_URL and wire the POST through SafeHttpClient."
        )


NOTIFIERS = {
    "console": ConsoleNotifier,
    "email": EmailNotifier,
    "webhook": WebhookNotifier,
    "none": ConsoleNotifier,
}


@dataclass
class NotificationCenter:
    """Collects notifications, filters by what is enabled, and delivers them.

    A failing channel never loses the notification: it is still collected and
    still written to the activity log, and the failure is reported.
    """

    adapter: NotificationAdapter = field(default_factory=ConsoleNotifier)
    enabled: Tuple[EventType, ...] = DEFAULT_ENABLED_EVENTS
    collected: List[Notification] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    @classmethod
    def build(
        cls, adapter_name: str = "console", enabled: Optional[Tuple[EventType, ...]] = None
    ) -> "NotificationCenter":
        factory = NOTIFIERS.get((adapter_name or "console").strip().lower())
        if factory is None:
            raise ValueError(
                f"unknown notification adapter '{adapter_name}'. "
                f"Available: {', '.join(NOTIFIERS)}"
            )
        return cls(adapter=factory(), enabled=enabled or DEFAULT_ENABLED_EVENTS)

    def is_enabled(self, event: EventType) -> bool:
        return event in self.enabled

    def push(
        self,
        event: EventType,
        title: str,
        detail: str = "",
        property_id: str = "",
        address: str = "",
    ) -> Optional[Notification]:
        """Record a notification and try to deliver it."""
        if not self.is_enabled(event):
            return None
        notification = Notification(
            event=event, title=title, detail=detail,
            property_id=property_id, address=address,
        )
        self.collected.append(notification)
        try:
            self.adapter.notify(notification)
        except (IntegrationNotConfigured, NotImplementedError) as exc:
            message = f"{self.adapter.name}: {exc}"
            if message not in self.failures:
                self.failures.append(message)
        return notification

    def render(self) -> str:
        if not self.collected:
            return "NOTIFICATIONS\n  nothing to report."
        lines = [f"NOTIFICATIONS ({len(self.collected)})"]
        for notification in self.collected:
            lines.append("  " + notification.render().replace("\n", "\n  "))
        for failure in self.failures:
            lines.append(f"  (undelivered — {failure})")
        return "\n".join(lines)
