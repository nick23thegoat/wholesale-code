"""SMS, email and voice adapters. **Nothing is connected, nothing is sent.**

The interfaces exist so Wave 6+ can plug in a provider without touching the
workflow. What guards them:

* **no adapter sends without an explicit action.** Every send goes through
  :class:`OutreachGate`, which refuses unless the user asked for this specific
  message or turned automation on for this specific channel.
* **no mass messaging.** A batch above :data:`BULK_THRESHOLD` is refused
  outright unless bulk is explicitly enabled, and even then it is capped.
* **the suppression list is absolute.** A number marked DO_NOT_CONTACT,
  WRONG or INVALID is never sent to, whatever the caller asks for.
* **dry run is the default.** An adapter with no credentials records what
  *would* be sent and returns ``sent=False``.

Before connecting a real provider: SMS in the US needs 10DLC registration and
working opt-out handling; email needs CAN-SPAM compliance and a real unsubscribe;
recorded calls need state-by-state consent. The engine does not do any of that
for you and will not pretend otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import DeliveryResult, Integration, IntegrationNotConfigured, SendBlocked

#: A batch larger than this is "mass messaging" and is refused by default.
BULK_THRESHOLD = 5

#: Hard ceiling on a single batch even when bulk is explicitly enabled.
MAX_BULK_BATCH = 50


@dataclass
class Message:
    """One outbound message. Composed by you; never generated and sent."""

    channel: str  # SMS | EMAIL | VOICE
    recipient: str
    body: str
    subject: str = ""
    property_id: str = ""
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.created_at = self.created_at or datetime.now()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "property_id": self.property_id,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass
class OutreachGate:
    """The policy every send has to pass. Defaults refuse everything."""

    #: Per-channel automation. Off means a human has to ask,每 message.
    automation_enabled: Dict[str, bool] = field(default_factory=dict)
    #: Explicit opt-in for batches above BULK_THRESHOLD.
    allow_bulk: bool = False
    #: Values that must never be contacted, whatever the caller says.
    suppressed: Tuple[str, ...] = ()

    def is_suppressed(self, recipient: str) -> bool:
        from ..acquisitions.models import normalize_email, normalize_phone

        candidates = {recipient.strip().lower()}
        digits = normalize_phone(recipient)
        if digits:
            candidates.add(digits)
        email = normalize_email(recipient)
        if email:
            candidates.add(email)
        return any(str(s).strip().lower() in candidates for s in self.suppressed)

    def check(
        self, messages: Sequence[Message], explicit: bool, channel: str
    ) -> None:
        """Raise :class:`SendBlocked` unless this send is allowed."""
        if not messages:
            return
        automated = self.automation_enabled.get(channel.upper(), False)
        if not explicit and not automated:
            raise SendBlocked(
                f"{channel} send refused: nothing is sent without an explicit "
                f"action. Pass explicit=True for a message you asked for, or "
                f"enable automation for {channel} deliberately."
            )
        if len(messages) > BULK_THRESHOLD and not self.allow_bulk:
            raise SendBlocked(
                f"{channel} batch of {len(messages)} refused: anything above "
                f"{BULK_THRESHOLD} is mass messaging and needs allow_bulk set "
                "deliberately. Mass texting without 10DLC registration and "
                "working opt-out handling is not something this engine will do "
                "by accident."
            )
        if len(messages) > MAX_BULK_BATCH:
            raise SendBlocked(
                f"{channel} batch of {len(messages)} exceeds the hard cap of "
                f"{MAX_BULK_BATCH}."
            )
        for message in messages:
            if self.is_suppressed(message.recipient):
                raise SendBlocked(
                    f"{message.recipient} is on the suppression list "
                    "(DO_NOT_CONTACT / WRONG / INVALID). It is never contacted."
                )


class OutreachAdapter(Integration):
    """Base for SMS, email and voice. Records what would be sent."""

    kind = "outreach"
    channel = "OTHER"

    def __init__(self, gate: Optional[OutreachGate] = None) -> None:
        super().__init__()
        self.gate = gate or OutreachGate()
        self.log: List[DeliveryResult] = []

    def send(self, message: Message, explicit: bool = False) -> DeliveryResult:
        return self.send_batch([message], explicit=explicit)[0]

    def send_batch(
        self, messages: Sequence[Message], explicit: bool = False
    ) -> List[DeliveryResult]:
        """Check the policy, then deliver — or record a dry run."""
        self.gate.check(messages, explicit, self.channel)
        results: List[DeliveryResult] = []
        for message in messages:
            self.calls += 1
            results.append(self._deliver(message))
        self.log.extend(results)
        return results

    def _deliver(self, message: Message) -> DeliveryResult:
        """No credentials means a dry run, recorded and clearly marked."""
        if not self.is_configured:
            return DeliveryResult(
                adapter=self.name, channel=self.channel, sent=False, dry_run=True,
                recipient=message.recipient,
                detail=(
                    f"NOT CONNECTED — would have sent: {message.body[:60]}"
                    if message.body else "NOT CONNECTED — nothing sent"
                ),
            )
        raise IntegrationNotConfigured(
            f"{self.name} has credentials but no send implementation. Wiring the "
            "provider's API is the remaining step, and it must not go live until "
            "opt-out handling and the suppression list are honoured end to end."
        )


class SmsAdapter(OutreachAdapter):
    """**NOT CONNECTED.** SMS. Needs 10DLC registration and opt-out handling."""

    name = "sms"
    channel = "SMS"
    required_settings = ("SMS_API_KEY", "SMS_FROM_NUMBER")


class EmailOutreachAdapter(OutreachAdapter):
    """**NOT CONNECTED.** Email. Needs CAN-SPAM compliance and a real unsubscribe."""

    name = "email"
    channel = "EMAIL"
    required_settings = ("EMAIL_API_KEY", "EMAIL_FROM_ADDRESS")


class VoiceAdapter(OutreachAdapter):
    """**NOT CONNECTED.** Voice. Recording consent is state-by-state law."""

    name = "voice"
    channel = "VOICE"
    required_settings = ("VOICE_API_KEY", "VOICE_FROM_NUMBER")


OUTREACH_ADAPTERS = {
    "none": None,
    "sms": SmsAdapter,
    "email": EmailOutreachAdapter,
    "voice": VoiceAdapter,
}


def get_outreach_adapter(
    name: str, gate: Optional[OutreachGate] = None
) -> Optional[OutreachAdapter]:
    key = (name or "none").strip().lower()
    if key not in OUTREACH_ADAPTERS:
        raise ValueError(
            f"unknown outreach adapter '{name}'. Available: "
            f"{', '.join(OUTREACH_ADAPTERS)}"
        )
    factory = OUTREACH_ADAPTERS[key]
    return factory(gate) if factory else None
