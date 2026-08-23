"""Provider selection for ``--source``.

Deliberately conservative: the only provider that resolves out of the box is
``csv``. No paid vendor is wired in, because choosing one is a decision that
requires reading that vendor's actual API documentation, terms and pricing —
not something to guess at.

To add a real provider:

1. Read the vendor's official API documentation.
2. Subclass :class:`HttpPropertyDataProvider`, filling in ``search_path``,
   ``build_search_params`` and ``parse_lead``.
3. Register it here with :func:`register`.
4. Put the key in ``.env`` as ``PROPERTY_DATA_API_KEY``.

Nothing else in the engine changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..settings import NO_PROVIDER_MESSAGE, ProviderSettings
from .base import PropertyDataProvider, ProviderInfo, ProviderNotConfigured
from .csv_provider import CsvProvider
from .http_provider import HttpPropertyDataProvider
from .metrics import ProviderMetrics

#: name -> factory(settings, csv_path, comps_path, metrics) -> provider
Factory = Callable[..., PropertyDataProvider]

_REGISTRY: Dict[str, Factory] = {}
_DESCRIPTIONS: Dict[str, str] = {}


def register(name: str, factory: Factory, description: str = "") -> None:
    """Add a provider under ``name``, usable as ``--source <name>``."""
    key = name.strip().lower()
    if not key:
        raise ValueError("a provider needs a name")
    _REGISTRY[key] = factory
    _DESCRIPTIONS[key] = description


def registered_names() -> List[str]:
    return sorted(_REGISTRY)


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


register("csv", _csv_factory, CsvProvider.description)
register("http-template", _http_template_factory, HttpPropertyDataProvider.description)


def describe_sources(settings: Optional[ProviderSettings] = None) -> str:
    """A ``--list-sources`` table, including why each one is or is not usable."""
    settings = settings or ProviderSettings.from_env()
    lines = ["AVAILABLE SOURCES (--source)"]
    for name in registered_names():
        note = _DESCRIPTIONS.get(name, "")
        if name == "csv":
            status = "ready"
        elif settings.has_property_data:
            status = "credentials present, endpoint not implemented"
        else:
            status = "not configured (" + ", ".join(settings.missing_for_property_data()) + ")"
        lines.append(f"  {name:<16} {status}")
        if note:
            lines.append(f"  {'':<16} {note}")
    lines.append("")
    lines.append(f"  Credentials: {settings.describe()}")
    if not settings.has_property_data:
        lines.append(f"  {NO_PROVIDER_MESSAGE}")
    return "\n".join(lines)


def get_provider(
    name: str,
    settings: Optional[ProviderSettings] = None,
    csv_path: Optional[Path] = None,
    comps_path: Optional[Path] = None,
    metrics: Optional[ProviderMetrics] = None,
) -> PropertyDataProvider:
    """Build the named provider, or raise :class:`ProviderNotConfigured`.

    The caller decides what to do about an unconfigured provider. The funnel
    falls back to CSV and says so; it never pretends the live one answered.
    """
    key = (name or "csv").strip().lower()
    if key not in _REGISTRY:
        raise ProviderNotConfigured(
            f"unknown source '{name}'. Available: {', '.join(registered_names())}. "
            "No provider is selected for you — adding one means reading that "
            "vendor's API documentation first."
        )
    return _REGISTRY[key](settings or ProviderSettings.from_env(), csv_path, comps_path, metrics)


def provider_info(name: str) -> Optional[ProviderInfo]:
    """Static description of a provider without constructing it."""
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        return None
    if key == "csv":
        return ProviderInfo(
            name="csv",
            description=CsvProvider.description,
            is_local=True,
            requires_credentials=False,
            capabilities=("search_properties", "get_comps"),
            documentation_note=CsvProvider.documentation_note,
            configured=True,
        )
    settings = ProviderSettings.from_env()
    return ProviderInfo(
        name=key,
        description=_DESCRIPTIONS.get(key, ""),
        is_local=False,
        requires_credentials=True,
        capabilities=("search_properties",),
        documentation_note=HttpPropertyDataProvider.documentation_note,
        configured=settings.has_property_data,
        missing_settings=settings.missing_for_property_data(),
    )
