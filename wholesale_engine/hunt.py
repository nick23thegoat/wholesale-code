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
from .budget import ApiBudget, UsageReport
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
from .priority import DEFAULT_PRIORITY_ENGINE, PriorityEngine, PriorityScore
from .providers import (
    Capability,
    HuntCriteria,
    ProviderMetrics,
    PropertyDataProvider,
)
from .research import PropertyResearch, PropertyResearchService
from .storage import (
    ChangeSet,
    LeadSnapshot,
    LeadStore,
    StoredLead,
    dedupe_key,
    detect_changes,
)

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
    comps_limit: int = 25
    #: Ceiling on total billable calls to the property-data vendor for this
    #: run, sitting over research_limit and comps_limit. The adapter enforces
    #: it too, so a provider cannot be talked past this by a longer candidate
    #: list.
    reach_limit: int = 100
    #: Lead score a property must reach before it is worth paying to research.
    research_min_lead_score: float = 50.0
    #: Lead score a property must reach before it is worth paying for comps.
    comps_min_lead_score: float = 60.0
    #: Skip tracing is never performed in the hunt. Present so the cap exists,
    #: and so the run can report it alongside the caps it did apply.
    skip_trace_limit: int = 0

    @classmethod
    def from_api_budget(cls, budget: "ApiBudget") -> "HuntBudget":
        """The env-configured :class:`ApiBudget`, in the funnel's own shape."""
        return cls(
            research_limit=budget.max_research,
            comps_limit=budget.max_comps,
            reach_limit=budget.max_reach,
            research_min_lead_score=budget.research_min_lead_score,
            comps_min_lead_score=budget.comps_min_lead_score,
            skip_trace_limit=budget.max_skip_traces,
        )


@dataclass
class HuntResult:
    """Everything one hunt produced."""

    report: LeadPipelineReport = field(default_factory=LeadPipelineReport)
    metrics: ProviderMetrics = field(default_factory=ProviderMetrics)
    changes: Dict[str, ChangeSet] = field(default_factory=dict)
    #: Lead-hunter results ordered best-first, change bumps applied.
    prioritized: List[LeadResult] = field(default_factory=list)
    #: Normalized research, keyed by dedupe key. Empty for leads that did not
    #: reach the research stage — which is the point of the cost controls.
    research: Dict[str, PropertyResearch] = field(default_factory=dict)
    #: Priority scores, keyed by dedupe key.
    priorities: Dict[str, PriorityScore] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)
    provider_name: str = ""
    criteria: Optional[HuntCriteria] = None
    #: What this run spent, and whether a cap ended it early.
    usage: UsageReport = field(default_factory=UsageReport)

    def change_for(self, lead: Lead) -> Optional[ChangeSet]:
        return self.changes.get(dedupe_key(lead))

    def research_for(self, lead: Lead) -> Optional[PropertyResearch]:
        return self.research.get(dedupe_key(lead))

    def priority_for(self, lead: Lead) -> Optional[PriorityScore]:
        return self.priorities.get(dedupe_key(lead))

    def priority_of(self, result: LeadResult) -> float:
        """The PRIORITY SCORE for this lead, 0-100.

        Falls back to the deal score plus any change bump when the priority
        engine has not run — an unresearched lead still needs an ordering.
        """
        priority = self.priority_for(result.lead)
        if priority is not None:
            return priority.total
        base = result.deal_score if result.deal_score is not None else result.score.total
        change = self.change_for(result.lead)
        return base + (change.priority_bump if change else 0.0)

    def band_of(self, result: LeadResult) -> str:
        priority = self.priority_for(result.lead)
        return str(priority.band) if priority else ""


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
    priority_engine: Optional[PriorityEngine] = None,
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

    # --- 5. property / owner / distress / equity research (BILLABLE) -----
    research_pool = [
        entry for entry in scored
        if entry.score.total >= budget.research_min_lead_score
    ]
    result.usage.record_cap(
        "research", max(len(research_pool) - budget.research_limit, 0)
    )
    researched = research_properties(
        provider, [e.lead for e in research_pool], budget, metrics
    )
    researched_ids = {id(lead) for lead in researched}
    metrics.record_stage(STAGE_RESEARCHED, len(researched))

    # The normalized research record, built for exactly the leads that earned
    # the enrichment. Everything else stays unresearched, deliberately.
    research_service = PropertyResearchService(provider, metrics)
    for lead in researched:
        research = research_service.research(lead, as_of)
        result.research[dedupe_key(lead)] = research
        _apply_research(lead, research)

    # --- 6. comps (MOST BILLABLE) ----------------------------------------
    comp_pool = [
        entry.lead
        for entry in research_pool
        if id(entry.lead) in researched_ids
        and entry.score.total >= budget.comps_min_lead_score
    ]
    result.usage.record_cap("comps", max(len(comp_pool) - budget.comps_limit, 0))
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
    # Changes are detected before priority, because a price drop feeds into it.
    if store is not None:
        for entry in report.results:
            result.changes[dedupe_key(entry.lead)] = _diff(store, entry)

    # --- 9. PRIORITY SCORE ------------------------------------------------
    engine = priority_engine or PriorityEngine(
        target_wholesale_fee=engine_config.target_wholesale_fee
    )
    for entry in report.results:
        key = dedupe_key(entry.lead)
        result.priorities[key] = _score_priority(
            engine, entry, result.research.get(key), result.changes.get(key)
        )

    # --- 10. persist the finished picture ---------------------------------
    if store is not None:
        for entry in report.results:
            key = dedupe_key(entry.lead)
            _record(
                store, entry, as_of,
                result.changes.get(key),
                result.research.get(key),
                result.priorities.get(key),
            )

    # --- 11. order --------------------------------------------------------
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

    # --- 12. what the run spent -------------------------------------------
    result.usage.raw_leads = len(raw)
    result.usage.filtered_out = len(report.results) - len(scored)
    result.usage.research_calls = metrics.detail_calls + metrics.distress_calls
    result.usage.comp_calls = metrics.comp_calls
    result.usage.errors = metrics.errors
    result.usage.error_messages = list(metrics.error_messages)
    result.usage.provider_name = provider.name
    result.usage.adopt_provider_usage(provider)
    return result


def _apply_research(lead: Lead, research: PropertyResearch) -> None:
    """Feed research findings back onto the lead, filling blanks only.

    The analyzer reads the :class:`Lead`, so anything research established has
    to land there for it to matter. Existing values are never overwritten —
    research adds facts, it does not relitigate the ones already on file.
    """
    for attr, fact in (
        ("beds", research.beds),
        ("baths", research.baths),
        ("sqft", research.sqft),
        ("year_built", research.year_built),
        ("estimated_value", research.estimated_value),
        ("estimated_repairs", research.estimated_repairs),
        ("days_on_market", research.days_on_market),
    ):
        if getattr(lead, attr, None) is None and fact.is_known:
            setattr(lead, attr, fact.value)

    if lead.estimated_equity is None and research.equity.is_verified_enough_to_lean_on:
        lead.estimated_equity = research.equity.equity_amount

    if not lead.owner_name and research.owner.owner_name.is_known:
        lead.owner_name = str(research.owner.owner_name.value)

    # Distress signals research confirmed that the lead did not carry.
    for name, value in research.distress.as_bools().items():
        if value is not None and getattr(lead, name, "missing") is None:
            setattr(lead, name, value)

    for caveat in research.equity.caveats:
        if caveat not in lead.needs_verification:
            lead.needs_verification.append(caveat)


def _score_priority(
    engine: PriorityEngine,
    entry: LeadResult,
    research: Optional[PropertyResearch],
    change: Optional[ChangeSet],
) -> PriorityScore:
    """Assemble the priority inputs from wherever they live."""
    analysis = entry.analysis
    financials = analysis.financials if analysis else None

    data_confidence = None
    if analysis is not None:
        for component in analysis.score.components:
            if component.name == "Data confidence":
                data_confidence = component.score
                break

    distress_count = research.distress.count if research else len(entry.lead.confirmed_signals())
    urgent_count = research.distress.urgent_count if research else 0

    equity_pct = research.equity.equity_percentage if research else entry.lead.equity_ratio
    equity_calculated = (
        research.equity.is_verified_enough_to_lean_on if research else False
    )

    price_drop_pct = None
    if change is not None:
        for detected in change.of_kind("PRICE DROP"):
            before, after = detected.before, detected.after
            if before:
                price_drop_pct = (before - after) / before
        for detected in change.of_kind("PRICE INCREASE"):
            before, after = detected.before, detected.after
            if before:
                price_drop_pct = -(after - before) / before

    return engine.score(
        lead_score=entry.score.total,
        deal_score=entry.deal_score,
        wholesale_fee=financials.binding_wholesale_fee if financials else None,
        data_confidence=data_confidence,
        arv_confidence=analysis.arv.confidence if analysis else None,
        comp_confidence=analysis.comps.confidence if analysis else None,
        distress_count=distress_count,
        urgent_distress_count=urgent_count,
        equity_percentage=equity_pct,
        equity_is_calculated=equity_calculated,
        price_drop_percentage=price_drop_pct,
        days_on_market=entry.lead.days_on_market,
        decision=str(analysis.decision) if analysis else None,
    )


def _diff(store: LeadStore, entry: LeadResult) -> ChangeSet:
    """Compare this sighting against the stored record, without writing."""
    lead = entry.lead
    previous: Optional[StoredLead] = store.get_for_lead(lead)
    return detect_changes(
        previous,
        address=lead.address,
        asking_price=lead.asking_price,
        estimated_value=lead.estimated_value,
        estimated_repairs=lead.estimated_repairs,
        arv=entry.analysis.arv.arv if entry.analysis else None,
        days_on_market=lead.days_on_market,
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


def _record(
    store: LeadStore,
    entry: LeadResult,
    as_of: Optional[date],
    changes: Optional[ChangeSet] = None,
    research: Optional[PropertyResearch] = None,
    priority: Optional[PriorityScore] = None,
) -> None:
    """Persist this sighting, with its analysis, research and priority."""
    analysis = entry.analysis
    financials = analysis.financials if analysis else None
    equity = research.equity if research else None

    snapshot = LeadSnapshot(
        priority_score=priority.total if priority else None,
        priority_band=str(priority.band) if priority else "",
        arv=analysis.arv.arv if analysis else None,
        repair_estimate=analysis.repairs.base if analysis else None,
        mao=financials.mao if financials else None,
        recommended_offer=financials.recommended_offer if financials else None,
        potential_fee=financials.binding_wholesale_fee if financials else None,
        fee_status=str(financials.wholesale_fee_status) if financials else "",
        arv_confidence=str(analysis.arv.confidence) if analysis else "",
        comp_confidence=str(analysis.comps.confidence) if analysis else "",
        equity_amount=equity.equity_amount if equity else None,
        equity_percentage=equity.equity_percentage if equity else None,
        equity_status=str(equity.equity_status) if equity else "",
        distress_count=research.distress.count if research else 0,
        days_on_market=entry.lead.days_on_market,
        researched=research is not None,
        research_note=(
            f"{research.known_field_count} field(s) known, "
            f"{research.source_confidence} confidence, "
            f"{research.distress.count} distress signal(s)"
            if research
            else ""
        ),
    )
    store.upsert_lead(
        entry.lead,
        lead_score=entry.score.total,
        deal_score=entry.deal_score,
        final_decision="" if analysis is None else str(analysis.decision),
        seen_at=as_of,
        change_summary=changes.summary() if changes else "",
        snapshot=snapshot,
    )
