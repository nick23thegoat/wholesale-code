"""The Wave 4 funnel: raw leads in, a short list of real candidates out.

    REAL PROPERTY DATA -> LEAD HUNTER -> PROPERTY RESEARCH -> COMPS
      -> ARV -> REPAIRS -> MAO -> DEAL SCORE -> HOT LEADS

**Cost control is the design.** Paid property-data APIs bill per request, so
the funnel is ordered strictly cheapest-first and every stage narrows what the
next one sees::

    1,000 raw leads          1 search call
      -> cheap filters       free    (geography, price, type, signals)
    300 leads
      -> lead scoring        free    (Wave 2 rules, no calls)
    100 leads
      -> property research   billable, capped by research_limit
    30 candidates
      -> comps / valuation   billable, capped by comps_limit — the dearest call
    10 hot deals
      -> skip tracing        NOT CONNECTED. Interface only.

Two rules follow from that ordering and are enforced here, not left to
discipline: comps are never requested for a raw lead, and nothing is ever skip
traced. Both are also asserted by the tests.

Every number downstream of the funnel comes from the existing Wave 1 analyzer.
There is no second deal-analysis system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .analysis import analyze_property
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_LEAD_CONFIG,
    EngineConfig,
    LeadHunterConfig,
)
from .lead_hunter.filters import apply_filters
from .lead_hunter.models import (
    ARV_NEEDS_VERIFICATION,
    Lead,
    LeadPipelineReport,
    LeadResult,
    STATUS_ANALYZED,
    STATUS_BELOW_DEAL_SCORE,
    STATUS_FILTERED,
)
from .lead_hunter.normalizer import deduplicate
from .lead_hunter.pipeline import arv_status
from .lead_hunter.scoring import score_lead
from .providers import (
    Capability,
    HuntCriteria,
    ProviderMetrics,
    PropertyDataProvider,
)
from .storage import ChangeSet, LeadStore, StoredLead, dedupe_key, detect_changes

#: Stage labels, in funnel order. Reported by ProviderMetrics.stages.
STAGE_SEARCHED = "raw leads from source"
STAGE_DEDUPED = "after dedupe"
STAGE_FILTERED = "after cheap filters"
STAGE_LEAD_SCORED = "after lead score"
STAGE_RESEARCHED = "after property research"
STAGE_COMPED = "after comps"
STAGE_ANALYZED = "after deal score"
STAGE_HOT = "hot deals"


@dataclass
class HuntBudget:
    """Hard caps on billable calls for one run.

    Defaults are deliberately small. A run that wants to research a thousand
    properties should have to say so, in writing, on the command line.
    """

    #: Maximum properties sent for detail/distress enrichment.
    research_limit: int = 100
    #: Maximum properties sent for comps. The dearest call, so the tightest cap.
    comps_limit: int = 30
    #: Lead score a property must reach before it is worth paying to research.
    research_min_lead_score: float = 50.0
    #: Lead score a property must reach before it is worth paying for comps.
    comps_min_lead_score: float = 60.0
    #: Skip tracing is never performed. Present so the cap exists when it is.
    skip_trace_limit: int = 0


@dataclass
class HuntResult:
    """Everything one hunt produced."""

    report: LeadPipelineReport = field(default_factory=LeadPipelineReport)
    metrics: ProviderMetrics = field(default_factory=ProviderMetrics)
    changes: Dict[str, ChangeSet] = field(default_factory=dict)
    #: Lead-hunter results ordered best-first, change bumps applied.
    prioritized: List[LeadResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)
    provider_name: str = ""
    criteria: Optional[HuntCriteria] = None

    def change_for(self, lead: Lead) -> Optional[ChangeSet]:
        return self.changes.get(dedupe_key(lead))

    def priority_of(self, result: LeadResult) -> float:
        """Working priority: deal score, plus any change-driven bump."""
        base = result.deal_score if result.deal_score is not None else result.score.total
        change = self.change_for(result.lead)
        return base + (change.priority_bump if change else 0.0)


# ---------------------------------------------------------------------------
# Cheap, free filtering — runs before anything billable
# ---------------------------------------------------------------------------


def _signal_ok(lead: Lead, required: Sequence[str]) -> bool:
    """Any-of match. Unknown never rejects — it is a gap, not a disqualifier."""
    if not required:
        return True
    for name in required:
        if getattr(lead, name, None) is True:
            return True
    # Every required signal explicitly False is a real rejection; any unknown
    # leaves the door open.
    return any(getattr(lead, name, None) is None for name in required)


def cheap_filter(
    leads: Sequence[Lead], criteria: HuntCriteria
) -> Tuple[List[Lead], List[Tuple[Lead, str]]]:
    """Geography, price, type, signals and equity. Free, so it runs first."""
    kept: List[Lead] = []
    dropped: List[Tuple[Lead, str]] = []
    for lead in leads:
        if not criteria.matches_geography(lead.state, lead.county, lead.city, lead.zip_code):
            dropped.append((lead, "outside the requested geography"))
            continue
        if not criteria.matches_price(lead.asking_price):
            dropped.append((lead, "asking price outside the requested band"))
            continue
        if not criteria.matches_property_type(str(lead.property_type.value)):
            dropped.append((lead, f"property type {lead.property_type} not requested"))
            continue
        if not _signal_ok(lead, criteria.required_signals):
            dropped.append(
                (lead, "none of the required signals reported: "
                 + ", ".join(criteria.required_signals))
            )
            continue
        equity = lead.equity_estimate
        if criteria.min_equity is not None and equity is not None and equity < criteria.min_equity:
            dropped.append((lead, f"equity below ${criteria.min_equity:,.0f}"))
            continue
        kept.append(lead)
    return kept, dropped


# ---------------------------------------------------------------------------
# Billable enrichment — only ever called on survivors
# ---------------------------------------------------------------------------


def research_properties(
    provider: PropertyDataProvider,
    candidates: Sequence[Lead],
    budget: HuntBudget,
    metrics: ProviderMetrics,
) -> List[Lead]:
    """Detail + distress enrichment for the survivors, within budget.

    A provider that does not support a capability costs nothing and blocks
    nothing: the lead keeps whatever it already had, and the gap is reported.
    """
    selected = list(candidates)[: budget.research_limit]
    for lead in selected:
        if provider.supports(Capability.PROPERTY):
            response = provider.get_property(lead)
            metrics.detail_calls += 1
            if response.ok and response.data is not None:
                _merge_detail(lead, response.data)
            elif response.supported and response.reason:
                lead.needs_verification.append(response.reason)
        if provider.supports(Capability.DISTRESS):
            response = provider.get_distress_data(lead)
            metrics.distress_calls += 1
            if response.ok and isinstance(response.data, dict):
                _apply_distress(lead, response.data)
    return selected


def _merge_detail(lead: Lead, detail: Lead) -> None:
    """Fill blanks from a detail record. Never overwrite what we already had."""
    for name in (
        "county", "zip_code", "beds", "baths", "sqft", "year_built",
        "estimated_value", "estimated_repairs", "estimated_equity", "owner_name",
    ):
        current = getattr(lead, name, None)
        incoming = getattr(detail, name, None)
        if incoming not in (None, "") and current in (None, ""):
            setattr(lead, name, incoming)


def _apply_distress(lead: Lead, data: Dict[str, object]) -> None:
    """Apply public-record distress facts. Only ever True/False, never guessed."""
    for name in (
        "vacant", "tax_delinquent", "pre_foreclosure", "foreclosure",
        "probate", "inherited", "code_violation", "absentee_owner",
    ):
        value = data.get(name)
        if isinstance(value, bool) and getattr(lead, name, None) is None:
            setattr(lead, name, value)


def fetch_comps(
    provider: PropertyDataProvider,
    candidates: Sequence[Lead],
    budget: HuntBudget,
    metrics: ProviderMetrics,
) -> List[Lead]:
    """The most expensive call, on the shortest list.

    Comps are what turn a SOURCE-PROVIDED ARV into a verified one, so they are
    worth paying for — but only on properties that already survived everything
    cheaper.
    """
    if not provider.supports(Capability.COMPS):
        metrics.record_unsupported(str(Capability.COMPS))
        return list(candidates)
    selected = list(candidates)[: budget.comps_limit]
    for lead in selected:
        if lead.comps:
            continue  # already supplied — do not pay for it twice
        response = provider.get_comps(lead)
        if not provider.is_local:
            metrics.comp_calls += 1
        if response.ok and response.data:
            lead.comps = list(response.data)
    return list(candidates)


# ---------------------------------------------------------------------------
# The funnel
# ---------------------------------------------------------------------------


def run_hunt(
    provider: PropertyDataProvider,
    criteria: Optional[HuntCriteria] = None,
    engine_config: EngineConfig = DEFAULT_CONFIG,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    budget: Optional[HuntBudget] = None,
    store: Optional[LeadStore] = None,
    as_of: Optional[date] = None,
) -> HuntResult:
    """Run the full cost-controlled funnel against one provider."""
    criteria = criteria or HuntCriteria(states=lead_config.target_states)
    budget = budget or HuntBudget()
    metrics = provider.metrics
    metrics.provider_name = provider.name

    result = HuntResult(
        metrics=metrics, provider_name=provider.name, criteria=criteria
    )

    # --- 1. search (1 call) ---------------------------------------------
    response = provider.search_properties(criteria)
    if not response.supported:
        result.warnings.append(response.reason)
        return result
    raw = list(response.data or [])
    if response.reason:
        result.notices.append(response.reason)
    result.warnings.extend(getattr(provider, "warnings", []))
    metrics.record_stage(STAGE_SEARCHED, len(raw))

    report = LeadPipelineReport(source_name=provider.name, rows_read=len(raw))
    result.report = report

    # --- 2. normalize + dedupe (free) ------------------------------------
    unique, duplicates = deduplicate(raw)
    report.duplicates = duplicates
    metrics.duplicates_merged = len(duplicates)
    if duplicates:
        report.warnings.append(
            f"{len(duplicates)} duplicate row(s) merged into {len(unique)} unique properties"
        )
    metrics.record_stage(STAGE_DEDUPED, len(unique))

    # --- 3. cheap filters (free) -----------------------------------------
    survivors, dropped = cheap_filter(unique, criteria)
    metrics.record_stage(STAGE_FILTERED, len(survivors))

    results_by_key: Dict[int, LeadResult] = {}
    for lead, reason in dropped:
        score = score_lead(lead, lead_config)
        outcome = apply_filters(lead, score, lead_config)
        outcome.reject(reason)
        entry = LeadResult(lead=lead, score=score, filter_outcome=outcome)
        entry.status = STATUS_FILTERED
        entry.arv_status = arv_status(lead, None)
        results_by_key[id(lead)] = entry
        report.results.append(entry)

    # --- 4. lead scoring (free) ------------------------------------------
    scored: List[LeadResult] = []
    for lead in survivors:
        score = score_lead(lead, lead_config)
        outcome = apply_filters(lead, score, lead_config)
        if score.total < criteria.min_lead_score:
            outcome.reject(
                f"lead score {score.total:.0f} is below the minimum of "
                f"{criteria.min_lead_score:.0f}"
            )
        entry = LeadResult(lead=lead, score=score, filter_outcome=outcome)
        results_by_key[id(lead)] = entry
        report.results.append(entry)
        if outcome.passed:
            scored.append(entry)
        else:
            entry.status = STATUS_FILTERED
            entry.arv_status = arv_status(lead, None)
    scored.sort(key=lambda r: -r.score.total)
    metrics.record_stage(STAGE_LEAD_SCORED, len(scored))

    # --- 5. property research (BILLABLE) ---------------------------------
    research_pool = [
        entry for entry in scored
        if entry.score.total >= budget.research_min_lead_score
    ]
    researched = research_properties(
        provider, [e.lead for e in research_pool], budget, metrics
    )
    researched_ids = {id(lead) for lead in researched}
    metrics.record_stage(STAGE_RESEARCHED, len(researched))

    # --- 6. comps (MOST BILLABLE) ----------------------------------------
    comp_pool = [
        entry.lead
        for entry in research_pool
        if id(entry.lead) in researched_ids
        and entry.score.total >= budget.comps_min_lead_score
    ]
    fetch_comps(provider, comp_pool, budget, metrics)
    metrics.record_stage(STAGE_COMPED, min(len(comp_pool), budget.comps_limit))

    # --- 7. deal analysis (free — Wave 1, unchanged) ---------------------
    analyzed = 0
    for entry in scored:
        entry.analysis = analyze_property(
            entry.lead.to_property_lead(), engine_config, as_of
        )
        entry.arv_status = arv_status(entry.lead, entry.analysis)
        if entry.analysis.score.total < criteria.min_deal_score:
            entry.status = STATUS_BELOW_DEAL_SCORE
            entry.filter_outcome.reasons.append(
                f"deal score {entry.analysis.score.total:.0f} is below the minimum of "
                f"{criteria.min_deal_score:.0f}"
            )
        else:
            entry.status = STATUS_ANALYZED
            analyzed += 1
    metrics.record_stage(STAGE_ANALYZED, analyzed)

    # --- 8. change detection + persistence -------------------------------
    if store is not None:
        for entry in report.results:
            result.changes[dedupe_key(entry.lead)] = _record(store, entry, as_of)

    # --- 9. prioritize ----------------------------------------------------
    result.prioritized = sorted(
        report.results,
        key=lambda r: (
            0 if r.status == STATUS_ANALYZED else 1,
            -result.priority_of(r),
            -r.score.total,
        ),
    )
    metrics.record_stage(
        STAGE_HOT, sum(1 for r in report.results if r.is_hot_lead and r.status == STATUS_ANALYZED)
    )
    return result


def _record(store: LeadStore, entry: LeadResult, as_of: Optional[date]) -> ChangeSet:
    """Diff against the stored record, then persist this sighting."""
    lead = entry.lead
    previous: Optional[StoredLead] = store.get_for_lead(lead)
    changes = detect_changes(
        previous,
        address=lead.address,
        asking_price=lead.asking_price,
        estimated_value=lead.estimated_value,
        estimated_repairs=lead.estimated_repairs,
        signals={
            name: getattr(lead, name, None)
            for name in (
                "absentee_owner", "vacant", "high_equity", "pre_foreclosure",
                "foreclosure", "tax_delinquent", "probate", "inherited",
                "code_violation", "tired_landlord",
            )
        },
        lead_score=entry.score.total,
        deal_score=entry.deal_score,
    )
    store.upsert_lead(
        lead,
        lead_score=entry.score.total,
        deal_score=entry.deal_score,
        final_decision="" if entry.analysis is None else str(entry.analysis.decision),
        seen_at=as_of,
        change_summary=changes.summary(),
    )
    return changes
