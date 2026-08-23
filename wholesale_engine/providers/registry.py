"""The provider registry: adapters in, no vendor hard-coded.

A provider is registered by name with a factory and a capability declaration.
The application never mentions a vendor — it asks the registry for whatever
``DATA_PROVIDER`` names, and works with whatever capabilities that adapter
declares.

Three entries ship:

``csv``             local files, no credentials, no cost — the TEST-mode default
``propertyreach``   the PropertyReach adapter; needs ``PROPERTYREACH_API_KEY``
``http-template``   a finished transport with no vendor behind it

No vendor is *selected* for you: ``DATA_PROVIDER`` / ``--source`` decides, and
an adapter with no credentials reports NOT CONNECTED rather than pretending.

Adding a real one:

    from wholesale_engine.providers import (
        Capability, HttpPropertyDataProvider, register,
    )

    class MyVendor(HttpPropertyDataProvider):
        name = "myvendor"
        search_path = "properties/search"          # from THEIR documentation
        capabilities = (Capability.SEARCH, Capability.OWNER)

        def build_search_params(self, criteria): ...
        def parse_lead(self, payload): ...

    register("myvendor", MyVendor.from_settings, "My Vendor", MyVendor.capabilities)

Then ``DATA_PROVIDER=myvendor`` in ``.env``. Nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..settings import NO_PROVIDER_MESSAGE, ProviderSettings
from .base import Capability, PropertyDataProvider, ProviderInfo, ProviderNotConfigured
from .csv_provider import CsvProvider
from .http_provider import HttpPropertyDataProvider
from .metrics import ProviderMetrics
from .propertyreach import PropertyReachProvider
from .propertyreach import build as _propertyreach_factory
from .propertyreach_schema import API_KEY_VAR as PROPERTYREACH_API_KEY_VAR

#: factory(settings, csv_path, comps_path, metrics) -> PropertyDataProvider
Factory = Callable[..., PropertyDataProvider]


@dataclass(frozen=True)
class Registration:
    """One registered adapter and everything known about it before construction."""

    name: str
    factory: Factory
    description: str = ""
    capabilities: Tuple[Capability, ...] = ()
    #: False for anything that reaches the network.
    is_local: bool = False
    #: True when the adapter fabricates data (TEST mode only).
    is_test_provider: bool = False
    #: Environment variables the adapter needs before it can be constructed.
    required_settings: Tuple[str, ...] = ()
    #: Where the endpoint and auth contract came from.
    documentation: str = ""

    def missing_settings(self, settings: ProviderSettings) -> List[str]:
        import os

        return [
            name for name in self.required_settings
            if not (os.environ.get(name, "").strip())
        ]

    def is_configured(self, settings: ProviderSettings) -> bool:
        return not self.missing_settings(settings)


_REGISTRY: Dict[str, Registration] = {}


def register(
    name: str,
    factory: Factory,
    description: str = "",
    capabilities: Tuple[Capability, ...] = (),
    is_local: bool = False,
    is_test_provider: bool = False,
    required_settings: Tuple[str, ...] = (),
    documentation: str = "",
) -> None:
    """Add an adapter under ``name``, usable as ``DATA_PROVIDER=<name>``."""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("a provider needs a name")
    _REGISTRY[key] = Registration(
        name=key,
        factory=factory,
        description=description,
        capabilities=tuple(capabilities),
        is_local=is_local,
        is_test_provider=is_test_provider,
        required_settings=tuple(required_settings),
        documentation=documentation,
    )


def unregister(name: str) -> bool:
    return _REGISTRY.pop((name or "").strip().lower(), None) is not None


def registered_names() -> List[str]:
    return sorted(_REGISTRY)


def registration(name: str) -> Optional[Registration]:
    return _REGISTRY.get((name or "").strip().lower())


def supports(name: str, capability: Capability) -> bool:
    """Does the named adapter declare this capability, without constructing it?"""
    entry = registration(name)
    return bool(entry and capability in entry.capabilities)


def providers_for(capability: Capability) -> List[str]:
    """Every registered adapter that declares ``capability``."""
    return sorted(n for n, e in _REGISTRY.items() if capability in e.capabilities)


# ---------------------------------------------------------------------------
# The two adapters that ship
# ---------------------------------------------------------------------------


def _csv_factory(
    settings: ProviderSettings,
    csv_path: Optional[Path] = None,
    comps_path: Optional[Path] = None,
    metrics: Optional[ProviderMetrics] = None,
) -> PropertyDataProvider:
    if csv_path is None:
        raise ProviderNotConfigured(
            "the csv provider needs a file: pass --leads <path> (or --sample-leads)."
        )
    return CsvProvider(csv_path, comps_path, metrics)


def _http_template_factory(
    settings: ProviderSettings,
    csv_path: Optional[Path] = None,
    comps_path: Optional[Path] = None,
    metrics: Optional[ProviderMetrics] = None,
) -> PropertyDataProvider:
    # Raises ProviderNotConfigured unless credentials AND a subclass endpoint
    # exist. That is the intended behaviour: the template is not a provider.
    return HttpPropertyDataProvider(settings, metrics)


register(
    "csv", _csv_factory, CsvProvider.description,
    capabilities=(Capability.SEARCH, Capability.COMPS),
    is_local=True,
    documentation="Local files only — no vendor API involved.",
)
register(
    "propertyreach", _propertyreach_factory, PropertyReachProvider.description,
    capabilities=PropertyReachProvider.capabilities,
    required_settings=(PROPERTYREACH_API_KEY_VAR,),
    documentation=PropertyReachProvider.documentation_note,
)
register(
    "http-template", _http_template_factory, HttpPropertyDataProvider.description,
    capabilities=(Capability.SEARCH,),
    required_settings=("PROPERTY_DATA_API_KEY", "PROPERTY_DATA_BASE_URL"),
    documentation=HttpPropertyDataProvider.documentation_note,
)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def describe_sources(settings: Optional[ProviderSettings] = None) -> str:
    """The ``--list-sources`` table, with why each adapter is or is not usable."""
    settings = settings or ProviderSettings.from_env()
    lines = ["AVAILABLE PROVIDERS (DATA_PROVIDER / --source)"]
    for name in registered_names():
        entry = _REGISTRY[name]
        if entry.is_local:
            status = "READY (local, no credentials)"
        elif entry.is_configured(settings):
            status = "CONFIGURED"
        else:
            status = "NOT CONNECTED — needs " + ", ".join(entry.missing_settings(settings))
        lines.append(f"  {name:<16}{status}")
        if entry.description:
            lines.append(f"  {'':<16}{entry.description}")
    lines.append("")
    lines.append(f"  Credentials: {settings.describe()}")
    if not settings.has_property_data:
        lines.append(f"  {NO_PROVIDER_MESSAGE}")
    return "\n".join(lines)


def capability_matrix(settings: Optional[ProviderSettings] = None) -> str:
    """Which adapter can answer which question. Independently, per capability."""
    settings = settings or ProviderSettings.from_env()
    names = registered_names()
    width = max([16] + [len(n) for n in names])
    lines = ["PROVIDER CAPABILITIES", ""]
    header = f"{'CAPABILITY':<20}" + "".join(f"{n:<{width + 2}}" for n in names)
    lines.append(header)
    lines.append("-" * len(header))
    for capability in Capability:
        row = f"{capability.label:<20}"
        for name in names:
            row += f"{'YES' if supports(name, capability) else 'no':<{width + 2}}"
        lines.append(row)
    lines.append("")
    for name in names:
        entry = _REGISTRY[name]
        state = (
            "READY" if entry.is_local
            else "CONFIGURED" if entry.is_configured(settings)
            else "NOT CONNECTED"
        )
        lines.append(f"  {name:<16}{state}")
        if entry.documentation:
            lines.append(f"  {'':<16}{entry.documentation}")
    return "\n".join(lines)


def health_report(
    settings: Optional[ProviderSettings] = None,
    csv_path: Optional[Path] = None,
    comps_path: Optional[Path] = None,
) -> str:
    """Try to construct every adapter and ask whether it is actually usable."""
    settings = settings or ProviderSettings.from_env()
    lines = ["PROVIDER HEALTH", ""]
    for name in registered_names():
        try:
            provider = get_provider(name, settings, csv_path, comps_path)
        except ProviderNotConfigured as exc:
            lines.append(f"  {name:<16}NOT CONNECTED — {exc}")
            continue
        ok, message = provider.health_check()
        lines.append(f"  {name:<16}{'OK' if ok else 'FAILING'} — {message}")
    return "\n".join(lines)


def get_provider(
    name: str,
    settings: Optional[ProviderSettings] = None,
    csv_path: Optional[Path] = None,
    comps_path: Optional[Path] = None,
    metrics: Optional[ProviderMetrics] = None,
) -> PropertyDataProvider:
    """Build the named adapter, or raise :class:`ProviderNotConfigured`."""
    key = (name or "csv").strip().lower()
    entry = _REGISTRY.get(key)
    if entry is None:
        raise ProviderNotConfigured(
            f"unknown provider '{name}'. Registered: {', '.join(registered_names())}. "
            "No vendor is selected for you — adding one means reading that "
            "vendor's API documentation and registering an adapter."
        )
    missing = entry.missing_settings(settings or ProviderSettings.from_env())
    if missing:
        raise ProviderNotConfigured(
            f"{key} is NOT CONNECTED: {', '.join(missing)} not set. Copy "
            ".env.example to .env and fill in the values from your provider account."
        )
    return entry.factory(
        settings or ProviderSettings.from_env(), csv_path, comps_path, metrics
    )


def provider_info(name: str) -> Optional[ProviderInfo]:
    """Static description of an adapter without constructing it."""
    entry = registration(name)
    if entry is None:
        return None
    settings = ProviderSettings.from_env()
    return ProviderInfo(
        name=entry.name,
        description=entry.description,
        is_local=entry.is_local,
        requires_credentials=bool(entry.required_settings),
        capabilities=tuple(str(c) for c in entry.capabilities),
        documentation_note=entry.documentation,
        configured=entry.is_configured(settings),
        missing_settings=entry.missing_settings(settings),
    )
