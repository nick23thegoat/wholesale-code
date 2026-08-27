"""Flat CSV export — one row per analyzed property.

The column set is fixed so the file drops straight into a spreadsheet, a
Google Sheet, or (later) a CRM import without remapping.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..models.results import AnalysisResult

CSV_COLUMNS: List[str] = [
    "address",
    "city",
    "state",
    "asking_price",
    "arv",
    "repair_estimate",
    "mao",
    "recommended_offer",
    "assignment_price",
    "potential_spread",
    "target_wholesale_fee",
    "potential_wholesale_fee",
    "wholesale_fee_at_asking",
    "wholesale_fee_status",
    "deal_score",
    "classification",
    "arv_confidence",
    "comp_confidence",
    "final_decision",
    "risk_flags",
    "missing_data",
]

#: Extra columns written when ``include_detail=True``. Kept out of the default
#: set so the primary export stays exactly as specified.
DETAIL_COLUMNS: List[str] = [
    "county",
    "beds",
    "baths",
    "sqft",
    "year_built",
    "occupancy",
    "condition",
    "repair_low",
    "repair_mid",
    "repair_high",
    "seventy_percent_arv",
    "end_buyer_max_price",
    "binding_wholesale_fee",
    "buyer_margin",
    "mao_vs_asking",
    "offer_discount_pct",
    "comps_supplied",
    "comps_used",
    "needs_more_data",
    "decision_explanation",
]


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def result_to_row(result: AnalysisResult, include_detail: bool = False) -> Dict[str, Any]:
    """Flatten one :class:`AnalysisResult` into a CSV row."""
    lead = result.lead
    fin = result.financials
    row: Dict[str, Any] = {
        "address": lead.address,
        "city": lead.city,
        "state": lead.state,
        "asking_price": _round(lead.asking_price),
        "arv": _round(result.arv.arv),
        "repair_estimate": _round(result.repairs.base),
        "mao": _round(fin.mao),
        "recommended_offer": _round(fin.recommended_offer),
        "assignment_price": _round(fin.assignment_price),
        "potential_spread": _round(fin.potential_gross_spread),
        "target_wholesale_fee": _round(fin.target_wholesale_fee),
        "potential_wholesale_fee": _round(fin.potential_wholesale_fee),
        "wholesale_fee_at_asking": _round(fin.wholesale_fee_at_asking),
        "wholesale_fee_status": str(fin.wholesale_fee_status),
        "deal_score": result.score.total,
        "classification": str(result.score.classification),
        "arv_confidence": str(result.arv.confidence),
        "comp_confidence": str(result.comps.confidence),
        "final_decision": str(result.decision),
        "risk_flags": " | ".join(
            f"{flag.severity}: {flag.message}" for flag in result.flags_by_severity()
        ),
        "missing_data": " | ".join(result.missing_data),
    }
    if include_detail:
        row.update(
            {
                "county": lead.county,
                "beds": lead.beds,
                "baths": lead.baths,
                "sqft": lead.sqft,
                "year_built": lead.year_built,
                "occupancy": str(lead.occupancy),
                "condition": str(lead.condition),
                "repair_low": _round(result.repairs.low),
                "repair_mid": _round(result.repairs.mid),
                "repair_high": _round(result.repairs.high),
                "seventy_percent_arv": _round(fin.seventy_percent_arv),
                "end_buyer_max_price": _round(fin.end_buyer_max_price),
                "binding_wholesale_fee": _round(fin.binding_wholesale_fee),
                "buyer_margin": _round(fin.buyer_margin),
                "mao_vs_asking": _round(fin.spread_vs_asking),
                "offer_discount_pct": round(fin.offer_discount_pct * 100, 1),
                "comps_supplied": result.comps.count,
                "comps_used": result.comps.reliable_count,
                "needs_more_data": result.score.needs_more_data,
                "decision_explanation": result.decision_explanation,
            }
        )
    return row


def write_csv(
    results: Iterable[AnalysisResult],
    path: Path,
    include_detail: bool = False,
) -> Path:
    """Write the analyses to ``path`` and return the path written."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    columns = CSV_COLUMNS + (DETAIL_COLUMNS if include_detail else [])
    with open(destination, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(result_to_row(result, include_detail))
    return destination
