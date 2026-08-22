"""Wave 2 report output: the full lead pipeline CSV and the hot-lead call list."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import DEFAULT_LEAD_CONFIG, LeadHunterConfig
from ..formatting import money
from ..lead_hunter.models import STATUS_ANALYZED, LeadPipelineReport, LeadResult
from ..lead_hunter.pipeline import hot_leads, prioritize

#: The specified pipeline columns, in order.
LEAD_PIPELINE_COLUMNS: List[str] = [
    "lead_id",
    "property_id",
    "address",
    "city",
    "state",
    "county",
    "zip_code",
    "owner_name",
    "asking_price",
    "estimated_value",
    "estimated_repairs",
    "lead_score",
    "lead_classification",
    "deal_score",
    "deal_classification",
    "mao",
    "recommended_offer",
    "potential_assignment_price",
    "potential_spread",
    "final_decision",
    "lead_source",
    "arv_confidence",
    "comp_confidence",
    "risk_flags",
    "missing_data",
]

#: Extra diagnostic columns, appended after the required set.
LEAD_DETAIL_COLUMNS: List[str] = [
    "pipeline_status",
    "lead_signals",
    "unconfirmed_signals",
    "filter_reasons",
    "needs_verification",
    "arv_status",
    "seventy_percent_arv",
    "wholesale_fee",
    "estimated_equity",
    "equity_is_derived",
    "merged_duplicates",
    "property_type",
    "occupancy",
    "condition",
    "decision_explanation",
]


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def lead_result_to_row(result: LeadResult, include_detail: bool = False) -> Dict[str, Any]:
    """Flatten one pipeline result into a CSV row.

    Deal-side columns stay blank when the lead was never analyzed — a blank is
    honest, a zero would read as a real number.
    """
    lead = result.lead
    analysis = result.analysis
    financials = analysis.financials if analysis else None

    row: Dict[str, Any] = {
        "lead_id": lead.lead_id,
        "property_id": lead.property_id,
        "address": lead.address,
        "city": lead.city,
        "state": lead.state,
        "county": lead.county,
        "zip_code": lead.zip_code,
        "owner_name": lead.owner_name,
        "asking_price": _round(lead.asking_price),
        "estimated_value": _round(lead.estimated_value),
        "estimated_repairs": _round(lead.estimated_repairs),
        "lead_score": result.score.total,
        "lead_classification": str(result.score.classification),
        "deal_score": None if analysis is None else analysis.score.total,
        "deal_classification": None if analysis is None else str(analysis.score.classification),
        "mao": _round(financials.mao) if financials else None,
        "recommended_offer": _round(financials.recommended_offer) if financials else None,
        "potential_assignment_price": _round(financials.assignment_price) if financials else None,
        "potential_spread": _round(financials.potential_gross_spread) if financials else None,
        "final_decision": None if analysis is None else str(analysis.decision),
        "lead_source": lead.source,
        "arv_confidence": None if analysis is None else str(analysis.arv.confidence),
        "comp_confidence": None if analysis is None else str(analysis.comps.confidence),
        "risk_flags": (
            " | ".join(f"{flag.severity}: {flag.message}" for flag in analysis.flags_by_severity())
            if analysis
            else ""
        ),
        "missing_data": " | ".join(
            analysis.missing_data if analysis else lead.missing_data
        ),
    }

    if include_detail:
        row.update(
            {
                "pipeline_status": result.status,
                "lead_signals": ", ".join(
                    f"{hit.label} (+{hit.points:g})" for hit in result.score.hits
                ),
                "unconfirmed_signals": ", ".join(result.score.unknown_signals),
                "filter_reasons": " | ".join(result.filter_outcome.reasons),
                "needs_verification": " | ".join(
                    result.filter_outcome.warnings + lead.needs_verification
                ),
                "arv_status": result.arv_status,
                "seventy_percent_arv": (
                    _round(financials.seventy_percent_arv) if financials else None
                ),
                "wholesale_fee": _round(financials.wholesale_fee) if financials else None,
                "estimated_equity": _round(lead.equity_estimate),
                "equity_is_derived": lead.equity_is_derived,
                "merged_duplicates": ", ".join(lead.merged_from),
                "property_type": str(lead.property_type),
                "occupancy": str(lead.occupancy),
                "condition": str(lead.condition),
                "decision_explanation": "" if analysis is None else analysis.decision_explanation,
            }
        )
    return row


def _write(rows: Iterable[Dict[str, Any]], path: Path, columns: List[str]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return destination


def write_lead_pipeline_csv(
    report: LeadPipelineReport,
    path: Path,
    include_detail: bool = True,
    hot_only: bool = False,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
) -> Path:
    """Write every lead that survived de-duplication, ranked best first.

    Filtered-out leads are included with a blank deal side and their reason in
    ``filter_reasons``: a lead you rejected is information, not noise.
    """
    results = (
        hot_leads(report, lead_config) if hot_only else prioritize(report.results)
    )
    columns = LEAD_PIPELINE_COLUMNS + (LEAD_DETAIL_COLUMNS if include_detail else [])
    rows = (lead_result_to_row(result, include_detail) for result in results)
    return _write(rows, path, columns)


def write_hot_leads_csv(
    report: LeadPipelineReport,
    path: Path,
    include_detail: bool = True,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
) -> Path:
    """Write only 🔥 HOT and 🟠 STRONG leads, ranked by deal score, then lead
    score, then potential spread."""
    columns = LEAD_PIPELINE_COLUMNS + (LEAD_DETAIL_COLUMNS if include_detail else [])
    rows = (
        lead_result_to_row(result, include_detail)
        for result in hot_leads(report, lead_config)
    )
    return _write(rows, path, columns)


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

WIDTH = 100


def render_lead_summary(
    report: LeadPipelineReport,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    hot_only: bool = False,
) -> str:
    """One line per lead, best opportunities first."""
    lines = [
        "=" * WIDTH,
        f"LEAD PIPELINE — {report.rows_read} row(s) read from {report.source_name or 'source'}, "
        f"{len(report.results)} unique propert{'y' if len(report.results) == 1 else 'ies'}",
        "=" * WIDTH,
        f"{'ADDRESS':<30}{'ST':<4}{'LEAD':>6} {'CLASS':<11}{'DEAL':>6} "
        f"{'DECISION':<18}{'OFFER':>12}{'SPREAD':>11}",
        "-" * WIDTH,
    ]

    shown = hot_leads(report, lead_config) if hot_only else prioritize(report.results)
    for result in shown:
        lead = result.lead
        address = (lead.address or lead.display_id())[:29]
        deal = "  —  " if result.deal_score is None else f"{result.deal_score:5.1f}"
        if result.status != STATUS_ANALYZED:
            decision = result.status.replace("_", " ")[:17]
            offer = spread = "—"
        else:
            analysis = result.analysis
            decision = str(analysis.decision)[:17]
            offer = money(analysis.financials.recommended_offer, unknown="—")
            spread = money(analysis.financials.potential_gross_spread, unknown="—")
        lines.append(
            f"{address:<30}{lead.state or '--':<4}{result.score.total:>6.1f} "
            f"{str(result.score.classification):<11}{deal:>6} {decision:<18}"
            f"{offer:>12}{spread:>11}"
        )

    hot = hot_leads(report, lead_config)
    lines.append("-" * WIDTH)
    lines.append(
        f"{len(report.analyzed)} analyzed · {len(report.filtered_out)} filtered out · "
        f"{len(report.duplicates)} duplicate row(s) merged · {len(hot)} hot/strong lead(s)"
    )
    lines.append(
        "LEAD score = worth a call. DEAL score = worth a contract. They are not the "
        "same test, and a hot lead can still be a bad deal."
    )
    lines.append("=" * WIDTH)
    return "\n".join(lines)
