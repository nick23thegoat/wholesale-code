"""Wave 4 property-data provider layer.

    HuntCriteria ──► PropertyDataProvider ──► List[Lead] ──► lead_hunter
                                                                  │
                                                    Wave 1 analyzer (unchanged)

The provider is the only part that knows where data came from. Everything
downstream — normalization, dedupe, lead scoring, the deal analyzer — is the
code that already existed and is not duplicated here.
"""

from __future__ import annotations

from .base import (
    Capability,
    PropertyDataProvider,
    ProviderError,
    ProviderInfo,
    ProviderNotConfigured,
    ProviderResponse,
)
from .criteria import HuntCriteria
from .csv_provider import CsvProvider
from .http_provider import HttpPropertyDataProvider
from .metrics import ProviderMetrics
from .registry import (
    describe_sources,
    get_provider,
    provider_info,
    register,
    registered_names,
)

__all__ = [
    "Capability",
    "CsvProvider",
    "HttpPropertyDataProvider",
    "HuntCriteria",
    "PropertyDataProvider",
    "ProviderError",
    "ProviderInfo",
    "ProviderMetrics",
    "ProviderNotConfigured",
    "ProviderResponse",
    "describe_sources",
    "get_provider",
    "provider_info",
    "register",
    "registered_names",
]
