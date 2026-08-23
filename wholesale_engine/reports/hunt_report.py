"""Wave 4 hunt outputs: four CSVs, one JSON, and a console summary.

Every row states what is known, how confident the engine is in it, and what is
missing — so no number in these files can be read without its provenance:

* separate LEAD score and DEAL score (never merged: one says "call them", the
  other says "buy it")
* three confidence readings — data, ARV, comp
* the fee quantities kept apart: target, potential fee, deal cushion, MAO,
  recommended offer
* the final decision, the risk flags behind it, and the gaps still open
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..formatting import money
from ..lead_hunter.models import STATUS_ANALYZED, LeadResult
from ..outputs import CsvAdapter, JsonAdapter
from ..storage import ChangeSet

#: Output names, written under ``reports/output/``.
DAILY_LEADS = "daily_leads"
HOT_LEADS = "hot_leads"
DEALS_TO_REVIEW = "deals_to_review"
REJECTED_LEADS = "rejected_leads"

HUNT_COLUMNS: List[str] = [
    "lead_id",
    "address",
    "city",
    "state",
    "zip_code",
    "county",
    "property_type",
    "source",
    "status",
    # --- the two scores, never merged ---
    "lead_score",
    "lead_classification",
    "deal_score",
    "deal_classification",
    # --- confidence ---
    "data_confidence",
    "arv_confidence",
    "comp_confidence",
    "arv_status",
    # --- economics, each quantity distinct ---
    "arv",
    "repair_estimate",
    "asking_price",
    "end_buyer_ceiling",
    "target_wholesale_fee",
    "mao",
    "recommended_offer",
    "deal_cushion",
    "potential_wholesale_fee",
    "wholesale_fee_at_asking",
    "wholesale_fee_status",
    "assignment_price",
    # --- outcome ---
    "final_decision",
    "risk_flags",
    "missing_data",
    "needs_verification",
    # --- across-run memory ---
    "first_seen",
    "change_summary",
    "priority",
]


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _data_confidence(result: LeadResult) -> Optional[str]:
    """The analyzer's own data-confidence component, as a percentage."""
    if result.analysis is None:
        return None
    for component in result.analysis.score.components:
        if component.name == "Data confidence":
            if component.weight <= 0:
                return None
            return f"{component.score * 100:.0f}%"
    return None


def hunt_row(
    result: LeadResult,
    change: Optional[ChangeSet] = None,
    priority: Optional[float] = None,
) -> Dict[str, Any]:
    """Flatten one lead-plus-analysis into a hunt output row."""
    lead = result.lead
    analysis = result.analysis
    financials = analysis.financials if analysis else None

    return {
        "lead_id": lead.lead_id or lead.display_id(),
        "address": lead.address,
        "city": lead.city,
        "state": lead.state,
        "zip_code": lead.zip_code,
        "county": lead.county,
        "property_type": str(lead.property_type),
        "source": lead.source,
        "status": result.status,
        "lead_score": result.score.total,
        "lead_classification": str(result.score.classification),
        "deal_score": None if analysis is None else analysis.score.total,
        "deal_classification": None if analysis is None else str(analysis.score.classification),
        "data_confidence": _data_confidence(result),
        "arv_confidence": None if analysis is None else str(analysis.arv.confidence),
        "comp_confidence": None if analysis is None else str(analysis.comps.confidence),
        "arv_status": result.arv_status,
        "arv": None if analysis is None else _round(analysis.arv.arv),
        "repair_estimate": None if analysis is None else _round(analysis.repairs.base),
        "asking_price": _round(lead.asking_price),
        "end_buyer_ceiling": _round(financials.end_buyer_max_price) if financials else None,
        "target_wholesale_fee": _round(financials.target_wholesale_fee) if financials else None,
        "mao": _round(financials.mao) if financials else None,
        "recommended_offer": _round(financials.recommended_offer) if financials else None,
        # MAO - offer. Cushion, never the fee.
        "deal_cushion": _round(financials.potential_gross_spread) if financials else None,
        # Fee at the recommended offer, per the wholesale-economics definition.
        "potential_wholesale_fee": (
            _round(financials.potential_wholesale_fee) if financials else None
        ),
        # Fee if the seller will not move. The decisive one.
        "wholesale_fee_at_asking": (
            _round(financials.wholesale_fee_at_asking) if financials else None
        ),
        "wholesale_fee_status": (
            str(financials.wholesale_fee_status) if financials else "UNKNOWN"
        ),
        "assignment_price": _round(financials.assignment_price) if financials else None,
        "final_decision": None if analysis is None else str(analysis.decision),
        "risk_flags": (
            " | ".join(
                f"{flag.severity}: {flag.message}" for flag in analysis.flags_by_severity()
            )
            if analysis
            else ""
        ),
        "missing_data": " | ".join(
            analysis.missing_data if analysis else lead.missing_data
        ),
        "needs_verification": " | ".join(
            result.filter_outcome.warnings + lead.needs_verification
        ),
        "first_seen": "",  # filled by the writer from the store when available
        "change_summary": change.summary() if change else "",
        "priority": None if priority is None else round(priority, 1),
    }


def split_outputs(
    results: Sequence[LeadResult],
    rows: Dict[int, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Sort finished rows into the four named output files.

    * ``daily_leads``      — everything the hunt touched, best first
    * ``hot_leads``        — analyzed, HOT/STRONG lead score, decision GO
    * ``deals_to_review``  — analyzed and worth a look, but not a green light
    * ``rejected_leads``   — filtered out or scored below the gates, with why
    """
    daily: List[Dict[str, Any]] = []
    hot: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for result in results:
        row = rows[id(result)]
        daily.append(row)
        if result.status != STATUS_ANALYZED:
            rejected.append(row)
            continue
        decision = str(result.analysis.decision) if result.analysis else ""
        if result.is_hot_lead and "GO" in decision:
            hot.append(row)
        else:
            review.append(row)
    return {
        DAILY_LEADS: daily,
        HOT_LEADS: hot,
        DEALS_TO_REVIEW: review,
        REJECTED_LEADS: rejected,
    }


def write_hunt_outputs(
    hunt_result,
    directory: Path,
    write_json: bool = True,
) -> Dict[str, Path]:
    """Write every hunt output. Returns ``name -> path``."""
    directory = Path(directory)
    rows = {
        id(result): hunt_row(
            result,
            hunt_result.change_for(result.lead),
            hunt_result.priority_of(result),
        )
        for result in hunt_result.prioritized
    }
    datasets = split_outputs(hunt_result.prioritized, rows)

    csv_adapter = CsvAdapter(directory)
    written: Dict[str, Path] = {}
    for label, dataset in datasets.items():
        written[label] = csv_adapter.publish(dataset, HUNT_COLUMNS, label)

    if write_json:
        json_adapter = JsonAdapter(
            directory,
            meta={
                "provider": hunt_result.provider_name,
                "criteria": hunt_result.criteria.describe() if hunt_result.criteria else "",
                "provider_calls": hunt_result.metrics.as_dict(),
            },
        )
        written[DAILY_LEADS + ".json"] = json_adapter.publish(
            datasets[DAILY_LEADS], HUNT_COLUMNS, DAILY_LEADS
        )
    return written


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

WIDTH = 118


def render_hunt_summary(hunt_result, limit: Optional[int] = None) -> str:
    """One line per lead, highest working priority first."""
    lines = [
        "=" * WIDTH,
        f"HUNT — source {hunt_result.provider_name}; {hunt_result.criteria.describe() if hunt_result.criteria else ''}",
        "=" * WIDTH,
    ]
    for notice in hunt_result.notices:
        lines.append(f"NOTE: {notice}")
    for warning in hunt_result.warnings + hunt_result.report.warnings:
        lines.append(f"WARNING: {warning}")
    lines.append(
        f"{'ADDRESS':<28}{'ST':<4}{'LEAD':>6}{'DEAL':>6}  {'DECISION':<18}"
        f"{'ASKING':>10}{'OFFER':>10}{'FEE@ASK':>10}  {'FEE STATUS':<14}{'CHANGE':<12}"
    )
    lines.append("-" * WIDTH)

    shown = hunt_result.prioritized[:limit] if limit else hunt_result.prioritized
    for result in shown:
        lead = result.lead
        analysis = result.analysis
        change = hunt_result.change_for(lead)
        if result.status != STATUS_ANALYZED or analysis is None:
            decision = result.status.replace("_", " ")[:17]
            offer = fee = "—"
            fee_status = ""
        else:
            financials = analysis.financials
            decision = str(analysis.decision)[:17]
            offer = money(financials.recommended_offer, unknown="—")
            fee = money(financials.wholesale_fee_at_asking, unknown="—")
            fee_status = str(financials.wholesale_fee_status)
        deal = "  —  " if result.deal_score is None else f"{result.deal_score:5.1f}"
        flag = "NEW" if change and change.is_new else ""
        if change and change.has_changes:
            flag = f"+{change.priority_bump:.0f} CHANGED" if change.priority_bump else "CHANGED"
        lines.append(
            f"{(lead.address or lead.display_id())[:27]:<28}{lead.state or '--':<4}"
            f"{result.score.total:>6.1f}{deal:>6}  {decision:<18}"
            f"{money(lead.asking_price, unknown='—'):>10}{offer:>10}{fee:>10}  "
            f"{fee_status:<14}{flag:<12}"
        )
    lines.append("-" * WIDTH)

    changed = [c for c in hunt_result.changes.values() if c.has_changes]
    if changed:
        lines.append("CHANGES SINCE LAST RUN")
        for change in sorted(changed, key=lambda c: -c.priority_bump):
            lines.append("  " + change.render().replace("\n", "\n  "))
        lines.append("-" * WIDTH)

    lines.append(hunt_result.metrics.render())
    lines.append("=" * WIDTH)
    return "\n".join(lines)
