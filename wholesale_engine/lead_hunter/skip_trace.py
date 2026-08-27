"""Skip-tracing seam. **Deliberately not implemented.**

The future flow is::

    Lead -> Property data -> Lead score -> Deal analysis
         -> SKIP TRACE -> phone/email -> CRM -> outreach

Wave 2 builds everything to the left of SKIP TRACE and stops there. This module
defines the contract a future provider must satisfy, and the gate that decides
*which* leads are even eligible to be traced — you pay per trace, so tracing
happens after a lead has proven itself, not before.

Non-negotiables for any future implementation:

* Contact data comes from a licensed provider under a written agreement. It is
  never scraped, guessed, or pattern-generated.
* This engine will not fabricate a phone number, an email address, a mailing
  address or an owner name. A trace that returns nothing returns nothing.
* Outreach is regulated (TCPA, state calling laws, federal and state DNC
  lists, CAN-SPAM). A provider integration must carry consent tracking, DNC
  scrubbing, and a suppression list before a single call or text goes out.
* Traces and their results must be logged for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..config import DEFAULT_LEAD_CONFIG, LeadHunterConfig
from ..data.sources import NotConfiguredError
from ..models.enums import Decision
from .models import STATUS_ANALYZED, LeadPipelineReport, LeadResult


@dataclass(frozen=True)
class SkipTraceRequest:
    """What a provider would be given. Property identity only — no guesses."""

    lead_id: str
    address: str
    city: str
    state: str
    zip_code: str
    owner_name: str = ""


@dataclass
class SkipTraceResult:
    """What a provider would return. Empty is a valid, honest answer."""

    lead_id: str
    phones: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    mailing_address: str = ""
    provider: str = ""
    traced_at: Optional[str] = None
    dnc_flagged: bool = False

    @property
    def has_contact(self) -> bool:
        return bool(self.phones or self.emails)


def build_request(result: LeadResult) -> SkipTraceRequest:
    """Assemble the identity payload for one lead. No contact fields invented."""
    lead = result.lead
    return SkipTraceRequest(
        lead_id=lead.lead_id,
        address=lead.address,
        city=lead.city,
        state=lead.state,
        zip_code=lead.zip_code,
        owner_name=lead.owner_name,
    )


def skip_trace_candidates(
    report: LeadPipelineReport,
    lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
) -> List[LeadResult]:
    """Leads that would be worth paying to trace once a provider exists.

    The gate: the lead survived filtering, was analyzed, and the deal did not
    come back as an outright PASS. Tracing a PASS is spending money to call
    someone about a property you would not buy.
    """
    eligible: List[LeadResult] = []
    for result in report.results:
        if result.status != STATUS_ANALYZED or result.analysis is None:
            continue
        if result.analysis.decision is Decision.PASS:
            continue
        eligible.append(result)
    return eligible


class UnconfiguredSkipTraceProvider:
    """Placeholder provider. Every call raises — by design."""

    name = "skip-trace"

    def trace(self, request: SkipTraceRequest) -> SkipTraceResult:
        raise NotConfiguredError(
            "Skip tracing is not implemented. This engine has no skip-tracing "
            "database and will not generate phone numbers or emails. Connecting a "
            "licensed provider is Wave 3 work."
        )

    def trace_batch(self, requests: List[SkipTraceRequest]) -> List[SkipTraceResult]:
        raise NotConfiguredError("Skip tracing is not implemented.")
