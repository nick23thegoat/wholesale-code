"""The RentCast provider adapter.

RentCast is the connected property-data vendor on the free plan: **50
successful requests per calendar month**. Every design decision in this file
falls out of that number.

Where the requests go
---------------------

``/properties`` costs **one request and returns up to 500 records**, each one
already carrying owner of record, tax assessments and sale history. That makes
the search the cheapest thing this engine does and the detail lookup the most
wasteful — so this adapter deliberately does **not** declare
:attr:`Capability.PROPERTY`. The funnel calls ``get_property`` once per lead in
the research pool (up to ``MAX_RESEARCH``, 100 by default); at one billed
request each that would spend a whole month's plan twice over to re-fetch data
the search already returned. Owner, tax and distress are answered instead from
the record the search handed back, which costs nothing.

``/avm/value`` is the only genuinely per-property call, and it is the one this
adapter guards hardest: quota checked before the request, a reserve held back
so a valuation run can never eat the searches, and the response cached so a
re-run of the funnel is free.

Three rules inherited from the rest of the engine:

* **a field RentCast did not return stays unknown** — never zero, never an
  empty string standing in for an answer
* **an AVM is a claim, not a fact** — it arrives as an unverified ARV and only
  becomes VERIFIED/SUPPORTED once comparable sales back it
* **owner data is ownership of record only** — never a phone number, never an
  email address; that is skip tracing and it lives behind its own interface

Nothing here logs a response body. RentCast property records contain owner
names and mailing addresses, and on a server that is journald keeping owner
PII in plaintext for as long as the log rotation allows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
from .cache import TTL_PROPERTY_RECORDS, TTL_VALUATION, ResponseCache
from .criteria import HuntCriteria
from .http_client import HttpConfig, HttpError, SafeHttpClient
from .metrics import ProviderMetrics
from .quota import QuotaExceeded, QuotaLedger
from .rentcast_schema import (
    API_KEY_VAR,
    AUTH_HEADER,
    AUTH_SCHEME,
    AVM_COMPS_CANDIDATE_KEYS,
    AVM_VALUE_CANDIDATE_KEYS,
    BASE_URL,
    BASE_URL_VAR,
    FREE_TIER_MONTHLY_LIMIT,
    MAX_PAGE_SIZE,
    RENTCAST_TO_ENGINE,
    SEARCH_PATH,
    VALUATION_PATH,
    to_rentcast_types,
)

#: Seconds between requests. RentCast publishes no per-second limit for the
#: free plan, so the client stays deliberately polite.
MIN_SECONDS_BETWEEN_CALLS = 1.0

#: Requests held back from valuations so a month's scheduled searches can
#: always run. Four covers a weekly cadence with one spare.
DEFAULT_SEARCH_RESERVE = 4
SEARCH_RESERVE_VAR = "RENTCAST_SEARCH_RESERVE"


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


def _number(value: Any) -> Optional[float]:
    """A float, or ``None`` when the value cannot be trusted as one.

    Containers are rejected outright: a nested object reaching a numeric field
    means a key named a container rather than a value, and stringifying it
    would put ``"{'price': None}"`` where a number belongs.
    """
    if value is None or value == "" or isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _flag(value: Any) -> Optional[bool]:
    """Tri-state. ``None`` means RentCast did not say, and never scores."""
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    return None


def _date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def first_present(payload: Dict[str, Any], keys: Sequence[str]) -> Any:
    """The first candidate key that carries a usable scalar."""
    for key in keys:
        raw = dig(payload, key) if "." in key else payload.get(key)
        if raw not in (None, "") and not isinstance(raw, (dict, list, tuple, set)):
            return raw
    return None


def first_list(payload: Any, keys: Sequence[str]) -> List[Dict[str, Any]]:
    """The first key holding a list of records, or an empty list.

    ``/properties`` returns a bare JSON array, so a list argument is the
    normal case rather than the exception.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def latest_year_entry(container: Any) -> Dict[str, Any]:
    """The newest entry of a year-keyed map, e.g. ``{"2023": {...}}``.

    RentCast keys tax assessments and property taxes by year. Taking the
    highest key is the only ordering that is meaningful, and a map that is not
    year-keyed yields nothing rather than a wrong year.
    """
    if not isinstance(container, dict) or not container:
        return {}
    years = [k for k in container if str(k).strip().isdigit()]
    if not years:
        return {}
    newest = max(years, key=lambda k: int(str(k).strip()))
    entry = container.get(newest)
    if not isinstance(entry, dict):
        return {}
    return {"year": int(str(newest).strip()), **entry}


# ---------------------------------------------------------------------------
# Model mapping
# ---------------------------------------------------------------------------


def to_property_type(raw: Any) -> PropertyType:
    """RentCast's Title Case type to ours. Unrecognised becomes UNKNOWN.

    UNKNOWN never rejects a lead on its own — the funnel treats an unknown
    property type as a gap to fill, not a disqualification.
    """
    text = _text(raw)
    if not text:
        return PropertyType.UNKNOWN
    mapped = RENTCAST_TO_ENGINE.get(text)
    if mapped is None:
        # Case- and separator-insensitive second pass, so "single family" and
        # "Single-Family" land on the same place as "Single Family".
        folded = text.lower().replace("-", " ").replace("_", " ")
        for name, ours in RENTCAST_TO_ENGINE.items():
            if name.lower().replace("-", " ") == folded:
                mapped = ours
                break
    if mapped is None:
        return PropertyType.parse(text)
    return PropertyType.parse(mapped)


def to_occupancy(owner_occupied: Optional[bool]) -> Occupancy:
    """Occupancy from RentCast's ``ownerOccupied`` flag, or UNKNOWN.

    ``ownerOccupied=False`` deliberately does **not** become TENANT_OCCUPIED.
    RentCast publishes no vacancy flag, so a property the owner does not live
    in is either rented or empty and RentCast has not said which — and the
    difference between "tenant in place" and "vacant" is most of what makes a
    lead worth driving to. The reported fact, that the owner lives elsewhere,
    is carried by ``absentee_owner`` instead, where it is true without being
    embellished.
    """
    if owner_occupied is True:
        return Occupancy.OWNER_OCCUPIED
    return Occupancy.UNKNOWN


def owner_of_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Ownership from a ``/properties`` record. Ownership only.

    Names, entity type and mailing address are public record. Phone numbers
    and email addresses are not in this response and are never synthesised
    from one — that is skip tracing, behind its own interface.
    """
    owner = record.get("owner")
    if not isinstance(owner, dict):
        return {}
    names = owner.get("names")
    if isinstance(names, list):
        names = [_text(n) for n in names if _text(n)]
    elif _text(names):
        names = [_text(names)]
    else:
        names = []

    mailing = owner.get("mailingAddress")
    mailing_text = ""
    if isinstance(mailing, dict):
        mailing_text = _text(
            mailing.get("formattedAddress")
            or ", ".join(
                part for part in (
                    _text(mailing.get("addressLine1")),
                    _text(mailing.get("city")),
                    _text(mailing.get("state")),
                    _text(mailing.get("zipCode")),
                ) if part
            )
        )
    elif _text(mailing):
        mailing_text = _text(mailing)

    found: Dict[str, Any] = {}
    if names:
        found["owner_name"] = names[0]
        found["owner_names"] = names
    if _text(owner.get("type")):
        found["owner_type"] = _text(owner.get("type"))
    if mailing_text:
        found["owner_mailing_address"] = mailing_text
    return found


def tax_of_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """The most recent tax assessment and tax bill, if either is present."""
    found: Dict[str, Any] = {}
    assessment = latest_year_entry(record.get("taxAssessments"))
    if assessment:
        value = _number(assessment.get("value"))
        if value is not None:
            found["assessed_value"] = value
        for key, ours in (("land", "assessed_land_value"),
                          ("improvements", "assessed_improvement_value")):
            number = _number(assessment.get(key))
            if number is not None:
                found[ours] = number
        found["assessment_year"] = assessment.get("year")

    taxes = latest_year_entry(record.get("propertyTaxes"))
    if taxes:
        total = _number(taxes.get("total"))
        if total is not None:
            found["tax_amount"] = total
        found["tax_year"] = taxes.get("year")
    return found


def to_lead(record: Dict[str, Any], source: str = "rentcast") -> Lead:
    """One ``/properties`` record -> our :class:`Lead`.

    Every field RentCast did not return is left at its default and reported
    downstream as a gap. Note what is deliberately **not** set: ``vacant``,
    ``pre_foreclosure``, ``tax_delinquent`` and the rest stay ``None``, because
    RentCast does not report them and unknown must never be scored as False.
    """
    owner = owner_of_record(record)
    tax = tax_of_record(record)
    owner_occupied = _flag(record.get("ownerOccupied"))

    address = _text(record.get("formattedAddress")) or _text(record.get("addressLine1"))
    lead = Lead(
        lead_id=_text(record.get("id")),
        property_id=_text(record.get("id")),
        address=address,
        city=_text(record.get("city")),
        state=_text(record.get("state")),
        county=_text(record.get("county")),
        zip_code=_text(record.get("zipCode")),
        owner_name=_text(owner.get("owner_name")),
        beds=_int(record.get("bedrooms")),
        baths=_number(record.get("bathrooms")),
        sqft=_int(record.get("squareFootage")),
        year_built=_int(record.get("yearBuilt")),
        property_type=to_property_type(record.get("propertyType")),
        occupancy=to_occupancy(owner_occupied),
        source=source,
    )

    # RentCast's /properties is a record feed, not a listing feed: there is no
    # asking price in it. Leaving it None is correct — the last sale price is
    # what a previous buyer paid, not what this seller wants, and substituting
    # one for the other would corrupt every offer downstream.
    last_sale = _number(record.get("lastSalePrice"))
    if last_sale is not None:
        lead.raw["last_sale_price"] = str(last_sale)
    last_sale_date = _date(record.get("lastSaleDate"))
    if last_sale_date is not None:
        lead.raw["last_sale_date"] = last_sale_date.isoformat()

    # An absentee owner is the one distress signal RentCast actually answers:
    # ownerOccupied=False is a reported fact, not an inference. When the flag
    # is absent the signal stays unknown.
    if owner_occupied is not None:
        lead.absentee_owner = not owner_occupied

    for key, value in list(owner.items()) + list(tax.items()):
        if value is None:
            continue
        lead.raw[key] = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)

    if not lead.raw.get("assessed_value"):
        lead.needs_verification.append(
            "RentCast returned no tax assessment for this property."
        )
    lead.needs_verification.append(
        "RentCast reports no asking price, vacancy, lien or foreclosure status. "
        "Those remain unknown and must be checked against county records."
    )
    return lead


def to_comp(record: Dict[str, Any]) -> Optional[Comp]:
    """One AVM comparable -> our :class:`Comp`.

    Returns ``None`` without an address or a price: a comp missing either
    cannot support a valuation and must not be counted as though it could.
    """
    address = _text(record.get("formattedAddress")) or _text(record.get("addressLine1"))
    price = _number(first_present(record, ("price", "salePrice", "lastSalePrice")))
    if not address or price is None:
        return None
    sale_date = _date(first_present(record, ("removedDate", "lastSeenDate", "saleDate")))
    return Comp(
        address=address,
        sale_price=price,
        sale_date=sale_date,
        sale_status=SaleStatus.CLOSED if sale_date else SaleStatus.UNKNOWN,
        beds=_int(record.get("bedrooms")),
        baths=_number(record.get("bathrooms")),
        sqft=_int(record.get("squareFootage")),
        year_built=_int(record.get("yearBuilt")),
        distance_miles=_number(record.get("distance")),
        property_type=to_property_type(record.get("propertyType")),
        source="rentcast",
    )


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


@dataclass
class RentCastUsage:
    """Billable RentCast requests this run, and what stopped them."""

    limit: int = FREE_TIER_MONTHLY_LIMIT
    search_calls: int = 0
    avm_calls: int = 0
    cache_hits: int = 0
    free_reads: int = 0
    errors: int = 0
    stopped_by_budget: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def used(self) -> int:
        """Billable requests only. Cache hits and record reads are not."""
        return self.search_calls + self.avm_calls

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rentcast_limit": self.limit,
            "rentcast_used": self.used,
            "rentcast_remaining": self.remaining,
            "search_calls": self.search_calls,
            "avm_calls": self.avm_calls,
            "cache_hits": self.cache_hits,
            "free_reads": self.free_reads,
            "errors": self.errors,
            "stopped_by_budget": self.stopped_by_budget,
        }

    def render(self) -> str:
        lines = [
            "RENTCAST USAGE (this run)",
            f"  Billable requests   {self.used}",
            f"    property search   {self.search_calls}",
            f"    valuation (AVM)   {self.avm_calls}",
            f"  Served from cache   {self.cache_hits}   (free)",
            f"  Read from record    {self.free_reads}   (free — owner/tax/distress)",
            f"  Errors              {self.errors}   (failures are not billed)",
            f"  Budget stopped run  {'YES' if self.stopped_by_budget else 'no'}",
        ]
        for note in self.notes:
            lines.append(f"  NOTE: {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class RentCastProvider(PropertyDataProvider):
    """RentCast, through the engine's standard provider interface."""

    name = "rentcast"
    description = (
        "RentCast property records, owner of record, tax history and automated "
        "valuations. Requires RENTCAST_API_KEY. Free plan: 50 requests/month."
    )
    is_local = False
    requires_credentials = True

    #: What this adapter can answer, and at what price.
    #:
    #: SEARCH     one billed request for up to 500 records
    #: OWNER      free — read from the record the search already returned
    #: TAX        free — same
    #: DISTRESS   free — absentee owner only; everything else stays unknown
    #: VALUATION  one billed request per property, quota-guarded
    #: COMPS      the comparables carried by that same valuation response
    #:
    #: PROPERTY is deliberately absent. The funnel calls ``get_property`` once
    #: per researched lead; at one billed request each that is the whole
    #: monthly plan spent re-fetching what the search already returned.
    capabilities = (
        Capability.SEARCH,
        Capability.OWNER,
        Capability.TAX,
        Capability.DISTRESS,
        Capability.VALUATION,
        Capability.COMPS,
    )
    documentation_note = (
        "https://developers.rentcast.io — base URL, X-Api-Key auth, the 500-record "
        "page size and success-only billing are confirmed; response field names "
        "are from the docs and unverified against a live response."
    )

    def __init__(
        self,
        settings: Optional[ProviderSettings] = None,
        metrics: Optional[ProviderMetrics] = None,
        ledger: Optional[QuotaLedger] = None,
        cache: Optional[ResponseCache] = None,
        client: Optional[SafeHttpClient] = None,
        search_reserve: Optional[int] = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        super().__init__(metrics)
        self.settings = settings or ProviderSettings.from_env()
        self.ledger = ledger if ledger is not None else QuotaLedger.load("rentcast")
        self.cache = cache if cache is not None else ResponseCache(provider="rentcast")
        self.usage = RentCastUsage(limit=self.ledger.limit)
        self.page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        self.search_reserve = (
            search_reserve if search_reserve is not None else _env_int(
                SEARCH_RESERVE_VAR, DEFAULT_SEARCH_RESERVE
            )
        )
        self.warnings: List[str] = []

        if client is not None:
            # Injected for tests. No credentials are read and nothing is sent.
            self.client = client
            return

        api_key = os.environ.get(API_KEY_VAR, "").strip()
        if not api_key:
            raise ProviderNotConfigured(
                f"RentCast is NOT CONNECTED: {API_KEY_VAR} is not set. Create a key "
                "at https://app.rentcast.io/app/api, put it in .env, and never "
                "commit it."
            )
        base_url = os.environ.get(BASE_URL_VAR, "").strip() or BASE_URL
        self.client = SafeHttpClient(
            base_url,
            api_key,
            HttpConfig(min_interval_seconds=MIN_SECONDS_BETWEEN_CALLS),
            auth_header=AUTH_HEADER,
            auth_scheme=AUTH_SCHEME,
        )

    # ------------------------------------------------------------------
    # Transport: cache -> quota -> request -> record -> cache
    # ------------------------------------------------------------------

    def _fetch(
        self,
        path: str,
        params: Dict[str, Any],
        ttl_seconds: int,
        reserve: int = 0,
    ) -> Tuple[Any, str, bool]:
        """One guarded request. Returns ``(payload, error, was_billed)``.

        The order matters and is the whole point of this method:

        1. **cache first** — a hit costs nothing and is not recorded
        2. **quota second** — refuse before the request, never after the bill
        3. request, and only on success record one spent request
        4. cache the successful response

        A failure is never recorded against the quota (RentCast bills
        successes only) and is never cached (an error is not an answer).
        """
        cached = self.cache.get(path, params, ttl_seconds)
        if cached is not None:
            self.usage.cache_hits += 1
            self.ledger.record_cache_hit()
            return cached, "", False

        affordable = self.ledger.remaining - max(reserve, 0)
        if affordable < 1:
            self.usage.stopped_by_budget = True
            reason = (
                f"RentCast quota guard: {self.ledger.used}/{self.ledger.limit} requests "
                f"used this month"
                + (f", {reserve} held back for scheduled searches" if reserve else "")
                + ". No request was made. Raise MAX_RENTCAST in .env if you have "
                "upgraded the plan, or wait for the month to roll over."
            )
            self.warnings.append(reason)
            return None, reason, False

        try:
            self.ledger.require(1)
        except QuotaExceeded as exc:
            self.usage.stopped_by_budget = True
            self.warnings.append(str(exc))
            return None, str(exc), False

        try:
            payload = self.client.request(path, params)
        except HttpError as exc:
            self.usage.errors += 1
            self.metrics.record_error(str(exc))
            if exc.is_auth_failure:
                return None, (
                    f"RentCast rejected the credential ({exc.status}). Check "
                    f"{API_KEY_VAR}. Rejected requests are not billed."
                ), False
            if exc.is_rate_limit:
                return None, (
                    "RentCast rate limit reached (429). The key works; the plan "
                    "quota does not. Check https://app.rentcast.io/app/api."
                ), False
            return None, str(exc), False

        # Success: RentCast bills this one. Record before anything else can fail.
        self.ledger.record(1)
        self.cache.put(path, params, payload)
        return payload, "", True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def build_search_params(self, criteria: HuntCriteria) -> Dict[str, Any]:
        """Criteria -> documented ``/properties`` parameters.

        Only parameters RentCast documents are sent. An undocumented one is
        either ignored — losing the filter silently — or an error, and either
        way it costs a request from a plan of fifty.

        Price is **not** sent: RentCast documents no price filter on this
        endpoint. The buy box's price band is applied locally by the funnel
        instead, which costs nothing extra because the request returns the
        same 500 records either way.
        """
        params: Dict[str, Any] = {"limit": self.page_size}
        if criteria.zip_codes:
            params["zipCode"] = criteria.zip_codes[0]
        elif criteria.cities:
            params["city"] = criteria.cities[0].title()
        if criteria.states:
            params["state"] = criteria.states[0]

        property_types = to_rentcast_types(criteria.property_types)
        if property_types:
            params["propertyType"] = property_types
        return params

    def search_properties(self, criteria: HuntCriteria) -> ProviderResponse[List[Lead]]:
        """One billed request for up to 500 records.

        RentCast's ``/properties`` takes one geography at a time, so a criteria
        set naming several ZIPs would cost one request each. This adapter
        searches the first only and says so, rather than quietly spending the
        month — the service layer decides how many geographies a run can
        afford and calls once per geography.
        """
        extra_geographies = max(len(criteria.zip_codes) + len(criteria.cities) - 1, 0)
        if extra_geographies:
            self.warnings.append(
                f"RentCast searches one geography per request; {extra_geographies} "
                "other location(s) in these criteria were not searched. Each would "
                "cost one more request."
            )

        params = self.build_search_params(criteria)
        self.metrics.search_calls += 1
        payload, error, billed = self._fetch(
            SEARCH_PATH, params, TTL_PROPERTY_RECORDS, reserve=0
        )
        if billed:
            self.usage.search_calls += 1
        if error:
            return ProviderResponse(data=[], supported=True, reason=error, source=self.name)

        records = first_list(payload, ("results", "data", "properties"))
        if not records:
            return ProviderResponse.empty(
                self.name, "RentCast returned no properties for these criteria."
            )

        leads: List[Lead] = []
        for record in records:
            try:
                lead = to_lead(record, self.name)
            except (TypeError, ValueError, AttributeError) as exc:
                # Never log the record itself: it carries owner name and
                # mailing address. The exception text is enough to debug a
                # mapping bug and carries no PII of its own.
                self.metrics.record_error(f"unparseable RentCast record: {exc}")
                continue
            if lead.address:
                leads.append(lead)

        self.metrics.properties_searched += len(records)
        self.metrics.properties_returned += len(leads)
        skipped = len(records) - len(leads)
        if skipped:
            self.warnings.append(
                f"{skipped} RentCast record(s) had no usable address and were skipped."
            )
        if len(records) >= self.page_size:
            self.warnings.append(
                f"RentCast returned a full page of {len(records)} records; there may "
                "be more. Another page costs another request."
            )
        return ProviderResponse(data=leads, source=self.name, calls=1 if billed else 0)

    # ------------------------------------------------------------------
    # Free reads — answered from the record the search already returned
    # ------------------------------------------------------------------

    def _from_record(
        self, lead: Lead, wanted: Sequence[str], label: str
    ) -> ProviderResponse[Dict[str, Any]]:
        """Slice fields out of the lead's own record. Costs nothing.

        This is the reason :attr:`Capability.PROPERTY` is not declared: the
        search response already carried owner, tax and occupancy, so answering
        from it is both free and identical to what a detail call would return.
        """
        raw = getattr(lead, "raw", {}) or {}
        found = {k: v for k, v in raw.items() if k in wanted and v not in (None, "")}
        self.usage.free_reads += 1
        if not found:
            return ProviderResponse.empty(
                self.name,
                f"RentCast's property record carried no {label} fields for this "
                "property. Nothing has been invented to fill the gap.",
            )
        return ProviderResponse(data=found, source=self.name, calls=0)

    def get_owner(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """Ownership of record. Never contact information."""
        return self._from_record(
            lead,
            ("owner_name", "owner_names", "owner_type", "owner_mailing_address"),
            "ownership",
        )

    def get_tax_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        return self._from_record(
            lead,
            ("assessed_value", "assessed_land_value", "assessed_improvement_value",
             "assessment_year", "tax_amount", "tax_year"),
            "tax",
        )

    def get_distress_data(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """Absentee ownership, and an explicit list of what RentCast cannot say.

        RentCast publishes no lien, foreclosure, tax-delinquency or vacancy
        data. Those signals stay ``None`` on the lead so they never score and
        never reject; the reason says where to go and check them instead.
        """
        found: Dict[str, Any] = {}
        if lead.absentee_owner is not None:
            found["absentee_owner"] = lead.absentee_owner
        mailing = (lead.raw or {}).get("owner_mailing_address")
        if mailing:
            found["owner_mailing_address"] = mailing
        self.usage.free_reads += 1
        if not found:
            return ProviderResponse.empty(
                self.name,
                "RentCast reported no occupancy for this property, and it publishes "
                "no lien, foreclosure, tax-delinquency or vacancy data. Those stay "
                "unknown and are listed as gaps to fill from county records.",
            )
        return ProviderResponse(data=found, source=self.name, calls=0)

    # ------------------------------------------------------------------
    # Valuation and comps — the only per-property billable calls
    # ------------------------------------------------------------------

    def _avm_params(self, lead: Lead) -> Optional[Dict[str, Any]]:
        """The smallest documented body that identifies one property."""
        if lead.address:
            parts = [lead.address]
            if lead.city:
                parts.append(lead.city)
            if lead.state:
                parts.append(f"{lead.state} {lead.zip_code}".strip())
            return {"address": ", ".join(parts)}
        return None

    def _avm(self, lead: Lead) -> Tuple[Optional[Dict[str, Any]], str]:
        """One ``/avm/value`` response for this lead, cached and quota-guarded.

        Valuation and comps come from the same response, so asking for both
        costs one request: the second call is served from cache.
        """
        params = self._avm_params(lead)
        if params is None:
            return None, (
                "RentCast needs an address to value a property, and this lead has "
                "none. No request was made."
            )
        payload, error, billed = self._fetch(
            VALUATION_PATH, params, TTL_VALUATION, reserve=self.search_reserve
        )
        if billed:
            self.usage.avm_calls += 1
        if error:
            return None, error
        if not isinstance(payload, dict):
            self.usage.errors += 1
            return None, "RentCast returned a valuation that is not a JSON object."
        return payload, ""

    def get_valuation(self, lead: Lead) -> ProviderResponse[Dict[str, Any]]:
        """RentCast's AVM. A claim, not a verified ARV."""
        payload, error = self._avm(lead)
        if error:
            return ProviderResponse(data=None, supported=True, reason=error, source=self.name)

        value = _number(first_present(payload, AVM_VALUE_CANDIDATE_KEYS))
        if value is None:
            return ProviderResponse.empty(
                self.name,
                "RentCast's valuation response carried no value under any known key. "
                "The estimate stays unknown rather than being guessed at.",
            )
        found: Dict[str, Any] = {"estimated_value": value}
        low = _number(first_present(payload, ("priceRangeLow", "valueRangeLow", "low")))
        high = _number(first_present(payload, ("priceRangeHigh", "valueRangeHigh", "high")))
        if low is not None:
            found["value_range_low"] = low
        if high is not None:
            found["value_range_high"] = high
        return ProviderResponse(
            data=found,
            source=self.name,
            calls=1,
            reason=(
                "RentCast automated valuation. Treated as an unverified ARV claim "
                "until comparable sales support it."
            ),
        )

    def get_comps(
        self, lead: Lead, radius_miles: float = 1.0, months_back: int = 6
    ) -> ProviderResponse[List[Comp]]:
        """The comparables carried by the valuation response.

        RentCast returns comps inside ``/avm/value``, so this shares a request
        with :meth:`get_valuation` rather than costing a second one. The
        ``radius_miles`` and ``months_back`` arguments are accepted for
        interface compatibility and not sent: RentCast selects its own
        comparable set and documents no parameter for either.
        """
        payload, error = self._avm(lead)
        if error:
            return ProviderResponse(data=[], supported=True, reason=error, source=self.name)

        records = first_list(payload, AVM_COMPS_CANDIDATE_KEYS)
        if not records:
            return ProviderResponse.empty(
                self.name,
                "RentCast's valuation response carried no comparable sales. The ARV "
                "stays unverified.",
            )
        comps = [c for c in (to_comp(r) for r in records) if c is not None]
        if not comps:
            return ProviderResponse.empty(
                self.name,
                f"RentCast returned {len(records)} comparable(s), none with both an "
                "address and a price. None were counted.",
            )
        self.metrics.comp_calls += 1
        return ProviderResponse(data=comps, source=self.name, calls=1)

    # ------------------------------------------------------------------

    def health_check(self) -> Tuple[bool, str]:
        """Is RentCast usable right now?

        Deliberately makes **no** network call. On a fifty-request plan, a
        health check that spends one is a health check you cannot afford to
        run, and the two things that actually go wrong — a missing key and a
        spent quota — are both answerable from local state.
        """
        if not os.environ.get(API_KEY_VAR, "").strip():
            return False, f"NOT CONNECTED — {API_KEY_VAR} is not set"
        if self.ledger.exhausted:
            return False, (
                f"CONFIGURED but out of quota — {self.ledger.used}/{self.ledger.limit} "
                f"requests used this month. No request will be made."
            )
        return True, (
            f"credentials present; {self.ledger.remaining} of {self.ledger.limit} "
            "requests remaining this month (local ledger, advisory)"
        )

    def status(self) -> str:
        return "\n".join([
            self.ledger.render(), "", self.cache.stats.render(), "", self.usage.render(),
        ])


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def build(
    settings: Optional[ProviderSettings] = None,
    csv_path: Any = None,
    comps_path: Any = None,
    metrics: Optional[ProviderMetrics] = None,
) -> RentCastProvider:
    """Registry factory. Signature matches every other provider."""
    return RentCastProvider(settings=settings, metrics=metrics)
