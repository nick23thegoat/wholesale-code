"""Address normalization and duplicate detection.

The same house arrives from different sources as "123 Main Street",
"123 Main St.", and "123 MAIN ST" — one property, three rows. This module
folds those into one normalized form so duplicates collapse, while keeping
apartment/unit numbers intact so that 123 Main St #1 and 123 Main St #2 are
never merged into each other.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from ..models.enums import Condition, Occupancy, PropertyType, SellerMotivation
from .models import SIGNAL_FIELDS, Lead

#: Street-type words folded to their USPS-style abbreviation.
STREET_SUFFIXES: Dict[str, str] = {
    "STREET": "ST", "STR": "ST", "ST": "ST",
    "AVENUE": "AVE", "AVENU": "AVE", "AVEN": "AVE", "AV": "AVE", "AVE": "AVE",
    "ROAD": "RD", "RD": "RD",
    "DRIVE": "DR", "DRV": "DR", "DR": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CRT": "CT", "CT": "CT",
    "CIRCLE": "CIR", "CIRC": "CIR", "CIR": "CIR",
    "HIGHWAY": "HWY", "HIWAY": "HWY", "HWY": "HWY",
    "BOULEVARD": "BLVD", "BOULV": "BLVD", "BLVD": "BLVD",
    "PARKWAY": "PKWY", "PARKWY": "PKWY", "PKWY": "PKWY",
    "PLACE": "PL", "PL": "PL",
    "TERRACE": "TER", "TERR": "TER", "TER": "TER",
    "TRAIL": "TRL", "TRL": "TRL",
    "SQUARE": "SQ", "SQ": "SQ",
    "LOOP": "LOOP",
    "WAY": "WAY",
    "RUN": "RUN",
    "COVE": "CV", "CV": "CV",
    "CROSSING": "XING", "XING": "XING",
}

#: Directionals folded to their abbreviation.
DIRECTIONALS: Dict[str, str] = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "N": "N", "S": "S", "E": "E", "W": "W",
    "NE": "NE", "NW": "NW", "SE": "SE", "SW": "SW",
}

#: Unit designators. These are kept in the normalized address on purpose —
#: dropping them would merge different units of the same building.
UNIT_WORDS: Dict[str, str] = {
    "APARTMENT": "UNIT", "APT": "UNIT", "UNIT": "UNIT", "SUITE": "UNIT",
    "STE": "UNIT", "#": "UNIT", "LOT": "LOT", "BLDG": "BLDG", "BUILDING": "BLDG",
    "FLOOR": "FL", "FL": "FL", "RM": "RM", "ROOM": "RM",
}

_US_STATES: Dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_PUNCTUATION = re.compile(r"[.,;:'\"\\/()\[\]]+")
_WHITESPACE = re.compile(r"\s+")
_HASH_UNIT = re.compile(r"#\s*", flags=re.IGNORECASE)


def normalize_address(raw: Optional[str]) -> str:
    """Fold one street address into a comparable canonical form.

    ``"123 Main Street."`` , ``"123 main st"`` and ``"123  MAIN ST"`` all
    become ``"123 MAIN ST"``. Unit numbers are preserved.
    """
    if not raw:
        return ""
    text = str(raw).upper().strip()
    text = _HASH_UNIT.sub("UNIT ", text)          # "#4" -> "UNIT 4"
    text = text.replace("-", " ")
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""

    tokens = text.split(" ")
    output: List[str] = []
    for index, token in enumerate(tokens):
        if token in UNIT_WORDS:
            output.append(UNIT_WORDS[token])
            continue
        # Directionals only at the ends: "N MAIN ST", "MAIN ST W".
        if token in DIRECTIONALS and (index in (0, 1) or index >= len(tokens) - 2):
            output.append(DIRECTIONALS[token])
            continue
        if token in STREET_SUFFIXES:
            output.append(STREET_SUFFIXES[token])
            continue
        output.append(token)
    return " ".join(output)


def normalize_city(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = _PUNCTUATION.sub(" ", str(raw).upper())
    return _WHITESPACE.sub(" ", text).strip()


def normalize_state(raw: Optional[str]) -> str:
    """Return a two-letter state code, or "" when it cannot be determined."""
    if not raw:
        return ""
    text = _PUNCTUATION.sub(" ", str(raw).upper())
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) == 2:
        return text
    return _US_STATES.get(text, text[:2] if text else "")


def normalize_zip(raw: Optional[str]) -> str:
    """Return the 5-digit ZIP, dropping any +4 extension."""
    if raw is None:
        return ""
    digits = re.sub(r"[^0-9]", "", str(raw))
    if len(digits) >= 5:
        return digits[:5]
    return ""


def normalize_lead(lead: Lead) -> Lead:
    """Populate the normalized address fields on a lead, in place."""
    lead.normalized_address = normalize_address(lead.address)
    lead.normalized_city = normalize_city(lead.city)
    lead.normalized_state = normalize_state(lead.state)
    lead.normalized_zip = normalize_zip(lead.zip_code)
    if lead.normalized_state and lead.state != lead.normalized_state:
        lead.state = lead.normalized_state
    if lead.normalized_zip:
        lead.zip_code = lead.normalized_zip
    return lead


def zips_conflict(first: str, second: str) -> bool:
    """True only when both ZIPs are known and differ."""
    return bool(first) and bool(second) and first != second


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _merge_scalar(primary_value, incoming_value):
    """Keep what we already have; only fill genuine blanks."""
    if primary_value in (None, "", []):
        return incoming_value
    return primary_value


def merge_leads(primary: Lead, duplicate: Lead) -> Lead:
    """Fold ``duplicate`` into ``primary``, filling blanks only.

    Nothing is invented and nothing already known is overwritten. Where the
    two rows disagree on a signal, the positive claim wins (it is the one
    worth verifying) and the conflict is recorded under NEEDS VERIFICATION.
    """
    scalar_fields = (
        "property_id", "address", "city", "state", "county", "zip_code",
        "owner_name", "asking_price", "estimated_value", "estimated_repairs",
        "estimated_equity", "beds", "baths", "sqft", "year_built",
        "days_on_market", "source", "source_url",
    )
    for name in scalar_fields:
        setattr(primary, name, _merge_scalar(getattr(primary, name), getattr(duplicate, name)))

    for name, unknown in (
        ("property_type", PropertyType.UNKNOWN),
        ("occupancy", Occupancy.UNKNOWN),
        ("condition", Condition.UNKNOWN),
        ("seller_motivation", SellerMotivation.UNKNOWN),
    ):
        if getattr(primary, name) is unknown:
            setattr(primary, name, getattr(duplicate, name))

    for name in SIGNAL_FIELDS:
        mine, theirs = getattr(primary, name), getattr(duplicate, name)
        if mine is None:
            setattr(primary, name, theirs)
        elif theirs is not None and mine != theirs:
            setattr(primary, name, True)
            primary.needs_verification.append(
                f"sources disagree on '{name}' ({mine} vs {theirs}); treating it as "
                "reported until verified"
            )

    known = {(c.address, c.sale_price) for c in primary.comps}
    for comp in duplicate.comps:
        if (comp.address, comp.sale_price) not in known:
            primary.comps.append(comp)

    if duplicate.notes and duplicate.notes not in primary.notes:
        primary.notes = f"{primary.notes} | {duplicate.notes}".strip(" |")
    primary.merged_from.append(duplicate.lead_id or duplicate.address)
    return primary


def deduplicate(leads: Iterable[Lead]) -> Tuple[List[Lead], List[Lead]]:
    """Collapse duplicate rows on normalized address + city + state (+ ZIP).

    Returns ``(unique_leads, duplicates_removed)``. Two rows merge only when
    their normalized addresses match exactly and their ZIPs do not contradict
    each other, so distinct properties are never folded together. Leads with
    no usable address are always kept separate — an empty address is not
    evidence that two rows are the same house.
    """
    unique: List[Lead] = []
    removed: List[Lead] = []
    index: Dict[tuple, List[Lead]] = {}

    for lead in leads:
        normalize_lead(lead)
        if not lead.normalized_address:
            unique.append(lead)
            continue

        key = lead.dedupe_key
        match: Optional[Lead] = None
        for candidate in index.get(key, []):
            if not zips_conflict(candidate.normalized_zip, lead.normalized_zip):
                match = candidate
                break

        if match is None:
            index.setdefault(key, []).append(lead)
            unique.append(lead)
        else:
            merge_leads(match, lead)
            normalize_lead(match)
            removed.append(lead)

    return unique, removed
