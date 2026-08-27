"""The Wave 2 pipeline.

::

    LEAD SOURCE -> NORMALIZE -> DEDUPLICATE -> LEAD SCORE -> LEAD FILTER
        -> convert to PropertyLead -> WAVE 1 ANALYZER (ARV, repairs, MAO,
           deal score, decision) -> PRIORITIZE -> hot_leads.csv + lead_pipeline.csv

The deal math is **not** reimplemented here. Everything from ARV through MAO to
the final decision is the Wave 1 analyzer, called unchanged, so there is exactly
one MAO calculator in this codebase.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from ..analysis.analyzer import analyze_property
from ..config import DEFAULT_CONFIG, DEFAULT_LEAD_CONFIG, EngineConfig, LeadHunterConfig
from ..models.enums import ARVConfidence, Classification
from ..models.results import AnalysisResult
from .filters import apply_filters
from .models import (
    ARV_COMP_ESTIMATED,
    ARV_COMP_SUPPORTED,
    ARV_NEEDS_VERIFICATION,
    ARV_SOURCE_PROVIDED,
    STATUS_ANALYZED,
    STATUS_BELOW_DEAL_SCORE,
    STATUS_FILTERED,
    Lead,
    LeadPipelineReport,
    LeadResult,
)
from .normalizer import deduplicate
from .scoring import score_lead
from .sources.base import BaseLeadSource
from .sources.csv_source import CsvLeadSource, attach_comps


def arv_status(lead: Lead, analysis: Optional[AnalysisResult]) -> str:
    """Label where the ARV came from and whether it can be trusted yet.

    A value handed over by a lead source is SOURCE-PROVIDED until the Wave 1
    comp engine has something to say about it. It is never treated as fact
    just because it arrived in a spreadsheet column.
    """
    if analysis is None:
        if lead.estimated_value is not None:
            return ARV_SOURCE_PROVIDED
        return ARV_NEEDS_VERIFICATION
    confidence = analysis.arv.confidence
    if confidence is ARVConfidence.VERIFIED_SUPPORTED:
        return ARV_COMP_SUPPORTED
    if confidence is ARVConfidence.ESTIMATED:
        return ARV_COMP_ESTIMATED
    if confidence is ARVConfidence.USER_PROVIDED:
        return ARV_SOURCE_PROVIDED
    return ARV_NEEDS_VERIFICATION


def run_lead_pipeline(
    leads: Iterable[Lead],
    engine_config: EngineConfig = DEFAULT_CONFIG,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    analyze: bool = True,
    as_of: Optional[date] = None,
    source_name: str = "",
) -> LeadPipelineReport:
    """Run leads through normalization, dedupe, scoring, filtering and analysis."""
    incoming = list(leads)
    report = LeadPipelineReport(source_name=source_name, rows_read=len(incoming))

    unique, duplicates = deduplicate(incoming)
    report.duplicates = duplicates
    if duplicates:
        report.warnings.append(
            f"{len(duplicates)} duplicate row(s) merged into {len(unique)} unique properties"
        )

    for lead in unique:
        score = score_lead(lead, lead_config)
        outcome = apply_filters(lead, score, lead_config)

        result = LeadResult(lead=lead, score=score, filter_outcome=outcome)
        if not outcome.passed:
            result.status = STATUS_FILTERED
            result.arv_status = arv_status(lead, None)
            report.results.append(result)
            continue

        if analyze:
            # Wave 1 owns every number from here on.
            result.analysis = analyze_property(lead.to_property_lead(), engine_config, as_of)
            result.arv_status = arv_status(lead, result.analysis)
            if result.analysis.score.total < lead_config.min_deal_score:
                result.status = STATUS_BELOW_DEAL_SCORE
                result.filter_outcome.reasons.append(
                    f"deal score {result.analysis.score.total:.0f} is below the minimum of "
                    f"{lead_config.min_deal_score:.0f}"
                )
            else:
                result.status = STATUS_ANALYZED
        else:
            result.status = STATUS_ANALYZED
            result.arv_status = arv_status(lead, None)

        report.results.append(result)

    return report


def run_from_source(
    source: BaseLeadSource,
    engine_config: EngineConfig = DEFAULT_CONFIG,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    analyze: bool = True,
    as_of: Optional[date] = None,
    comps_path: Optional[Path] = None,
) -> LeadPipelineReport:
    """Pull leads from any :class:`BaseLeadSource` and run the pipeline.

    ``comps_path`` is optional. When supplied, the comps are joined onto the
    leads so the Wave 1 valuation engine can verify the source's ARV instead
    of taking it on faith.
    """
    leads = source.search_leads()
    if comps_path:
        matched = attach_comps(leads, Path(comps_path))
        if matched == 0:
            source_warnings = getattr(source, "warnings", None)
            if source_warnings is not None:
                source_warnings.append(
                    f"{Path(comps_path).name}: no comps matched any lead — check that "
                    "lead_id / property_id / address line up between the two files."
                )
    report = run_lead_pipeline(
        leads, engine_config, lead_config, analyze, as_of, source_name=source.name
    )
    report.warnings.extend(getattr(source, "warnings", []))
    return report


def run_from_csv(
    path: Path,
    engine_config: EngineConfig = DEFAULT_CONFIG,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
    analyze: bool = True,
    as_of: Optional[date] = None,
    comps_path: Optional[Path] = None,
) -> LeadPipelineReport:
    """Convenience wrapper: CSV path in, finished pipeline report out."""
    return run_from_source(
        CsvLeadSource(path), engine_config, lead_config, analyze, as_of, comps_path
    )


# ---------------------------------------------------------------------------
# Prioritization
# ---------------------------------------------------------------------------


def _sort_key(result: LeadResult) -> tuple:
    """Deal score, then lead score, then spread — best opportunities first."""
    deal = result.deal_score if result.deal_score is not None else -1.0
    spread = result.potential_spread if result.potential_spread is not None else -1e12
    return (-deal, -result.score.total, -spread)


def prioritize(results: Iterable[LeadResult]) -> List[LeadResult]:
    """Rank results highest-quality first."""
    return sorted(results, key=_sort_key)


def hot_leads(
    report: LeadPipelineReport,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
) -> List[LeadResult]:
    """The 🔥 HOT and 🟠 STRONG leads that survived filtering, ranked.

    Qualification is on the LEAD score — this is the call list. The deal score
    and final decision ride along in the output so a hot lead attached to a bad
    deal is obvious rather than hidden, and ranking puts the ones that also
    underwrite well at the top.
    """
    wanted = {
        Classification[name].value if name in Classification.__members__ else name
        for name in lead_config.hot_lead_classifications
    }
    qualifying = [
        result
        for result in report.results
        if result.status == STATUS_ANALYZED and result.score.classification.value in wanted
    ]
    return prioritize(qualifying)


def with_overrides(
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG, **overrides
) -> LeadHunterConfig:
    """Return a copy of the config with the given fields replaced."""
    clean = {key: value for key, value in overrides.items() if value is not None}
    return replace(lead_config, **clean) if clean else lead_config
