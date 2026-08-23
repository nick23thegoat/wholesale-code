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
from .http_client import (
    HttpConfig,
    HttpError,
    HttpStats,
    SafeHttpClient,
    redact,
    redact_headers,
    redact_payload,
)
from .propertyreach import (
    PropertyReachProvider,
    ReachUsage,
    to_comp,
    to_lead,
)
from .propertyreach_schema import (
    DEFAULT_BASE_URL as PROPERTYREACH_BASE_URL,
    ENDPOINTS as PROPERTYREACH_ENDPOINTS,
    schema_status as propertyreach_schema_status,
    unverified_endpoints as propertyreach_unverified_endpoints,
)
from .registry import (
    Registration,
    capability_matrix,
    describe_sources,
    get_provider,
    health_report,
    provider_info,
    providers_for,
    register,
    registered_names,
    registration,
    supports,
    unregister,
)

__all__ = [
    "Capability",
    "HttpConfig",
    "HttpError",
    "HttpStats",
    "Registration",
    "SafeHttpClient",
    "capability_matrix",
    "health_report",
    "providers_for",
    "redact",
    "redact_headers",
    "redact_payload",
    "registration",
    "supports",
    "unregister",
    "CsvProvider",
    "HttpPropertyDataProvider",
    "HuntCriteria",
    "PROPERTYREACH_BASE_URL",
    "PROPERTYREACH_ENDPOINTS",
    "PropertyDataProvider",
    "PropertyReachProvider",
    "ReachUsage",
    "propertyreach_schema_status",
    "propertyreach_unverified_endpoints",
    "to_comp",
    "to_lead",
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
