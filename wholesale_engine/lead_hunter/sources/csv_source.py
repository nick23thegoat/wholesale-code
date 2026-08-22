"""CSV lead source.

Reads a raw lead export and normalizes it into :class:`Lead` objects. Column
naming is forgiving — every field accepts several aliases — because lead lists
arrive from list brokers, county exports and CRM dumps with different headers
every time. A field that is absent stays blank; nothing is filled in.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...data.csv_loader import _first, to_float, to_int, to_str
from ...data.sources import SearchCriteria
from ...models.enums import Condition, Occupancy, PropertyType, SellerMotivation
from ..models import Lead
from .base import BaseLeadSource

#: Column aliases, in priority order, for every field the loader understands.
COLUMN_ALIASES: Dict[str, tuple] = {
    "lead_id": ("lead_id", "id", "record_id"),
    "property_id": ("property_id", "parcel_id", "apn", "parcel"),
    "address": ("address", "property_address", "street_address", "site_address", "situs_address"),
    "city": ("city", "property_city", "situs_city"),
    "state": ("state", "st", "property_state", "situs_state"),
    "county": ("county", "county_name"),
    "zip_code": ("zip_code", "zip", "postal_code", "zipcode", "situs_zip"),
    "owner_name": ("owner_name", "owner", "owner_1", "ownername"),
    "asking_price": ("asking_price", "list_price", "price", "listing_price", "ask"),
    "estimated_value": ("estimated_value", "market_value", "arv", "est_value", "avm", "value"),
    "estimated_repairs": (
        "estimated_repairs", "repairs", "repair_estimate", "rehab", "rehab_estimate",
    ),
    "estimated_equity": ("estimated_equity", "equity", "equity_amount"),
    "beds": ("beds", "bedrooms", "br", "bed"),
    "baths": ("baths", "bathrooms", "ba", "bath"),
    "sqft": ("sqft", "square_feet", "building_sqft", "living_area", "sq_ft"),
    "year_built": ("year_built", "year", "yr_built", "effective_year"),
    "property_type": ("property_type", "type", "land_use", "use_code", "property_use"),
    "occupancy": ("occupancy", "occupancy_status", "occupied"),
    "condition": ("condition", "property_condition", "seller_reported_condition"),
    "days_on_market": ("days_on_market", "dom", "days_listed"),
    "seller_motivation": ("seller_motivation", "motivation", "urgency"),
    "source": ("source", "lead_source", "list_source"),
    "source_url": ("source_url", "url", "listing_url", "link"),
    "notes": ("notes", "note", "comments", "remarks", "additional_notes"),
}

#: Aliases for the tri-state signal columns.
SIGNAL_ALIASES: Dict[str, tuple] = {
    "absentee_owner": ("absentee_owner", "absentee", "out_of_state_owner", "non_owner_occupied"),
    "vacant": ("vacant", "vacant_property", "is_vacant", "vacancy"),
    "tax_delinquent": ("tax_delinquent", "delinquent_taxes", "tax_default", "back_taxes"),
    "pre_foreclosure": ("pre_foreclosure", "preforeclosure", "notice_of_default", "nod", "lis_pendens"),
    "foreclosure": ("foreclosure", "in_foreclosure", "auction", "reo"),
    "probate": ("probate", "probate_case", "estate_sale"),
    "inherited": ("inherited", "heir", "inherited_property"),
    "code_violation": ("code_violation", "code_enforcement", "violation", "citation"),
    "high_equity": ("high_equity", "equity_flag", "free_and_clear"),
    "tired_landlord": ("tired_landlord", "landlord", "burned_out_landlord", "rental_owner"),
}

_TRUE_VALUES = {"y", "yes", "true", "t", "1", "x", "sí", "si"}
_FALSE_VALUES = {"n", "no", "false", "f", "0", "none", "not applicable"}
#: Values that mean "we were not told", as distinct from an explicit "no".
_UNKNOWN_VALUES = {"", "unknown", "unk", "n/a", "na", "?", "-", "tbd", "null"}


def to_tri_bool(value: Any) -> Optional[bool]:
    """Parse a yes/no cell into ``True`` / ``False`` / ``None`` (unknown).

    Blank and "unknown" are *not* the same as "no". Treating them as "no"
    would silently invent a fact about the property.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _UNKNOWN_VALUES:
        return None
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def lead_from_row(row: Dict[str, Any], source: str = "csv") -> Lead:
    """Build a :class:`Lead` from one raw CSV row using the alias table."""

    def value(field: str) -> Any:
        return _first(row, *COLUMN_ALIASES[field])

    lead = Lead(
        lead_id=to_str(value("lead_id")),
        property_id=to_str(value("property_id")),
        address=to_str(value("address")),
        city=to_str(value("city")),
        state=to_str(value("state")),
        county=to_str(value("county")),
        zip_code=to_str(value("zip_code")),
        owner_name=to_str(value("owner_name")),
        asking_price=to_float(value("asking_price")),
        estimated_value=to_float(value("estimated_value")),
        estimated_repairs=to_float(value("estimated_repairs")),
        estimated_equity=to_float(value("estimated_equity")),
        beds=to_float(value("beds")),
        baths=to_float(value("baths")),
        sqft=to_int(value("sqft")),
        year_built=to_int(value("year_built")),
        property_type=PropertyType.parse(to_str(value("property_type"))),
        occupancy=Occupancy.parse(to_str(value("occupancy"))),
        condition=Condition.parse(to_str(value("condition"))),
        days_on_market=to_int(value("days_on_market")),
        seller_motivation=SellerMotivation.parse(to_str(value("seller_motivation"))),
        source=to_str(value("source")) or source,
        source_url=to_str(value("source_url")),
        notes=to_str(value("notes")),
        raw={str(k): str(v) for k, v in row.items() if k is not None},
    )

    for signal, aliases in SIGNAL_ALIASES.items():
        setattr(lead, signal, to_tri_bool(_first(row, *aliases)))

    # "vacant" also arrives as an occupancy value rather than its own column.
    if lead.vacant is None and lead.occupancy is Occupancy.VACANT:
        lead.vacant = True
    if lead.absentee_owner is None and lead.occupancy is Occupancy.TENANT_OCCUPIED:
        # A tenant in place tells us the owner does not live there.
        lead.absentee_owner = True

    if not lead.lead_id:
        lead.lead_id = _derive_lead_id(lead, source)
    return lead


def _derive_lead_id(lead: Lead, source: str) -> str:
    """Stable, readable id derived from the address — not a random value."""
    slug = re.sub(r"[^A-Z0-9]+", "-", (lead.address or "unknown").upper()).strip("-")
    return f"{slug[:28]}" if slug else f"{source}-lead"


class CsvLeadSource(BaseLeadSource):
    """Lead source backed by a local CSV file."""

    is_local = True

    def __init__(self, path: Path, name: Optional[str] = None) -> None:
        self.path = Path(path)
        self.name = name or f"csv:{self.path.name}"
        self.warnings: List[str] = []
        self.rows_read = 0

    def search_leads(self, criteria: Optional[SearchCriteria] = None) -> List[Lead]:
        """Read the file. ``criteria`` is applied by the filter stage, not here,
        so that rejected leads can still be reported instead of vanishing."""
        leads: List[Lead] = []
        self.warnings = []
        self.rows_read = 0
        with open(self.path, newline="", encoding="utf-8-sig") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), start=2):
                if not any(str(v or "").strip() for v in row.values()):
                    continue
                self.rows_read += 1
                lead = lead_from_row(row, source=self.name)
                if not lead.address:
                    self.warnings.append(
                        f"{self.path.name} line {line_number}: no address — kept, but it "
                        "cannot be de-duplicated or analyzed reliably"
                    )
                leads.append(lead)
        return leads

    # Convenience alias so the source also satisfies the Wave 1 LeadSource shape.
    def fetch(self, criteria: Optional[SearchCriteria] = None) -> List[Lead]:
        return self.search_leads(criteria)


def attach_comps(leads: List[Lead], comps_path: Path) -> int:
    """Join a comps CSV onto leads, reusing the Wave 1 comps loader.

    Comps join on ``lead_id``, ``property_id`` or the property address,
    whichever the comps file carries. Returns the number of leads matched.
    With comps attached, the Wave 1 valuation engine can lift an ARV from
    SOURCE-PROVIDED to VERIFIED/SUPPORTED; without them it stays unverified.
    """
    from ...data.csv_loader import _normalise_key, load_comps_csv

    grouped = load_comps_csv(Path(comps_path))
    matched = 0
    for lead in leads:
        for candidate in (lead.lead_id, lead.property_id, lead.address):
            key = _normalise_key(candidate or "")
            if key and key in grouped:
                lead.comps.extend(grouped[key])
                matched += 1
                break
    return matched
