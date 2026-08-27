"""What every outbound integration has in common.

The engine talks to the outside world through adapters. Each one declares
whether it is actually connected, and the default for every slot is a local
implementation that needs no credentials.

Three states, and the reports never blur them:

``BUILT``          the interface exists; no implementation behind it
``CONFIGURED``     credentials are present, but nothing has been sent
``CONNECTED``      credentials present and a health check passed

An adapter that is not connected **raises** rather than silently doing
nothing. A silent no-op that looks like a successful send is worse than an
error, because you would act on it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class IntegrationState(str, Enum):
    BUILT = "BUILT"
    CONFIGURED = "CONFIGURED"
    CONNECTED = "CONNECTED"
    NOT_CONNECTED = "NOT CONNECTED"

    def __str__(self) -> str:
        return self.value


class IntegrationNotConfigured(RuntimeError):
    """The adapter has no credentials, or no implementation behind it."""


class SendBlocked(RuntimeError):
    """A send was refused by policy, not by a missing credential."""


@dataclass
class DeliveryResult:
    """What happened when something was handed to an adapter."""

    adapter: str
    channel: str
    sent: bool = False
    dry_run: bool = True
    recipient: str = ""
    detail: str = ""
    timestamp: Optional[datetime] = None
    external_id: str = ""

    def __post_init__(self) -> None:
        self.timestamp = self.timestamp or datetime.now()

    def render(self) -> str:
        state = "SENT" if self.sent else ("DRY RUN" if self.dry_run else "NOT SENT")
        return f"[{state}] {self.adapter}/{self.channel} -> {self.recipient}: {self.detail}"


class Integration(ABC):
    """Base for every outbound adapter."""

    name: str = "unconfigured"
    #: What this adapter does, for the status table.
    kind: str = "integration"
    #: Environment variables it needs before it can be used.
    required_settings: Tuple[str, ...] = ()
    #: True when the adapter needs no credentials and always works.
    is_local: bool = False

    def __init__(self) -> None:
        self.calls = 0

    # ------------------------------------------------------------------

    def missing_settings(self) -> List[str]:
        import os

        return [n for n in self.required_settings if not os.environ.get(n, "").strip()]

    @property
    def is_configured(self) -> bool:
        return self.is_local or not self.missing_settings()

    def state(self) -> IntegrationState:
        if self.is_local:
            return IntegrationState.CONNECTED
        if not self.required_settings:
            return IntegrationState.BUILT
        return (
            IntegrationState.CONFIGURED
            if self.is_configured
            else IntegrationState.NOT_CONNECTED
        )

    def health_check(self) -> Tuple[bool, str]:
        if self.is_local:
            return True, "local adapter, no credentials needed"
        missing = self.missing_settings()
        if missing:
            return False, "NOT CONNECTED — needs " + ", ".join(missing)
        return False, "credentials present, but no implementation is wired to them"

    def require_ready(self) -> None:
        missing = self.missing_settings()
        if missing:
            raise IntegrationNotConfigured(
                f"{self.name} is NOT CONNECTED: {', '.join(missing)} not set. "
                "Add the values to .env, or leave this integration off — the CSV "
                "and JSON outputs work without it."
            )

    def describe(self) -> str:
        return f"{self.name:<18}{self.kind:<16}{self.state()}"
