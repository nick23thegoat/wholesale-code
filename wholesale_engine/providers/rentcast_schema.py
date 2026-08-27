"""RentCast wire format: endpoints, auth, and field mapping.

Everything here is labelled CONFIRMED, UNVERIFIED, or NOT KNOWN, per the
project's standing no-invention rule. CONFIRMED facts come from RentCast's
published API documentation (base URL, auth header, billing behaviour, the
endpoint paths and the query parameters). UNVERIFIED facts are documented
field *names* never checked against a live response from this account.
NOT KNOWN means the shape is genuinely unknown.

The first live call should be made with ``scripts/rentcast_probe.py`` (spends
exactly one request) and the resulting field inventory pasted back, so this
file can be corrected in one place.
"""

from __future__ import annotations

from typing import Dict, Optional

#: CONFIRMED -- from RentCast's published docs.
BASE_URL = "https://api.rentcast.io/v1"
AUTH_HEADER = "X-Api-Key"
AUTH_SCHEME = ""  # bare key, no "Bearer" prefix
API_KEY_VAR = "RENTCAST_API_KEY"
BASE_URL_VAR = "RENTCAST_BASE_URL"

#: CONFIRMED -- endpoint paths.
SEARCH_PATH = "properties"
VALUATION_PATH = "avm/value"

#: CONFIRMED -- billing. Only successful requests count; the free tier is
#: 50/month. 401/403/429/timeouts are never billed, which is why the quota
#: ledger records a request only after a 2xx.
FREE_TIER_MONTHLY_LIMIT = 50

#: CONFIRMED -- /properties max page size. Still ONE billed request, which is
#: the single most important fact about using this API economically.
MAX_PAGE_SIZE = 500

#: CONFIRMED -- documented /properties query parameters. Anything not on this
#: list is not sent: an undocumented parameter is either ignored (wasting the
#: filter) or an error (wasting the request).
SEARCH_PARAMS = (
    "address", "city", "state", "zipCode",
    "latitude", "longitude", "radius",
    "propertyType", "bedrooms", "bathrooms",
    "limit", "offset",
)

#: CONFIRMED -- multi-value parameters are pipe-separated (e.g. "1|3").
MULTI_VALUE_SEPARATOR = "|"

#: NOT KNOWN -- RentCast does not document a price filter on /properties, so
#: the engine does NOT send one. The buy box price range is applied locally by
#: the funnel's cheap filter instead. That costs nothing extra: a request
#: returns up to 500 records whatever the filters, so narrowing locally spends
#: the same single request.
PRICE_FILTER_SUPPORTED = False

# ---------------------------------------------------------------------
# Property types
# ---------------------------------------------------------------------
# The engine's vocabulary is lower_snake_case (HuntCriteria lowercases it);
# RentCast's is Title Case. Both directions are needed and they are NOT
# symmetrical -- several of ours have no RentCast equivalent, and sending an
# unrecognised value would silently match nothing.

#: UNVERIFIED -- RentCast's documented propertyType values, mapped to ours.
RENTCAST_TO_ENGINE: Dict[str, str] = {
    "Single Family": "single_family",
    "Condo": "condo",
    "Townhouse": "townhouse",
    "Multi-Family": "multi_family",
    "Manufactured": "mobile",
    "Apartment": "multi_family",
    "Land": "land",
}

#: Ours -> RentCast, for building a request. Types with no RentCast equivalent
#: are absent on purpose: :func:`to_rentcast_types` drops them rather than
#: sending a value the API does not recognise.
ENGINE_TO_RENTCAST: Dict[str, str] = {
    "single_family": "Single Family",
    "condo": "Condo",
    "townhouse": "Townhouse",
    "multi_family": "Multi-Family",
    "mobile": "Manufactured",
    "land": "Land",
}


def to_rentcast_types(engine_types) -> Optional[str]:
    """Our property types as a RentCast ``propertyType`` value.

    Returns ``None`` when nothing maps, which means "send no filter" -- a
    wider search is recoverable, a filter RentCast cannot parse is a wasted
    request. Duplex/triplex/fourplex all fold to Multi-Family, so the result
    is de-duplicated while keeping a stable order.
    """
    seen = []
    for name in engine_types or ():
        mapped = ENGINE_TO_RENTCAST.get(str(name).strip().lower())
        if mapped and mapped not in seen:
            seen.append(mapped)
    return MULTI_VALUE_SEPARATOR.join(seen) if seen else None


# ---------------------------------------------------------------------
# /properties response fields -- UNVERIFIED against a live response.
# A key not present in a real response simply never gets set; the Lead field
# stays at its default (None / "" / UNKNOWN) and is reported as a gap.
# ---------------------------------------------------------------------
PROPERTY_FIELD_MAP_UNVERIFIED = {
    "id": "property_id",
    "formattedAddress": "address",
    "city": "city",
    "state": "state",
    "county": "county",
    "zipCode": "zip_code",
    "bedrooms": "beds",
    "bathrooms": "baths",
    "squareFootage": "sqft",
    "yearBuilt": "year_built",
    "propertyType": "property_type",
    "ownerOccupied": "_owner_occupied",  # inverted -> absentee_owner
}

#: UNVERIFIED -- owner sub-object, per RentCast's docs:
#: ``owner: {names: [...], type: str, mailingAddress: {...}}``
#: Ownership of record only. Never phone or email -- that is skip tracing,
#: which lives behind its own interface.
OWNER_FIELDS_UNVERIFIED = ("names", "type", "mailingAddress")

#: UNVERIFIED -- tax maps are year-keyed, e.g. ``taxAssessments: {"2024": {...}}``.
TAX_FIELDS_UNVERIFIED = ("taxAssessments", "propertyTaxes")

#: UNVERIFIED -- sale history fields.
SALE_FIELDS_UNVERIFIED = ("lastSaleDate", "lastSalePrice", "history")

# ---------------------------------------------------------------------
# /avm/value -- response shape NOT KNOWN. Never checked live.
# The adapter tries each candidate key in order; if none match, the valuation
# stays None and a reason is returned. It never fabricates a default.
# ---------------------------------------------------------------------
AVM_FIELDS_CONFIRMED = False
AVM_VALUE_CANDIDATE_KEYS = ("price", "value", "estimatedValue", "avm")
AVM_COMPS_CANDIDATE_KEYS = ("comparables", "comps")

#: NOT KNOWN -- does /avm/value return comparables inline? If it does, a
#: valuation and its comps cost one request instead of two, which matters a
#: great deal at 50/month. The adapter checks for the comps keys in the same
#: response before ever making a separate call, so this resolves itself on
#: first live use rather than needing a guess now.
COMPARABLES_INCLUDED_IN_AVM = None  # None = unknown, NOT False
