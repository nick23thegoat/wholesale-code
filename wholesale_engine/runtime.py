"""TEST vs LIVE: which data the engine is allowed to touch on this run.

Two modes, and the boundary between them is absolute:

``TEST`` (the default)
    Local CSV files and clearly-labelled fictional demo data. No network
    calls to a paid provider, no real contact details, and every fabricated
    record carries a flag that the reports and exports render as TEST DATA.

``LIVE``
    Real provider APIs with your credentials. **Refuses to start** when a
    required credential is missing, rather than silently degrading to test
    data and letting you act on it.

The two never mix. A run is entirely one or the other, the mode is stamped on
the run banner, and :func:`assert_live_ready` is what stops a half-configured
LIVE run from starting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .settings import ProviderSettings, load_dotenv


class RunMode(str, Enum):
    """Which universe of data this run may touch."""

    TEST = "TEST"
    LIVE = "LIVE"

    def __str__(self) -> str:
        return self.value

    @property
    def is_live(self) -> bool:
        return self is RunMode.LIVE

    @classmethod
    def parse(cls, raw: object) -> "RunMode":
        text = str(raw or "").strip().upper()
        if text in ("", "TEST", "DEMO", "SANDBOX", "DEV"):
            return cls.TEST
        if text in ("LIVE", "PROD", "PRODUCTION", "REAL"):
            return cls.LIVE
        raise ValueError(
            f"unknown mode '{raw}'. Valid: TEST (default) or LIVE."
        )


class ModeError(RuntimeError):
    """LIVE mode was requested but the run cannot safely proceed."""


#: Environment variable that selects the mode when ``--mode`` is not given.
MODE_ENV_VAR = "WHOLESALE_MODE"

#: Provider slots the user configures by name. Each maps to a registry entry.
PROVIDER_SLOTS = (
    "DATA_PROVIDER",
    "COMPS_PROVIDER",
    "SKIP_TRACE_PROVIDER",
    "NOTIFICATION_PROVIDER",
    "CRM_PROVIDER",
    "SHEETS_PROVIDER",
    "SMS_PROVIDER",
    "EMAIL_PROVIDER",
    "VOICE_PROVIDER",
    "AI_PROVIDER",
)

#: What each slot falls back to when it is not configured. Every default is a
#: local, credential-free implementation.
SLOT_DEFAULTS: Dict[str, str] = {
    "DATA_PROVIDER": "csv",
    "COMPS_PROVIDER": "csv",
    "SKIP_TRACE_PROVIDER": "none",
    "NOTIFICATION_PROVIDER": "console",
    "CRM_PROVIDER": "none",
    "SHEETS_PROVIDER": "none",
    "SMS_PROVIDER": "none",
    "EMAIL_PROVIDER": "none",
    "VOICE_PROVIDER": "none",
    "AI_PROVIDER": "none",
}

#: Provider names that are local and safe in TEST mode.
LOCAL_PROVIDERS = frozenset(
    {"csv", "none", "console", "mock", "file", "dry-run", "local", "local-crm",
     "local-sheets", "rule-based"}
)

#: Provider names that fabricate data. Allowed in TEST mode only, and every
#: record they produce is flagged.
TEST_ONLY_PROVIDERS = frozenset({"mock"})


@dataclass
class RuntimeConfig:
    """Everything about *how* this run may operate, decided once at startup."""

    mode: RunMode = RunMode.TEST
    settings: ProviderSettings = field(default_factory=ProviderSettings)
    slots: Dict[str, str] = field(default_factory=dict)
    #: Set when the user explicitly opted into unattended bulk actions.
    auto_confirm: bool = False
    #: Warnings collected while resolving the configuration.
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        mode: Optional[str] = None,
        overrides: Optional[Dict[str, str]] = None,
        load_file: bool = True,
        auto_confirm: bool = False,
    ) -> "RuntimeConfig":
        """Resolve the mode and every provider slot.

        Precedence per slot: an explicit ``--source``-style override, then the
        environment (``DATA_PROVIDER=...``), then the local default.
        """
        if load_file:
            load_dotenv()
        resolved_mode = RunMode.parse(
            mode if mode is not None else os.environ.get(MODE_ENV_VAR)
        )
        slots: Dict[str, str] = {}
        for slot in PROVIDER_SLOTS:
            value = (overrides or {}).get(slot) or os.environ.get(slot, "").strip()
            slots[slot] = (value or SLOT_DEFAULTS[slot]).strip().lower()

        config = cls(
            mode=resolved_mode,
            settings=ProviderSettings.from_env(load_file=False),
            slots=slots,
            auto_confirm=auto_confirm,
        )
        config._check()
        return config

    def _check(self) -> None:
        """Collect warnings. LIVE-mode failures are raised separately."""
        for slot, name in self.slots.items():
            if self.mode.is_live and name in TEST_ONLY_PROVIDERS:
                self.warnings.append(
                    f"{slot}={name} fabricates data and cannot be used in LIVE mode."
                )
            if not self.mode.is_live and name not in LOCAL_PROVIDERS:
                self.warnings.append(
                    f"{slot}={name} needs credentials and is inert in TEST mode."
                )

    # ------------------------------------------------------------------

    def slot(self, name: str) -> str:
        return self.slots.get(name, SLOT_DEFAULTS.get(name, "none"))

    @property
    def is_live(self) -> bool:
        return self.mode.is_live

    @property
    def data_provider(self) -> str:
        return self.slot("DATA_PROVIDER")

    @property
    def skip_trace_provider(self) -> str:
        """In TEST mode a configured live tracer is refused, not silently used."""
        name = self.slot("SKIP_TRACE_PROVIDER")
        if not self.is_live and name not in LOCAL_PROVIDERS:
            return "none"
        return name

    def allows_fabricated_data(self) -> bool:
        """Only TEST mode may hold fabricated records."""
        return not self.is_live

    def missing_for_live(self) -> List[str]:
        """Credentials LIVE mode needs but does not have.

        Only the slots actually configured to a remote provider are checked —
        a LIVE run using the CSV data provider and no outreach needs nothing.
        """
        missing: List[str] = []
        if self.data_provider not in LOCAL_PROVIDERS:
            missing.extend(self._data_provider_requirements())
        if self.slot("COMPS_PROVIDER") not in LOCAL_PROVIDERS and not self.settings.has_comps:
            missing.append("COMPS_API_KEY")
        if (
            self.slot("SKIP_TRACE_PROVIDER") not in LOCAL_PROVIDERS
            and not self.settings.has_skip_trace
        ):
            missing.append("SKIP_TRACE_API_KEY")
        # De-duplicate while keeping order.
        seen: List[str] = []
        for name in missing:
            if name not in seen:
                seen.append(name)
        return seen

    def _data_provider_requirements(self) -> List[str]:
        """What the configured data adapter says it needs, and does not have.

        Asked of the registry rather than assumed, so each adapter names its
        own variables — PropertyReach needs PROPERTYREACH_API_KEY, not the
        generic pair. An adapter that is not registered falls back to the
        generic property-data credentials.
        """
        from .providers.registry import registration

        entry = registration(self.data_provider)
        if entry is None:
            return self.settings.missing_for_property_data()
        return entry.missing_settings(self.settings)

    def blocking_problems(self) -> List[str]:
        """Everything that must be fixed before a LIVE run may start."""
        problems: List[str] = []
        for name in self.missing_for_live():
            problems.append(f"{name} is not set")
        for slot, name in self.slots.items():
            if name in TEST_ONLY_PROVIDERS:
                problems.append(
                    f"{slot}={name} produces fabricated data and is refused in LIVE mode"
                )
        return problems

    def assert_live_ready(self) -> None:
        """Raise :class:`ModeError` unless a LIVE run can safely proceed."""
        if not self.is_live:
            return
        problems = self.blocking_problems()
        if problems:
            lines = "\n".join(f"  - {p}" for p in problems)
            raise ModeError(
                "LIVE mode cannot start:\n"
                + lines
                + "\n\nCopy .env.example to .env and fill in the values from your "
                "provider accounts, or run in TEST mode (the default) to work from "
                "local CSV files and clearly-labelled demo data."
            )

    # ------------------------------------------------------------------

    def banner(self) -> str:
        """The run banner. Printed before anything else happens."""
        if self.is_live:
            head = (
                "MODE: LIVE — real provider APIs, real credentials, billable calls."
            )
        else:
            head = (
                "MODE: TEST — local files and clearly-labelled fictional data. "
                "No live provider is contacted."
            )
        lines = [head, f"  data={self.data_provider}"]
        for slot in ("COMPS_PROVIDER", "SKIP_TRACE_PROVIDER", "NOTIFICATION_PROVIDER"):
            lines[-1] += f"  {slot.split('_')[0].lower()}={self.slot(slot)}"
        lines.append(f"  credentials: {self.settings.describe()}")
        for warning in self.warnings:
            lines.append(f"  NOTE: {warning}")
        return "\n".join(lines)


#: Default TEST-mode runtime, for callers that do not build one.
DEFAULT_RUNTIME = RuntimeConfig()
