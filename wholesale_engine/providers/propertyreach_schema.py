"""PropertyReach wire format: what is confirmed, and what still needs the docs.

This module is the single place the vendor's shape is declared, so the adapter
in :mod:`wholesale_engine.providers.propertyreach` contains no magic strings
and filling in the rest is an edit here rather than a rewrite.

WHAT IS CONFIRMED
-----------------
From PropertyReach's own published documentation and API page:

* base URL           ``https://api.propertyreach.com/v1``
* authentication     an ``x-api-key`` request header
* transport          REST, JSON responses
* skip trace         ``POST /v1/skip-trace``
* rate limits        per-plan; the vendor does not publish a fixed number, so
                     the client's own conservative limiter governs us
* operations offered Property Search, Property Detail, Comparables, Skip Trace

WHAT IS NOT CONFIRMED
---------------------
The exact REST paths for Property Search, Property Detail and Comparables, the
request parameter names, and the response field names. ``docs.propertyreach.com``
is not reachable from this environment, and those specifics are not in any
public index.

They are therefore marked ``verified=False`` below and **the adapter refuses to
call an unverified endpoint against the live API**. It will not guess a path and
send a request that 404s, or worse, hits something unintended.

The naming convention is *suggested* by the one confirmed path (`/v1/skip-trace`
— kebab-case under `/v1`), and the candidates below follow it. They are
candidates, not facts.

HOW TO FINISH THIS
------------------
Open https://docs.propertyreach.com, and for each operation fill in:

1. ``path`` and ``method`` from the endpoint reference
2. ``request`` — our criteria name -> their parameter name
3. ``response`` — our model field -> their JSON key (dotted paths supported)
4. flip ``verified`` to True

Then run the mocked tests with a real sample response pasted in. Nothing else
in the engine changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Confirmed
# ---------------------------------------------------------------------------

#: PropertyReach's published API root.
DEFAULT_BASE_URL = "https://api.propertyreach.com/v1"

#: The header PropertyReach authenticates with. Not Bearer.
AUTH_HEADER = "x-api-key"

#: The auth header carries the bare key, with no scheme prefix.
AUTH_SCHEME = ""

#: Environment variables this adapter reads. Never hard-code a key.
API_KEY_VAR = "PROPERTYREACH_API_KEY"
BASE_URL_VAR = "PROPERTYREACH_BASE_URL"

#: PropertyReach publishes no fixed rate-limit number — it varies by plan — so
#: the client imposes its own. Raise only if your plan documents more headroom.
MIN_SECONDS_BETWEEN_CALLS = 1.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """One PropertyReach operation."""

    name: str
    path: str
    method: str = "POST"
    #: True only when the path and method come from the vendor's documentation.
    verified: bool = False
    note: str = ""

    def describe(self) -> str:
        state = "CONFIRMED" if self.verified else "UNVERIFIED"
        return f"{self.method} /{self.path}  [{state}]"


#: The four operations PropertyReach documents. Only Skip Trace has a path we
#: can confirm; the rest carry candidates drawn from that naming convention.
ENDPOINTS: Dict[str, Endpoint] = {
    "search": Endpoint(
        name="Property Search",
        path="property-search",
        method="POST",
        verified=False,
        note="Path follows the /v1/skip-trace convention but is not confirmed.",
    ),
    "detail": Endpoint(
        name="Property Detail",
        path="property-detail",
        method="POST",
        verified=False,
        note="Path follows the /v1/skip-trace convention but is not confirmed.",
    ),
    "comps": Endpoint(
        name="Comparables",
        path="comparables",
        method="POST",
        verified=False,
        note="Operation name confirmed; REST path not confirmed.",
    ),
    "skip_trace": Endpoint(
        name="Skip Trace",
        path="skip-trace",
        method="POST",
        verified=True,
        note="Confirmed: POST https://api.propertyreach.com/v1/skip-trace",
    ),
}


# ---------------------------------------------------------------------------
# Request parameters — our criteria name -> their parameter name
# ---------------------------------------------------------------------------

#: UNVERIFIED. PropertyReach advertises 130+ search filters; these are the ones
#: our HuntCriteria needs. Replace the right-hand side from the docs.
SEARCH_PARAMS: Dict[str, str] = {
    "states": "state",
    "counties": "county",
    "cities": "city",
    "zip_codes": "zip",
    "property_types": "propertyType",
    "min_price": "minValue",
    "max_price": "maxValue",
    "min_equity": "minEquity",
    "limit": "limit",
    "page": "page",
}

#: UNVERIFIED. Distress filters, if the vendor exposes them as search filters.
SEARCH_SIGNAL_PARAMS: Dict[str, str] = {
    "vacant": "vacant",
    "absentee_owner": "absenteeOwner",
    "high_equity": "highEquity",
    "pre_foreclosure": "preForeclosure",
    "foreclosure": "foreclosure",
    "tax_delinquent": "taxDelinquent",
    "probate": "probate",
    "inherited": "inherited",
    "code_violation": "codeViolation",
    "tired_landlord": "tiredLandlord",
}


# ---------------------------------------------------------------------------
# Response mapping — our model field -> candidate JSON keys
# ---------------------------------------------------------------------------
#
# Each entry lists candidate keys in preference order. Dotted paths walk nested
# objects. **A field whose candidates all miss stays unknown** — the mapper
# never substitutes a default, so an unmapped field behaves exactly like a
# field the vendor did not return, which the engine already handles correctly
# everywhere (unknown never scores and never rejects).


@dataclass(frozen=True)
class FieldMap:
    """One of our fields and the vendor keys that might carry it."""

    field: str
    candidates: Tuple[str, ...]
    kind: str = "str"  # str | float | int | bool | date
    verified: bool = False


def _m(field_name: str, *candidates: str, kind: str = "str") -> FieldMap:
    return FieldMap(field=field_name, candidates=candidates, kind=kind)


#: UNVERIFIED response mapping. Candidates follow common conventions for this
#: kind of API; replace them with the exact keys from a real sample response.
PROPERTY_FIELDS: Tuple[FieldMap, ...] = (
    # --- identity and geography ---
    _m("property_id", "propertyId", "id", "apn", "parcelId"),
    _m("address", "address.line1", "addressLine1", "propertyAddress", "address"),
    _m("city", "address.city", "city", "propertyCity"),
    _m("state", "address.state", "state", "propertyState"),
    _m("zip_code", "address.zip", "zip", "zipCode", "propertyZip"),
    _m("county", "address.county", "county", "countyName"),
    # --- physical ---
    _m("property_type", "propertyType", "landUse", "useCode"),
    _m("beds", "bedrooms", "beds", "bedroomCount", kind="float"),
    _m("baths", "bathrooms", "baths", "bathroomCount", kind="float"),
    _m("sqft", "buildingSqft", "livingArea", "squareFeet", "buildingArea", kind="int"),
    _m("year_built", "yearBuilt", "effectiveYearBuilt", kind="int"),
    _m("lot_size", "lotSizeSqft", "lotSize", "landSqft", kind="float"),
    # --- valuation and price ---
    _m("estimated_value", "estimatedValue", "avm", "marketValue", "valuation", kind="float"),
    _m("asking_price", "listPrice", "listingPrice", "price", kind="float"),
    _m("last_sale_price", "lastSalePrice", "saleAmount", "priorSalePrice", kind="float"),
    _m("last_sale_date", "lastSaleDate", "saleDate", "priorSaleDate", kind="date"),
    _m("estimated_rent", "estimatedRent", "rentEstimate", "rentalValue", kind="float"),
    # --- ownership ---
    _m("owner_name", "owner.name", "ownerName", "ownerFullName", "owner1FullName"),
    _m("owner_mailing_address", "owner.mailingAddress", "mailingAddress",
       "ownerMailingAddress", "mailAddress.line1"),
    _m("owner_occupied", "ownerOccupied", "isOwnerOccupied", kind="bool"),
    _m("ownership_years", "yearsOwned", "ownershipLength", kind="float"),
    _m("properties_owned", "portfolioSize", "propertiesOwned", kind="int"),
    # --- money owed ---
    _m("mortgage_balance", "mortgageBalance", "openMortgageBalance",
       "estimatedMortgageBalance", "totalOpenLienBalance", kind="float"),
    _m("liens", "lienAmount", "totalLiens", "openLienAmount", kind="float"),
    _m("estimated_equity", "estimatedEquity", "equity", "availableEquity", kind="float"),
    _m("equity_percent", "equityPercent", "equityPercentage", kind="float"),
    # --- tax ---
    _m("tax_amount", "taxAmount", "annualTaxes", "taxBilledAmount", kind="float"),
    _m("assessed_value", "assessedValue", "totalAssessedValue", kind="float"),
    _m("tax_delinquent", "taxDelinquent", "isTaxDelinquent", "taxLien", kind="bool"),
    _m("tax_year", "taxYear", "assessmentYear", kind="int"),
    # --- distress ---
    _m("vacant", "vacant", "isVacant", "vacancyFlag", kind="bool"),
    _m("absentee_owner", "absenteeOwner", "isAbsenteeOwner", kind="bool"),
    _m("high_equity", "highEquity", "isHighEquity", kind="bool"),
    _m("pre_foreclosure", "preForeclosure", "isPreForeclosure", "noticeOfDefault", kind="bool"),
    _m("foreclosure", "foreclosure", "isForeclosure", "inForeclosure", kind="bool"),
    _m("auction_date", "auctionDate", "foreclosureAuctionDate", kind="date"),
    _m("probate", "probate", "isProbate", kind="bool"),
    _m("inherited", "inherited", "isInherited", kind="bool"),
    _m("code_violation", "codeViolation", "hasCodeViolation", kind="bool"),
    _m("tired_landlord", "tiredLandlord", "isTiredLandlord", kind="bool"),
    # --- market ---
    _m("days_on_market", "daysOnMarket", "dom", kind="int"),
    _m("mls_status", "mlsStatus", "listingStatus"),
)

#: UNVERIFIED. Comparable-sale fields.
COMP_FIELDS: Tuple[FieldMap, ...] = (
    _m("address", "address.line1", "addressLine1", "address"),
    _m("sale_price", "salePrice", "lastSalePrice", "closePrice", kind="float"),
    _m("sale_date", "saleDate", "lastSaleDate", "closeDate", kind="date"),
    _m("beds", "bedrooms", "beds", kind="float"),
    _m("baths", "bathrooms", "baths", kind="float"),
    _m("sqft", "buildingSqft", "livingArea", "squareFeet", kind="int"),
    _m("year_built", "yearBuilt", kind="int"),
    _m("distance_miles", "distance", "distanceMiles", kind="float"),
    _m("property_type", "propertyType", "landUse"),
)

#: UNVERIFIED. Where the list of results sits in a search response.
RESULT_LIST_KEYS: Tuple[str, ...] = ("results", "data", "properties", "items", "records")

#: UNVERIFIED. Where a total count sits, for paging.
TOTAL_COUNT_KEYS: Tuple[str, ...] = ("totalCount", "total", "count", "resultCount")

#: UNVERIFIED. Where comparable sales sit in a comps response.
COMP_LIST_KEYS: Tuple[str, ...] = ("comparables", "comps", "results", "data")


def unverified_endpoints() -> List[str]:
    """Operations whose path still has to come from the documentation."""
    return [key for key, endpoint in ENDPOINTS.items() if not endpoint.verified]


def schema_status() -> str:
    """What is confirmed and what is outstanding, for ``--provider-status``."""
    lines = [
        "PROPERTYREACH SCHEMA",
        "",
        "  CONFIRMED from PropertyReach's published documentation:",
        f"    base URL          {DEFAULT_BASE_URL}",
        f"    authentication    {AUTH_HEADER} request header",
        "    transport         REST, JSON",
        f"    skip trace        {ENDPOINTS['skip_trace'].describe()}",
        "",
        "  ENDPOINTS",
    ]
    for key, endpoint in ENDPOINTS.items():
        lines.append(f"    {endpoint.name:<18}{endpoint.describe()}")
        if endpoint.note:
            lines.append(f"    {'':<18}{endpoint.note}")
    lines += [
        "",
        f"  RESPONSE MAPPING    {len(PROPERTY_FIELDS)} property fields, "
        f"{len(COMP_FIELDS)} comp fields — ALL UNVERIFIED",
        "",
        "  An unverified endpoint is refused against the live API rather than",
        "  guessed at. Fill in propertyreach_schema.py from the docs at",
        "  https://docs.propertyreach.com and flip verified=True.",
    ]
    return "\n".join(lines)
