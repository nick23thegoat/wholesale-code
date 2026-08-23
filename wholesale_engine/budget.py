"""How much a run is allowed to spend, and on what.

Paid property and skip-trace APIs bill per request, so the pipeline is ordered
cheapest-first and every stage is capped::

    RAW LEADS  ->  CHEAP FILTER  ->  LEAD SCORE
               ->  PROPERTY RESEARCH  ->  COMPS  ->  SKIP TRACE
    (free)         (free)               (billable, capped at each step)

Two rules this module exists to enforce, both asserted by tests:

* **nothing expensive runs on a rejected lead** — a lead that failed the cheap
  filters or the score gate never reaches research, comps or skip tracing
* **skip tracing is never automatic for everything** — it is the dearest call
  and needs a lead to clear a quality bar first

Defaults are deliberately conservative. A run that wants to research a
thousand properties should have to say so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Environment variables that override the defaults.
BUDGET_ENV_VARS = (
    "MAX_RAW_LEADS",
    "MAX_RESEARCH",
    "MAX_COMPS",
    "MAX_SKIP_TRACES",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


@dataclass
class ApiBudget:
    """Hard caps and quality gates for one run."""

    # --- volume caps ----------------------------------------------------
    #: Raw leads pulled from the source before any filtering.
    max_raw_leads: int = 1_000
    #: Properties sent for detail / owner / distress enrichment.
    max_research: int = 100
    #: Properties sent for comps. The dearest property call, so the tightest cap.
    max_comps: int = 30
    #: Owners sent for skip tracing. The dearest call in the whole pipeline.
    max_skip_traces: int = 10

    # --- quality gates: what a lead must clear to earn a billable call ---
    research_min_lead_score: float = 50.0
    comps_min_lead_score: float = 60.0

    #: Skip tracing needs ANY of these, plus a live pipeline status.
    skip_trace_min_lead_score: float = 70.0
    skip_trace_min_deal_score: float = 70.0
    skip_trace_min_priority_score: float = 75.0

    #: Bulk skip tracing asks before spending unless this is on.
    auto_skip_trace: bool = False

    #: Per-lookup cost estimates, for the "this will cost about" line. Zero
    #: until you fill in your provider's real pricing — the engine will not
    #: invent a vendor's rate card.
    cost_per_research: float = 0.0
    cost_per_comp: float = 0.0
    cost_per_skip_trace: float = 0.0

    @classmethod
    def from_env(cls, **overrides: object) -> "ApiBudget":
        """Defaults, then environment, then explicit overrides."""
        budget = cls(
            max_raw_leads=_env_int("MAX_RAW_LEADS", cls.max_raw_leads),
            max_research=_env_int("MAX_RESEARCH", cls.max_research),
            max_comps=_env_int("MAX_COMPS", cls.max_comps),
            max_skip_traces=_env_int("MAX_SKIP_TRACES", cls.max_skip_traces),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(budget, key):
                setattr(budget, key, value)
        return budget

    # ------------------------------------------------------------------

    def qualifies_for_skip_trace(
        self,
        lead_score: Optional[float] = None,
        deal_score: Optional[float] = None,
        priority_score: Optional[float] = None,
        status: str = "",
        already_reachable: bool = False,
    ) -> bool:
        """Is this lead worth paying to trace?

        ANY of the three score bars, AND a status still worth working. A lead
        that already has a contact route is not re-traced.
        """
        from .pipeline_status import is_closed

        if already_reachable:
            return False
        if status and is_closed(status):
            return False
        return any(
            value is not None and value >= bar
            for value, bar in (
                (lead_score, self.skip_trace_min_lead_score),
                (deal_score, self.skip_trace_min_deal_score),
                (priority_score, self.skip_trace_min_priority_score),
            )
        )

    def describe_gates(self) -> str:
        return (
            f"lead score >= {self.skip_trace_min_lead_score:g} OR "
            f"deal score >= {self.skip_trace_min_deal_score:g} OR "
            f"priority score >= {self.skip_trace_min_priority_score:g}, "
            "and the property is not PASSED, DEAD or CLOSED"
        )

    def estimate(self, kind: str, count: int) -> float:
        rate = {
            "research": self.cost_per_research,
            "comps": self.cost_per_comp,
            "skip_trace": self.cost_per_skip_trace,
        }.get(kind, 0.0)
        return rate * count

    def render(self) -> str:
        return "\n".join([
            "API BUDGET",
            f"  MAX_RAW_LEADS       {self.max_raw_leads}",
            f"  MAX_RESEARCH        {self.max_research}"
            f"   (lead score >= {self.research_min_lead_score:g})",
            f"  MAX_COMPS           {self.max_comps}"
            f"   (lead score >= {self.comps_min_lead_score:g})",
            f"  MAX_SKIP_TRACES     {self.max_skip_traces}"
            f"   ({self.describe_gates()})",
            f"  Bulk skip tracing   {'automatic' if self.auto_skip_trace else 'asks first'}",
        ])


@dataclass
class UsageReport:
    """What a run actually spent. Printed whether or not anything went wrong."""

    raw_leads: int = 0
    filtered_out: int = 0
    research_calls: int = 0
    comp_calls: int = 0
    skip_trace_calls: int = 0
    errors: int = 0
    error_messages: List[str] = field(default_factory=list)
    skipped_for_budget: Dict[str, int] = field(default_factory=dict)

    def record_error(self, message: str) -> None:
        self.errors += 1
        if message not in self.error_messages:
            self.error_messages.append(message)

    def record_cap(self, stage: str, count: int) -> None:
        if count > 0:
            self.skipped_for_budget[stage] = self.skipped_for_budget.get(stage, 0) + count

    @property
    def billable_calls(self) -> int:
        return self.research_calls + self.comp_calls + self.skip_trace_calls

    def as_dict(self) -> Dict[str, object]:
        return {
            "raw_leads": self.raw_leads,
            "filtered_out": self.filtered_out,
            "research_calls": self.research_calls,
            "comp_calls": self.comp_calls,
            "skip_trace_calls": self.skip_trace_calls,
            "errors": self.errors,
            "billable_calls": self.billable_calls,
        }

    def render(self, budget: Optional[ApiBudget] = None) -> str:
        lines = [
            "API USAGE",
            f"  RAW LEADS           {self.raw_leads}",
            f"  FILTERED OUT        {self.filtered_out}   (free — no calls made)",
            f"  RESEARCH CALLS      {self.research_calls}",
            f"  COMP CALLS          {self.comp_calls}",
            f"  SKIP TRACE CALLS    {self.skip_trace_calls}",
            f"  ERRORS              {self.errors}",
            f"  BILLABLE TOTAL      {self.billable_calls}",
        ]
        if budget is not None:
            estimated = (
                budget.estimate("research", self.research_calls)
                + budget.estimate("comps", self.comp_calls)
                + budget.estimate("skip_trace", self.skip_trace_calls)
            )
            if estimated:
                lines.append(f"  ESTIMATED COST      ${estimated:,.2f}")
            else:
                lines.append(
                    "  ESTIMATED COST      unknown — set the per-call rates in "
                    "ApiBudget from your provider's published pricing."
                )
        for stage, count in self.skipped_for_budget.items():
            lines.append(f"  capped: {count} lead(s) not sent for {stage}")
        for message in self.error_messages:
            lines.append(f"  ERROR: {message}")
        return "\n".join(lines)


DEFAULT_BUDGET = ApiBudget()
