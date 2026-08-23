"""Template for a real HTTP property-data provider. **Not connected.**

This is the file to fill in once you have chosen a vendor and read its
published API documentation. It is deliberately inert until then:

* there is no default base URL, because inventing an endpoint would produce
  requests to something that does not exist;
* there is no default auth scheme, because header name, token format and
  refresh behaviour are vendor-specific facts, not guesses;
* there is no response parsing, because field names come from the vendor's
  schema.

What IS finished and does not need touching:

* the interface, so the funnel needs no changes when this goes live
* credential loading from the environment
* call counting, so you can see the bill before you get it
* rate limiting and retry/backoff around the transport
* the conversion contract into :class:`Lead`

**Rules this file exists to enforce.** Use the vendor's official, documented
API with your own account. Respect its published rate limits and terms. Do not
scrape a site's HTML, and do not work around CAPTCHAs, robots.txt, logins,
paywalls, or anti-bot measures — a provider that needs any of that is one this
engine will not add.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..lead_hunter.models import Lead
from ..settings import ProviderSettings
from .base import (
    Capability,
    PropertyDataProvider,
    ProviderNotConfigured,
    ProviderResponse,
)
from .criteria import HuntCriteria
from .metrics import ProviderMetrics


class HttpPropertyDataProvider(PropertyDataProvider):
    """Generic authenticated-JSON-API provider. Subclass per vendor.

    A subclass supplies four things, all of which come from the vendor's
    documentation and none of which can be guessed:

    1. :attr:`search_path` — the endpoint path for a property search
    2. :meth:`build_search_params` — criteria to that endpoint's query shape
    3. :meth:`parse_lead` — one item of the response to a :class:`Lead`
    4. :attr:`capabilities` — what the vendor actually sells you
    """

    name = "http-template"
    description = "Template for a real vendor API. Inert until a vendor is chosen."
    is_local = False
    requires_credentials = True
    capabilities = (Capability.SEARCH,)
    documentation_note = (
        "NO VENDOR SELECTED. Fill in search_path, build_search_params and "
        "parse_lead from the vendor's official API documentation."
    )

    #: Endpoint path, relative to the configured base URL. Vendor-specific.
    search_path: str = ""
    #: Header carrying the key. Vendor-specific — Bearer is only a common case.
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    #: Minimum seconds between requests. Raise to match the vendor's limit.
    min_seconds_between_calls: float = 0.5
    #: Attempts per request, with exponential backoff between them.
    max_retries: int = 3
    timeout_seconds: float = 20.0

    def __init__(
        self,
        settings: Optional[ProviderSettings] = None,
        metrics: Optional[ProviderMetrics] = None,
    ) -> None:
        super().__init__(metrics)
        self.settings = settings or ProviderSettings.from_env()
        missing = self.settings.missing_for_property_data()
        if missing:
            raise ProviderNotConfigured(
                f"{self.name} needs {' and '.join(missing)}. Copy .env.example to "
                ".env and fill in the values from your provider account. Until then "
                "the engine runs in CSV/test mode."
            )
        if not self.search_path:
            raise ProviderNotConfigured(
                f"{self.name} has no endpoint path configured. This is a template: "
                "subclass it and set search_path from your vendor's published API "
                "documentation. No endpoint is guessed."
            )
        self._last_call_at = 0.0

    # ------------------------------------------------------------------
    # Transport — finished, vendor-independent
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Self-imposed rate limit. Never hammer a provider."""
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self.min_seconds_between_calls:
            time.sleep(self.min_seconds_between_calls - elapsed)
        self._last_call_at = time.monotonic()

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """One authenticated GET with backoff. Raises on final failure."""
        base = (self.settings.property_data_base_url or "").rstrip("/")
        url = f"{base}/{path.lstrip('/')}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(url, method="GET")
        request.add_header(
            self.auth_header,
            f"{self.auth_scheme} {self.settings.property_data_api_key}".strip(),
        )
        request.add_header("Accept", "application/json")

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429 or exc.code >= 500:
                    # Rate limited or vendor-side: back off and try again.
                    time.sleep(2.0**attempt)
                    continue
                break  # 4xx: our request is wrong, retrying will not fix it
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(2.0**attempt)
        raise RuntimeError(f"{self.name}: {last_error}")

    # ------------------------------------------------------------------
    # Vendor-specific — must be implemented from real documentation
    # ------------------------------------------------------------------

    def build_search_params(self, criteria: HuntCriteria) -> Dict[str, Any]:
        """Translate criteria into the vendor's query parameters.

        Push every filter the API supports server-side. Each filter applied
        here is leads you do not pay to receive and do not pay to enrich.
        """
        raise NotImplementedError(
            "build_search_params must be written from the vendor's API documentation."
        )

    def parse_lead(self, payload: Dict[str, Any]) -> Lead:
        """Convert one response item into a normalized :class:`Lead`.

        Leave every field the vendor did not return blank. Do not default a
        signal to False — unknown is ``None``, and the engine reports it as a
        gap rather than scoring it either way.
        """
        raise NotImplementedError(
            "parse_lead must be written from the vendor's response schema."
        )

    # ------------------------------------------------------------------

    def search_properties(self, criteria: HuntCriteria) -> ProviderResponse[List[Lead]]:
        self.metrics.search_calls += 1
        try:
            payload = self._request(self.search_path, self.build_search_params(criteria))
        except NotImplementedError as exc:
            self.metrics.record_error(str(exc))
            return ProviderResponse(data=[], supported=False, reason=str(exc), source=self.name)
        except RuntimeError as exc:
            self.metrics.record_error(str(exc))
            return ProviderResponse(data=[], supported=True, reason=str(exc), source=self.name)

        items = payload.get("results") or payload.get("data") or []
        leads: List[Lead] = []
        for item in items:
            try:
                leads.append(self.parse_lead(item))
            except (KeyError, TypeError, ValueError) as exc:
                self.metrics.record_error(f"unparseable record: {exc}")
        self.metrics.properties_searched += len(items)
        self.metrics.properties_returned += len(leads)
        return ProviderResponse(data=leads, source=self.name, calls=1)
