"""Wave 2 — the lead hunter.

Raw lead lists in, prioritized opportunities out::

    source -> normalize -> deduplicate -> lead score -> filter
           -> Wave 1 analyzer -> deal score -> prioritize -> CSVs

The LEAD score (this package) and the DEAL score
(:mod:`wholesale_engine.analysis.scoring`) are separate on purpose: the first
says whether a seller is worth calling, the second says whether the property is
worth buying. A HOT lead can still be a terrible deal.
"""

from .filters import apply_filters, collect_gaps
from .models import (
    Lead,
    LeadPipelineReport,
    LeadResult,
    LeadScore,
    FilterOutcome,
    SignalHit,
)
from .normalizer import (
    deduplicate,
    merge_leads,
    normalize_address,
    normalize_city,
    normalize_lead,
    normalize_state,
    normalize_zip,
)
from .pipeline import (
    arv_status,
    hot_leads,
    prioritize,
    run_from_csv,
    run_from_source,
    run_lead_pipeline,
    with_overrides,
)
from .scoring import classify_lead, score_lead
from .skip_trace import skip_trace_candidates
from .sources import ApiLeadSourceTemplate, BaseLeadSource, CsvLeadSource

__all__ = [
    "ApiLeadSourceTemplate",
    "BaseLeadSource",
    "CsvLeadSource",
    "FilterOutcome",
    "Lead",
    "LeadPipelineReport",
    "LeadResult",
    "LeadScore",
    "SignalHit",
    "apply_filters",
    "arv_status",
    "classify_lead",
    "collect_gaps",
    "deduplicate",
    "hot_leads",
    "merge_leads",
    "normalize_address",
    "normalize_city",
    "normalize_lead",
    "normalize_state",
    "normalize_zip",
    "prioritize",
    "run_from_csv",
    "run_from_source",
    "run_lead_pipeline",
    "score_lead",
    "skip_trace_candidates",
    "with_overrides",
]
