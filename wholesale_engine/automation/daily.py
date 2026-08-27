"""``--daily``: the whole acquisitions day, in one command.

Thirteen steps, in the order money is made and lost::

     1. pull new leads from the configured source
     2. deduplicate against everything already stored
     3. detect what changed on leads we already had
     4. score the leads
     5. research the ones that earned it        (billable, capped)
     6. calculate the deals                     (Wave 1, unchanged)
     7. rank the opportunities
     8. identify hot leads
     9. identify follow-ups due
    10. identify seller counters
    11. identify offers needing attention
    12. identify contracts needing attention
    13. export the daily report

Steps 1-7 are the existing hunt, unchanged and not duplicated. Steps 8-12 read
the acquisition side. Nothing here re-implements any scoring or deal maths —
this is sequencing and reporting.

Safe to run on a schedule: it makes no outbound contact, sends nothing, and
respects every budget cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..acquisitions import AcquisitionWorkflow, is_closed, normalize_status
from ..budget import ApiBudget, UsageReport
from ..config import DEFAULT_CONFIG, DEFAULT_LEAD_CONFIG, EngineConfig, LeadHunterConfig
from ..formatting import money
from ..hunt import HuntBudget, HuntResult, run_hunt
from ..integrations.notifications import EventType, NotificationCenter
from ..providers import HuntCriteria, PropertyDataProvider
from ..runtime import RuntimeConfig
from ..storage import LeadStore
from .daily_priority import DailyPriorityEngine, PriorityItem
from .monitoring import DealChange, monitor

#: Deal score that makes a freshly-found lead worth surfacing as HOT.
HOT_DEAL_SCORE = 75.0
#: Deal score for the STRONG band.
STRONG_DEAL_SCORE = 60.0


@dataclass
class DailyReport:
    """Everything one daily run found. Rendered, exported, and notified on."""

    run_date: date = field(default_factory=date.today)
    mode: str = "TEST"
    provider: str = "csv"

    # --- what came in ---
    new_leads: int = 0
    duplicates_merged: int = 0
    total_tracked: int = 0

    # --- what moved ---
    changes: List[DealChange] = field(default_factory=list)

    # --- pipeline snapshot ---
    hot: List[Any] = field(default_factory=list)
    strong: List[Any] = field(default_factory=list)
    follow_ups_due: List[Any] = field(default_factory=list)
    follow_ups_overdue: List[Any] = field(default_factory=list)
    counters: List[Any] = field(default_factory=list)
    offers_open: List[Any] = field(default_factory=list)
    contracts_live: List[Any] = field(default_factory=list)
    buyer_search: List[Any] = field(default_factory=list)
    assigned: int = 0
    closed: int = 0

    # --- the plan ---
    priorities: List[PriorityItem] = field(default_factory=list)

    # --- accounting ---
    usage: UsageReport = field(default_factory=UsageReport)
    notifications: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    exports: List[Path] = field(default_factory=list)

    @property
    def improvements(self) -> List[DealChange]:
        return [c for c in self.changes if c.is_improvement]

    def summary_counts(self) -> Dict[str, int]:
        return {
            "NEW LEADS": self.new_leads,
            "HOT": len(self.hot),
            "STRONG": len(self.strong),
            "FOLLOW UPS": len(self.follow_ups_due) + len(self.follow_ups_overdue),
            "OFFERS": len(self.offers_open),
            "NEGOTIATIONS": len(self.counters),
            "UNDER CONTRACT": len(self.contracts_live),
            "BUYER SEARCH": len(self.buyer_search),
            "CLOSED": self.closed,
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_date": self.run_date.isoformat(),
            "mode": self.mode,
            "provider": self.provider,
            "counts": self.summary_counts(),
            "total_tracked": self.total_tracked,
            "duplicates_merged": self.duplicates_merged,
            "improvements": len(self.improvements),
            "usage": self.usage.as_dict(),
            "priorities": [item.as_dict() for item in self.priorities],
            "changes": [
                {
                    "property_id": c.property_id,
                    "address": c.address,
                    "improvement": c.is_improvement,
                    "movements": [m.description for m in c.movements],
                }
                for c in self.changes
            ],
        }


def run_daily(
    store: LeadStore,
    provider: Optional[PropertyDataProvider] = None,
    criteria: Optional[HuntCriteria] = None,
    engine_config: EngineConfig = DEFAULT_CONFIG,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    budget: Optional[ApiBudget] = None,
    runtime: Optional[RuntimeConfig] = None,
    notifications: Optional[NotificationCenter] = None,
    today: Optional[date] = None,
    ingest: bool = True,
) -> DailyReport:
    """Run the whole day. Returns a :class:`DailyReport`."""
    today = today or date.today()
    budget = budget or ApiBudget.from_env()
    runtime = runtime or RuntimeConfig()
    notifications = notifications or NotificationCenter.build("console")
    workflow = AcquisitionWorkflow(store, config=engine_config)

    report = DailyReport(
        run_date=today,
        mode=str(runtime.mode),
        provider=provider.name if provider else "none",
    )

    # --- 1-7. ingest, dedupe, score, research, analyze, rank ------------
    hunt: Optional[HuntResult] = None
    if ingest and provider is not None:
        hunt = run_hunt(
            provider,
            criteria or HuntCriteria(states=lead_config.target_states),
            engine_config=engine_config,
            lead_config=lead_config,
            budget=HuntBudget(
                research_limit=budget.max_research,
                comps_limit=budget.max_comps,
                research_min_lead_score=budget.research_min_lead_score,
                comps_min_lead_score=budget.comps_min_lead_score,
            ),
            store=store,
            as_of=today,
        )
        metrics = hunt.metrics
        report.new_leads = sum(
            1 for change in hunt.changes.values() if change.is_new
        )
        report.duplicates_merged = metrics.duplicates_merged
        report.usage.raw_leads = metrics.properties_searched
        report.usage.filtered_out = metrics.properties_filtered
        report.usage.research_calls = metrics.detail_calls + metrics.distress_calls
        report.usage.comp_calls = metrics.comp_calls
        report.usage.errors = metrics.errors
        for message in metrics.error_messages:
            report.usage.record_error(message)
        report.warnings.extend(hunt.warnings)

    # --- 3. what changed on leads we already had -------------------------
    report.changes = monitor(store)

    # --- 8-12. the pipeline right now ------------------------------------
    rows = store.search()
    report.total_tracked = len(rows)
    for row in rows:
        status = normalize_status(row.status)
        if status == "ASSIGNED":
            report.assigned += 1
        if status == "CLOSED":
            report.closed += 1
        if status == "BUYER_SEARCH":
            report.buyer_search.append(row)
        if is_closed(status):
            continue
        if (row.deal_score or 0) >= HOT_DEAL_SCORE:
            report.hot.append(row)
        elif (row.deal_score or 0) >= STRONG_DEAL_SCORE:
            report.strong.append(row)

    buckets = workflow.follow_ups_by_bucket(today)
    report.follow_ups_overdue = buckets["OVERDUE"]
    report.follow_ups_due = buckets["TODAY"]
    report.counters = workflow.open_counters()
    report.offers_open = workflow.store.all_offers(open_only=True)
    report.contracts_live = workflow.store.all_contracts(live_only=True)

    # --- the ranked plan --------------------------------------------------
    report.priorities = DailyPriorityEngine(workflow).build(today)

    # --- notifications ----------------------------------------------------
    _notify(report, notifications, today)
    report.notifications = list(notifications.collected)
    report.warnings.extend(notifications.failures)
    return report


def _notify(
    report: DailyReport, center: NotificationCenter, today: date
) -> None:
    """Raise a notification for anything that would be expensive to miss."""
    for row in report.hot[:10]:
        center.push(
            EventType.NEW_HOT_LEAD,
            f"Hot lead — deal {row.deal_score:.0f}",
            f"potential fee {money(row.potential_fee)}, offer {money(row.recommended_offer)}",
            property_id=row.dedupe_key, address=row.address,
        )
    for change in report.improvements[:10]:
        movements = "; ".join(m.description for m in change.improvements)
        event = (
            EventType.PRICE_REDUCTION
            if any(m.field == "asking_price" for m in change.improvements)
            else EventType.DEAL_SCORE_UP
        )
        center.push(
            event, "DEAL IMPROVEMENT DETECTED", movements,
            property_id=change.property_id, address=change.address,
        )
    for row, offer in report.counters[:10]:
        center.push(
            EventType.SELLER_COUNTER,
            f"Seller countered at {money(offer.seller_counter)}",
            f"MAO {money(offer.mao)}, fee there {money(offer.fee_at_current_price)}",
            property_id=row.dedupe_key, address=row.address,
        )
    for contract in report.contracts_live:
        days = contract.closing_days_left(today)
        inspection = contract.inspection_days_left(today)
        soonest = min([d for d in (days, inspection) if d is not None], default=None)
        if soonest is not None and soonest <= 7:
            center.push(
                EventType.CONTRACT_DEADLINE,
                f"Contract deadline in {soonest} day(s)",
                f"{contract.status} at {money(contract.purchase_price)}",
                property_id=contract.property_id,
            )


def render_daily_report(report: DailyReport) -> str:
    """The DAILY ACQUISITIONS REPORT."""
    width = 148
    lines = [
        "=" * width,
        f"DAILY ACQUISITIONS REPORT — {report.run_date.isoformat()}",
        f"MODE: {report.mode}   SOURCE: {report.provider}",
        "=" * width,
        "",
    ]
    for label, count in report.summary_counts().items():
        lines.append(f"  {label + ':':<20}{count:>6}")
    lines.append(f"  {'TOTAL TRACKED:':<20}{report.total_tracked:>6}")
    if report.duplicates_merged:
        lines.append(f"  {'DUPLICATES MERGED:':<20}{report.duplicates_merged:>6}")

    if report.improvements:
        lines.append("")
        lines.append(f"DEAL IMPROVEMENTS ({len(report.improvements)})")
        lines.append("-" * width)
        for change in report.improvements[:10]:
            lines.append("  " + change.render().replace("\n", "\n  "))

    if report.notifications:
        lines.append("")
        lines.append(f"NOTIFICATIONS ({len(report.notifications)})")
        lines.append("-" * width)
        for notification in report.notifications[:15]:
            lines.append("  " + notification.render().replace("\n", "\n  "))

    lines.append("")
    lines.append(report.usage.render())

    for warning in report.warnings:
        lines.append(f"  WARNING: {warning}")

    lines.append("")
    lines.append(
        "Projected figures only. Nothing here has been sent, and no deal is "
        "guaranteed."
    )
    lines.append("=" * width)
    return "\n".join(lines)
