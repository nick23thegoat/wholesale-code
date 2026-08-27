"""The PropertyReach provider adapter.

PropertyReach is the first real property-data vendor wired into this engine.
The transport, error handling, budget accounting and model mapping are all
finished here; what is still outstanding is the exact wire format, which lives
in :mod:`wholesale_engine.providers.propertyreach_schema`.

Confirmed and implemented: base URL ``https://api.propertyreach.com/v1``,
``x-api-key`` header authentication, JSON responses, and
``POST /v1/skip-trace``.

Not confirmed: the REST paths for Property Search, Property Detail and
Comparables, and the response field names. **This adapter refuses to call an
unverified endpoint against the live API.** It will not guess a path and fire
a request at PropertyReach's servers on the strength of a naming convention.

Three rules the mapping follows, inherited from the rest of the engine:

* **a field the vendor did not return stays unknown** — never zero, never an
  empty string standing in for a real answer
* **an AVM is a claim, not a fact** — PropertyReach's estimated value arrives
  as an unverified ARV and only becomes VERIFIED/SUPPORTED once comps back it
* **equity is only equity when a mortgage balance is known** — otherwise it is
  a spread, and the equity engine labels it as such
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..lead_hunter.models import Lead
from ..models.enums import Occupancy, PropertyType, SaleStatus
from ..models.property import Comp
from ..settings import ProviderSettings
from .base import (
    Capability,
    PropertyDataProvider,
    ProviderNotConfigured,
    ProviderResponse,
)
from .criteria import HuntCriteria
from .http_client import HttpConfig, HttpError, SafeHttpClient
from .metrics import ProviderMetrics
from .propertyreach_schema import (
    API_KEY_VAR,
    AUTH_HEADER,
    AUTH_SCHEME,
    BASE_URL_VAR,
    COMP_FIELDS,
    COMP_LIST_KEYS,
    DEFAULT_BASE_URL,
    ENDPOINTS,
    MIN_SECONDS_BETWEEN_CALLS,
    PROPERTY_FIELDS,
    RESULT_LIST_KEYS,
    SEARCH_PARAMS,
    SEARCH_SIGNAL_PARAMS,
    Endpoint,
    FieldMap,
    schema_status,
    unverified_endpoints,
)


class PropertyReachUnverifiedEndpoint(ProviderNotConfigured):
    """A call was attempted against an endpoint whose path is not confirmed."""


# ---------------------------------------------------------------------------
# Value extraction
# ---------------------------------------------------------------------------


def dig(payload: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts. Missing returns ``None``."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _coerce(value: Any, kind: str) -> Any:
    """Convert a raw JSON value, or return ``None`` when it cannot be trusted.

    A value that will not convert is treated as absent rather than forced.
    Guessing at a malformed number is how bad data reaches an offer.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list, tuple, set)):
        # A nested object reached a scalar field — usually a candidate key that
        # names a container rather than a value. Stringifying it would put
        # "{'line1': None}" in an address, so treat it as absent instead.
        return None
    try:
        if kind == "float":
            return float(str(value).replace(",", "").replace("$", "").strip())
        if kind == "int":
            return int(float(str(value).replace(",", "").strip()))
        if kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "yes", "y", "1"):
                return True
            if text in ("false", "no", "n", "0"):
                return False
            return None
        if kind == "date":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    text = str(value).strip()
    return text or None


def extract(payload: Dict[str, Any], mapping: Sequence[FieldMap]) -> Dict[str, Any]:
    """Pull our fields out of a vendor record.

    Each field tries its candidate keys in order and takes the first that is
    present. **Every candidate missing leaves the field absent from the
    result** — the caller then treats it as unknown, which is the correct
    behaviour and the reason an unfinished mapping degrades safely instead of
    producing wrong numbers.
    """
    found: Dict[str, Any] = {}
    for entry in mapping:
        for candidate in entry.candidates:
            raw = dig(payload, candidate) if "." in candidate else payload.get(candidate)
            value = _coerce(raw, entry.kind)
            if value is not None:
                found[entry.field] = value
                break
    return found


def first_list(payload: Dict[str, Any], keys: Sequence[str]) -> List[Dict[str, Any]]:
    """The first key holding a list of records, or an empty list."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


# ---------------------------------------------------------------------------
# Model mapping
# ---------------------------------------------------------------------------

#: PropertyReach land-use / property-type strings we recognise. Anything else
#: becomes UNKNOWN, which never rejects a lead on its own.
_TYPE_ALIASES = {
    "single family": PropertyType.SINGLE_FAMILY,
    "singlefamily": PropertyType.SINGLE_FAMILY,
    "sfr": PropertyType.SINGLE_FAMILY,
    "duplex": PropertyType.DUPLEX,
    "triplex": PropertyType.TRIPLEX,
    "fourplex": PropertyType.FOURPLEX,
    "quadplex": PropertyType.FOURPLEX,
    "townhouse": PropertyType.TOWNHOUSE,
    "condo": PropertyType.CONDO,
    "condominium": PropertyType.CONDO,
    "multi family": PropertyType.MULTI_FAMILY,
    "mobile": PropertyType.MOBILE,
    "land": PropertyType.LAND,
    "vacant land": PropertyType.LAND,
    "commercial": PropertyType.COMMERCIAL,
}


def to_property_type(raw: Any) -> PropertyType:
    if not raw:
        return PropertyType.UNKNOWN
    text = str(raw).strip().lower().replace("-", " ").replace("_", " ")
    if text in _TYPE_ALIASES:
        return _TYPE_ALIASES[text]
    return PropertyType.parse(text)


def to_occupancy(fields: Dict[str, Any]) -> Occupancy:
    """Occupancy from the vacancy and owner-occupied flags, or UNKNOWN.

    Vacancy wins: a vacant property is vacant whoever owns it.
    """
    if fields.get("vacant") is True:
        return Occupancy.VACANT
    if fields.get("owner_occupied") is True:
        return Occupancy.OWNER_OCCUPIED
    if fields.get("owner_occupied") is False or fields.get("absentee_owner") is True:
        return Occupancy.TENANT_OCCUPIED
    return Occupancy.UNKNOWN


#: Distress flags we carry straight through from PropertyReach.
_SIGNALS = (
    "vacant", "absentee_owner", "high_equity", "pre_foreclosure", "foreclosure",
    "tax_delinquent", "probate", "inherited", "code_violation", "tired_landlord",
)


def to_lead(payload: Dict[str, Any], source: str = "propertyreach") -> Lead:
    """One PropertyReach record -> our :class:`Lead`.

    Only fields the response actually carried are set; everything else stays
    ``None`` and is reported as a gap. The estimated value becomes
    ``estimated_value``, which Wave 1 treats as an unverified ARV claim.
    """
    fields = extract(payload, PROPERTY_FIELDS)

    lead = Lead(
        lead_id=str(fields.get("property_id") or ""),
        property_id=str(fields.get("property_id") or ""),
        address=str(fields.get("address") or ""),
        city=str(fields.get("city") or ""),
        state=str(fields.get("state") or ""),
        county=str(fields.get("county") or ""),
        zip_code=str(fields.get("zip_code") or ""),
        owner_name=str(fields.get("owner_name") or ""),
        asking_price=fields.get("asking_price"),
        estimated_value=fields.get("estimated_value"),
        estimated_equity=fields.get("estimated_equity"),
        beds=fields.get("beds"),
        baths=fields.get("baths"),
        sqft=fields.get("sqft"),
        year_built=fields.get("year_built"),
        property_type=to_property_type(fields.get("property_type")),
        occupancy=to_occupancy(fields),
        days_on_market=fields.get("days_on_market"),
        source=source,
    )

    for name in _SIGNALS:
        value = fields.get(name)
        if isinstance(value, bool):
            setattr(lead, name, value)

    # Equity is only equity when a mortgage balance is known. Without one the
    # research layer labels it a spread, so do not pass a bare figure through
    # as though a mortgage had been checked.
    if fields.get("mortgage_balance") is None and "estimated_equity" in fields:
        lead.needs_verification.append(
            "PropertyReach reported an equity figure with no mortgage balance; "
            "it is treated as unverified."
        )

    lead.raw = {k: str(v) for k, v in fields.items()}
    return lead


def to_comp(payload: Dict[str, Any]) -> Optional[Comp]:
    """One PropertyReach comparable -> our :class:`Comp`.

    Returns ``None`` when the record has no address or no sale price — a comp
    without either cannot support a valuation and must not be counted as one.
    """
    fields = extract(payload, COMP_FIELDS)
    address = fields.get("address")
    price = fields.get("sale_price")
    if not address or price is None:
        return None
    return Comp(
        address=str(address),
        sale_price=price,
        sale_date=fields.get("sale_date"),
        sale_status=SaleStatus.CLOSED if fields.get("sale_date") else SaleStatus.UNKNOWN,
        beds=fields.get("beds"),
        baths=fields.get("baths"),
        sqft=fields.get("sqft"),
        year_built=fields.get("year_built"),
        distance_miles=fields.get("distance_miles"),
        property_type=to_property_type(fields.get("property_type")),
        source="propertyreach",
    )


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


@dataclass
class ReachUsage:
    """PropertyReach call accounting, against the MAX_REACH budget."""

    limit: int = 100
    search_calls: int = 0
    detail_calls: int = 0
    comp_calls: int = 0
    errors: int = 0
    stopped_by_budget: bool = False
    refused_unverified: List[str] = field(default_factory=list)

    @property
    def used(self) -> int:
        return self.search_calls + self.detail_calls + self.comp_calls

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reach_limit": self.limit,
            "reach_used": self.used,
            "reach_remaining": self.remaining,
            "search_calls": self.search_calls,
            "detail_calls": self.detail_calls,
            "comp_calls": self.comp_calls,
            "errors": self.errors,
            "stopped_by_budget": self.stopped_by_budget,
        }

    def render(self) -> str:
        lines = [
            "PROPERTYREACH USAGE",
            f"  Calls used          {self.used}",
            f"  Calls remaining     {self.remaining}   (MAX_REACH={self.limit})",
            f"    search            {self.search_calls}",
            f"    property detail   {self.detail_calls}",
            f"    comparables       {self.comp_calls}",
            f"  Errors              {self.errors}",
            f"  Budget stopped run  {'YES' if self.stopped_by_budget else 'no'}",
        ]
        for name in self.refused_unverified:
            lines.append(f"  REFUSED (unverified endpoint): {name}")
        return "\n".join(lines)


class PropertyReachProvider(PropertyDataProvider):
    """PropertyReach, through the engine's standard provider interface."""

    name = "propertyreach"
    description = (
        "PropertyReach property data. Requires PROPERTYREACH_API_KEY. "
        "Endpoint paths for search/detail/comps still need the vendor docs."
    )
    is_local = False
    requires_credentials = True
    #: What PropertyReach documents offering. A capability being declared does
    #: not mean the endpoint is callable — see ``verified`` in the schema.
    capabilities = (
        Capability.SEARCH,
        Capability.PROPERTY,
        Capability.OWNER,
        Capability.EQUITY,
        Capability.DISTRESS,
        Capability.FORECLOSURE,
        Capability.TAX,
        Capability.COMPS,
        Capability.VALUATION,
    )
    documentation_note = (
        "https://docs.propertyreach.com — base URL and x-api-key confirmed; "
        "search/detail/comps paths and response fields still unverified."
    )

    def __init__(
        self,
        settings: Optional[ProviderSettings] = None,
        metrics: Optional[ProviderMetrics] = None,
        max_reach: int = 100,
        allow_unverified: bool = False,
        client: Optional[SafeHttpClient] = None,
    ) -> None:
        super().__init__(metrics)
        self.settings = settings or ProviderSettings.from_env()
        self.usage = ReachUsage(limit=max_reach)
        #: Opt-in switch for calling an endpoint whose path is not confirmed.
        #: Off by default: an unconfirmed path is a request we will not send.
        self.allow_unverified = allow_unverified
        self.warnings: List[str] = []

        if client is not None:
            # Injected for tests. No credentials are read and nothing is sent.
            self.client = client
            return

        api_key = os.environ.get(API_KEY_VAR, "").strip()
        if not api_key:
            raise ProviderNotConfigured(
                f"PropertyReach is NOT CONNECTED: {API_KEY_VAR} is not set. "
                "Get a key from the Keys tab at https://app.propertyreach.com, "
                "put it in .env, and never commit it."
            )
        base_url = os.environ.get(BASE_URL_VAR, "").strip() or DEFAULT_BASE_URL
        self.client = SafeHttpClient(
            base_url,
            api_key,
            HttpConfig(min_interval_seconds=MIN_SECONDS_BETWEEN_CALLS),
            auth_header=AUTH_HEADER,
            auth_scheme=AUTH_SCHEME,
        )

    # ------------------------------------------------------------------

    def _endpoint(self, key: str) -> Endpoint:
        return ENDPOINTS[key]

    def _guard(self, key: str) -> Optional[str]:
        """Why this call must not be made, or ``None`` if it may proceed."""
        endpoint = self._endpoint(key)
        if not endpoint.verified and not self.allow_unverified:
            self.usage.refused_unverified.append(endpoint.name)
            return (
                f"{endpoint.name} was not called: its REST path is not confirmed "
                f"by PropertyReach's documentation. The engine will not guess a "
                f"path and send a live request. Fill in the path in "
                f"propertyreach_schema.py, set verified=True, or pass "
                f"allow_unverified=True to override deliberately."
            )
        if self.usage.exhausted:
            self.usage.stopped_by_budget = True
            return (
                f"MAX_REACH budget of {self.usage.limit} PropertyReach call(s) is "
                "spent. Raise MAX_REACH or narrow the search."
            )
        return None

    def _post(self, key: str, body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """One guarded call. Returns ``(payload, error)`` — never both."""
        endpoint = self._endpoint(key)
        try:
            payload = self.client.request(endpoint.path, method=endpoint.method, body=body)
        except HttpError as exc:
            self.usage.errors += 1
            self.metrics.record_error(str(exc))
            if exc.is_auth_failure:
                return None, (
                    f"PropertyReach rejected the credential ({exc.status}). Check "
                    f"{API_KEY_VAR} and that the plan covers this endpoint."
                )
            if exc.is_rate_limit:
                return None, (
                    "PropertyReach rate limit reached (429). The key works; the "
                    "quota does not. Lower MAX_REACH or wait."
                )
            return None, str(exc)
        if not isinstance(payload, dict) and not isinstance(payload, list):
            self.usage.errors += 1
            return None, "PropertyReach returned a response that is not JSON object or array."
        return ({"results": payload} if isinstance(payload, list) else payload), ""

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def build_search_body(self, criteria: HuntCriteria, page: int = 1) -> Dict[str, Any]:
        """Criteria -> PropertyReach search parameters.

        Every filter that can go server-side does, because a lead you never
        receive is a lead you never pay to enrich.
        """
        body: Dict[str, Any] = {}
        for our_name, their_name in SEARCH_PARAMS.items():
            if our_name == "page":
                body[their_name] = page
                continue
            value = getattr(criteria, our_name, None)
            if value in (None, (), []):
                continue
            body[their_name] = list(value) if isinstance(value, tuple) else value
        for signal in criteria.required_signals:
            their_name = SEARCH_SIGNAL_PARAMS.get(signal)
            if their_name:
                body[their_name] = True
        return body

    def search_properties(self, criteria: HuntCriteria) -> ProviderResponse[List[Lead]]:
        blocked = self._guard("search")
        if blocked:
            self.warnings.append(blocked)
            return ProviderResponse(data=[], supported=True, reason=blocked, source=self.name)

        self.usage.search_calls += 1
        self.metrics.search_calls += 1
        payload, error = self._post("search", self.build_search_body(criteria))
        if error:
            return ProviderResponse(data=[], supported=True, reason=error, source=self.name)

        records = first_list(payload, RESULT_LIST_KEYS)
        if not records:
            return ProviderResponse.empty(
                self.name, "PropertyReach returned no properties for these criteria."
            )

        leads: List[Lead] = []
        for record in records:
            try:
                lead = to_lead(record, self.name)
            except (TypeError, ValueError, AttributeError) as exc:
                self.metrics.record_error(f"unparseable PropertyReach record: {exc}")
                continue
            if lead.address:
                leads.append(lead)

        self.metrics.properties_searched += len(records)
        self.metrics.properties_returned += len(leads)
        if len(leads) < len(records):
            self.warnings.append(
                f"{len(records) - len(leads)} PropertyReach record(s) had no usable "
                "address and were skipped."
            )
        return ProviderResponse(data=leads, source=self.name, calls=1)

    def get_property(self, lead: Lead) -> ProviderResponse[Lead]:
        blocked = self._guard("detail")
        if blocked:
            return ProviderResponse(data=None, supported=True, reason=blocked, source=self.name)
        self.usage.detail_calls += 1
        payload, error = self._post("detail", self._identify(lead))
        if error:
            return ProviderResponse(data=None, supported=True, reason=error, source=self.name)
        record = first_list(payload, RESULT_LIST_KEYS)
        detail = record[0] if record else payload
        if not isinstance(detail, dict) or not detail:
            return ProviderResponse.empty(self.name, "no detail returned for this property")
        return ProviderResponse(data=to_lead(detail, self.name), source=self.name, calls=1)

    def _identify(self, lead: Lead) -> Dict[str, Any]:
        """The smallest body that identifies one property."""
        if lead.property_id:
            return {"propertyId": lead.property_id}
        return {
            SEARCH_PARAMS.get("cities", "city"): lead.city,
            SEARCH_PARAMS.get("states", "state"): lead.state,
            SEARCH_PARAMS.get("zip_codes", "zip"): lead.zip_code,
            "address": lead.address,
        }

    def _detail_slice(
        self, lead: Lead, wanted: Sequence[str], capability_label: str
    ) -> ProviderResponse[Dict[str, Any]]:
        """Owner / equity / distress / foreclosure / tax all come from detail.

        One call, sliced — asking four times for four subsets of the same
        record would be four charges for one answer.
        """
        response = self.get_property(lead)
        if not response.supported or response.data is None:
            return ProviderResponse(
                data=None, supported=response.supported,
                reason=response.reason or f"no {capability_label} returned",
                source=self.name,
            )
        raw = getattr(response.data, "raw", {}) or {}
        sliced = {k: v for k, v in raw.items() if k in wanted}
        if not sliced:
            return ProviderResponse.empty(
                self.name, f"PropertyReach returned no {capability_label} fields"
            )
        return ProviderResponse(data=sliced, source=self.name)

    def get_owner(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._detail_slice(
            lead,
            ("owner_name", "owner_mailing_address", "owner_occupied",
             "ownership_years", "properties_owned", "absentee_owner"),
            "ownership",
        )

    def get_equity(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._detail_slice(
            lead,
            ("mortgage_balance", "liens", "estimated_equity", "equity_percent",
             "estimated_value"),
            "equity",
        )

    def get_distress_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._detail_slice(lead, _SIGNALS, "distress")

    def get_foreclosure_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._detail_slice(
            lead, ("pre_foreclosure", "foreclosure", "auction_date"), "foreclosure"
        )

    def get_tax_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._detail_slice(
            lead,
            ("tax_amount", "assessed_value", "tax_delinquent", "tax_year"),
            "tax",
        )

    def get_valuation(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """PropertyReach's AVM. A claim, not a verified ARV."""
        response = self._detail_slice(
            lead, ("estimated_value", "estimated_rent", "assessed_value"), "valuation"
        )
        if response.ok:
            response.reason = (
                "PropertyReach automated valuation. Treated as an unverified ARV "
                "until comparable sales support it."
            )
        return response

    def get_comps(
        self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6
    ) -> ProviderResponse[List[Comp]]:
        blocked = self._guard("comps")
        if blocked:
            return ProviderResponse(data=[], supported=True, reason=blocked, source=self.name)
        self.usage.comp_calls += 1
        self.metrics.comp_calls += 1
        body = dict(self._identify(lead))
        body.update({"radius": radius_miles, "monthsBack": months_back})
        payload, error = self._post("comps", body)
        if error:
            return ProviderResponse(data=[], supported=True, reason=error, source=self.name)

        records = first_list(payload, COMP_LIST_KEYS)
        comps = [c for c in (to_comp(r) for r in records) if c is not None]
        if not comps:
            return ProviderResponse.empty(
                self.name, "PropertyReach returned no usable comparable sales"
            )
        return ProviderResponse(data=comps, source=self.name, calls=1)

    # ------------------------------------------------------------------

    def health_check(self) -> Tuple[bool, str]:
        """Is PropertyReach usable right now?

        Deliberately does **not** make a network call: there is no confirmed
        cheap endpoint to probe, and a health check should not spend budget.
        """
        if not os.environ.get(API_KEY_VAR, "").strip():
            return False, f"NOT CONNECTED — {API_KEY_VAR} is not set"
        outstanding = unverified_endpoints()
        if outstanding and not self.allow_unverified:
            return False, (
                "CONFIGURED but not callable — the REST paths for "
                + ", ".join(ENDPOINTS[k].name for k in outstanding)
                + " are still unverified. Fill them in from "
                "https://docs.propertyreach.com."
            )
        return True, "credentials present and every endpoint path is confirmed"

    def status(self) -> str:
        return "\n".join([schema_status(), "", self.usage.render()])


def build(
    settings: Optional[ProviderSettings] = None,
    csv_path: Any = None,
    comps_path: Any = None,
    metrics: Optional[ProviderMetrics] = None,
) -> PropertyReachProvider:
    """Registry factory. Signature matches every other provider."""
    from ..budget import ApiBudget

    return PropertyReachProvider(
        settings=settings, metrics=metrics, max_reach=ApiBudget.from_env().max_reach
    )
