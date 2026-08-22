"""Load leads (and their comps) from CSV files.

Two shapes are supported, and they can be combined:

* ``properties.csv`` — one row per lead. An optional ``comps_json`` column can
  carry that lead's comps inline as a JSON array.
* ``comps.csv`` — one row per comparable sale, joined to the lead by
  ``property_id`` (falling back to a normalised address match).

Parsing is deliberately forgiving about formatting ("$185,000", "3 br",
"2024-05-11" vs "5/11/2024") and deliberately strict about invention: a value
that cannot be parsed becomes ``None``, never a guess.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models.enums import Condition, Occupancy, PropertyType, SaleStatus, SellerMotivation
from ..models.property import Comp, PropertyLead

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%Y/%m/%d")

#: Values that mean "the user did not tell us", as opposed to a real zero.
_NULLISH = {"", "n/a", "na", "none", "null", "unknown", "?", "-", "tbd", "unk"}


class LeadParseError(ValueError):
    """Raised when a row cannot be turned into a lead at all."""


@dataclass
class LoadReport:
    """What the loader managed to read, and what it could not."""

    leads: List[PropertyLead] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.leads)


# ---------------------------------------------------------------------------
# Scalar coercion
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None
    return text


def to_float(value: Any) -> Optional[float]:
    """Parse a money-ish or numeric string. Returns ``None`` when unparseable."""
    text = _clean(value)
    if text is None:
        return None
    text = text.replace(",", "").replace("$", "").strip()
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    number = to_float(value)
    return None if number is None else int(round(number))


def to_date(value: Any) -> Optional[date]:
    text = _clean(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_list(value: Any, separators: str = ";|") -> List[str]:
    """Split a multi-value cell (``"probate; vacant"``) into clean items."""
    text = _clean(value)
    if text is None:
        return []
    pattern = "[" + re.escape(separators) + "]"
    return [part.strip() for part in re.split(pattern, text) if part.strip()]


def to_str(value: Any) -> str:
    return _clean(value) or ""


def _first(row: Dict[str, Any], *names: str) -> Any:
    """Return the first present column among ``names`` (case/spacing tolerant)."""
    normalised = {
        re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items() if k is not None
    }
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in normalised:
            value = normalised[key]
            if _clean(value) is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Row -> model
# ---------------------------------------------------------------------------


def comp_from_dict(row: Dict[str, Any]) -> Comp:
    """Build a :class:`Comp` from a CSV row or a JSON object."""
    return Comp(
        address=to_str(_first(row, "comp_address", "address")),
        sale_price=to_float(_first(row, "sale_price", "price", "sold_price", "close_price")),
        sale_status=SaleStatus.parse(to_str(_first(row, "sale_status", "status"))),
        sale_date=to_date(_first(row, "sale_date", "close_date", "date")),
        beds=to_float(_first(row, "beds", "bedrooms", "br")),
        baths=to_float(_first(row, "baths", "bathrooms", "ba")),
        sqft=to_int(_first(row, "sqft", "square_feet", "living_area")),
        year_built=to_int(_first(row, "year_built", "year")),
        lot_size_sqft=to_int(_first(row, "lot_size_sqft", "lot_size", "lot")),
        distance_miles=to_float(_first(row, "distance_miles", "distance", "miles")),
        property_type=PropertyType.parse(to_str(_first(row, "property_type", "type"))),
        condition=Condition.parse(to_str(_first(row, "condition"))),
        source=to_str(_first(row, "source")) or "user-provided",
        notes=to_str(_first(row, "notes")),
    )


def lead_from_dict(row: Dict[str, Any], source: str = "csv") -> PropertyLead:
    """Build a :class:`PropertyLead` from a CSV row or a JSON object."""
    address = to_str(_first(row, "address", "property_address", "street_address"))
    if not address:
        raise LeadParseError("row has no address")

    lead = PropertyLead(
        property_id=to_str(_first(row, "property_id", "id", "lead_id")),
        address=address,
        city=to_str(_first(row, "city")),
        state=to_str(_first(row, "state", "st")),
        county=to_str(_first(row, "county")),
        zip_code=to_str(_first(row, "zip_code", "zip", "postal_code")),
        beds=to_float(_first(row, "beds", "bedrooms", "br")),
        baths=to_float(_first(row, "baths", "bathrooms", "ba")),
        sqft=to_int(_first(row, "sqft", "square_feet", "living_area")),
        lot_size_sqft=to_int(_first(row, "lot_size_sqft", "lot_size", "lot")),
        year_built=to_int(_first(row, "year_built", "year")),
        property_type=PropertyType.parse(to_str(_first(row, "property_type", "type"))),
        occupancy=Occupancy.parse(to_str(_first(row, "occupancy", "occupancy_status"))),
        condition=Condition.parse(
            to_str(_first(row, "condition", "seller_reported_condition", "property_condition"))
        ),
        asking_price=to_float(_first(row, "asking_price", "list_price", "price")),
        user_arv=to_float(_first(row, "arv", "estimated_arv", "user_arv")),
        user_repair_estimate=to_float(
            _first(row, "estimated_repairs", "repairs", "repair_estimate")
        ),
        estimated_monthly_rent=to_float(
            _first(row, "estimated_monthly_rent", "monthly_rent", "rent")
        ),
        annual_taxes=to_float(_first(row, "annual_taxes", "taxes")),
        days_on_market=to_int(_first(row, "days_on_market", "dom")),
        seller_motivation=SellerMotivation.parse(
            to_str(_first(row, "seller_motivation", "motivation"))
        ),
        distress_indicators=to_list(_first(row, "distress_indicators", "distress")),
        notes=to_str(_first(row, "notes", "additional_notes")),
        source=source,
    )

    inline = row.get("comps") if isinstance(row.get("comps"), (list, dict)) else None
    if inline is None:
        inline = _first(row, "comps_json", "comps")
    if inline:
        try:
            payload = inline if isinstance(inline, (list, dict)) else json.loads(str(inline))
            if isinstance(payload, dict):
                payload = [payload]
            lead.comps = [comp_from_dict(item) for item in payload]
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise LeadParseError(f"comps could not be parsed: {exc}") from exc

    return lead


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def _normalise_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def load_comps_csv(path: Path) -> Dict[str, List[Comp]]:
    """Read a comps CSV into ``{join key: [Comp, ...]}``.

    Comps are keyed by ``property_id`` when present and additionally by a
    normalised address so either column can join.
    """
    grouped: Dict[str, List[Comp]] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            key_source = _first(row, "property_id", "id", "lead_id") or _first(
                row, "property_address", "subject_address"
            )
            key = _normalise_key(to_str(key_source))
            if not key:
                continue
            grouped.setdefault(key, []).append(comp_from_dict(row))
    return grouped


def attach_comps(leads: Iterable[PropertyLead], grouped: Dict[str, List[Comp]]) -> None:
    """Attach loaded comps to their leads, in place."""
    for lead in leads:
        for candidate in (lead.property_id, lead.address):
            key = _normalise_key(candidate or "")
            if key and key in grouped:
                lead.comps.extend(grouped[key])
                break


def load_properties_csv(
    path: Path,
    comps_path: Optional[Path] = None,
    strict: bool = False,
) -> LoadReport:
    """Load a properties CSV (plus an optional comps CSV) into leads.

    With ``strict=False`` (the default) an unreadable row is skipped and
    recorded as a warning, so one bad row cannot lose the rest of your batch.
    """
    report = LoadReport()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            if not any(_clean(v) for v in row.values()):
                continue
            try:
                report.leads.append(lead_from_dict(row, source=f"csv:{path.name}"))
            except LeadParseError as exc:
                message = f"{path.name} line {line_number}: skipped ({exc})"
                if strict:
                    raise LeadParseError(message) from exc
                report.warnings.append(message)

    if comps_path:
        grouped = load_comps_csv(comps_path)
        attach_comps(report.leads, grouped)
        matched = sum(1 for lead in report.leads if lead.comps)
        if matched == 0 and grouped:
            report.warnings.append(
                f"{comps_path.name}: no comps matched any lead — check that property_id "
                "values line up between the two files."
            )
    return report


def load_properties_json(path: Path) -> LoadReport:
    """Load leads from a JSON array (or a single JSON object)."""
    report = LoadReport()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows: Sequence[Dict[str, Any]] = payload if isinstance(payload, list) else [payload]
    for index, row in enumerate(rows, start=1):
        try:
            lead = lead_from_dict(row, source=f"json:{Path(path).name}")
        except LeadParseError as exc:
            report.warnings.append(f"{Path(path).name} item {index}: skipped ({exc})")
            continue
        if not lead.comps and isinstance(row.get("comps"), list):
            lead.comps = [comp_from_dict(item) for item in row["comps"]]
        report.leads.append(lead)
    return report
