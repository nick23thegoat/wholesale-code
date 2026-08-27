"""Provider call accounting.

Every provider call goes through a counter so a run can report exactly what it
spent. Paid property-data APIs bill per request; a funnel that silently asks
for comps on a thousand raw leads is a bill, not a bug report, so the counts
are surfaced whether or not anything went wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ProviderMetrics:
    """Counts for one hunt. Cheap local work is counted separately from calls."""

    provider_name: str = ""

    # --- funnel volumes (free) -------------------------------------------
    properties_searched: int = 0
    properties_returned: int = 0
    properties_filtered: int = 0
    duplicates_merged: int = 0

    # --- billable calls ---------------------------------------------------
    search_calls: int = 0
    detail_calls: int = 0
    owner_calls: int = 0
    distress_calls: int = 0
    comp_calls: int = 0
    skip_trace_calls: int = 0

    # --- outcomes ---------------------------------------------------------
    errors: int = 0
    error_messages: List[str] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)

    #: Per-stage record of how many leads survived, in funnel order.
    stages: List[tuple] = field(default_factory=list)

    def record_stage(self, name: str, count: int) -> None:
        self.stages.append((name, count))

    def record_error(self, message: str) -> None:
        self.errors += 1
        if message not in self.error_messages:
            self.error_messages.append(message)

    def record_unsupported(self, capability: str) -> None:
        if capability not in self.unsupported:
            self.unsupported.append(capability)

    @property
    def estimated_api_calls(self) -> int:
        """Total billable requests issued this run."""
        return (
            self.search_calls
            + self.detail_calls
            + self.owner_calls
            + self.distress_calls
            + self.comp_calls
            + self.skip_trace_calls
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "provider": self.provider_name,
            "properties_searched": self.properties_searched,
            "properties_returned": self.properties_returned,
            "properties_filtered": self.properties_filtered,
            "duplicates_merged": self.duplicates_merged,
            "search_calls": self.search_calls,
            "property_detail_calls": self.detail_calls,
            "owner_calls": self.owner_calls,
            "distress_calls": self.distress_calls,
            "comp_calls": self.comp_calls,
            "skip_trace_calls": self.skip_trace_calls,
            "api_errors": self.errors,
            "estimated_api_calls": self.estimated_api_calls,
        }

    def render(self) -> str:
        """Human-readable cost report."""
        lines = [
            "PROVIDER CALLS",
            f"  Provider:              {self.provider_name or 'unknown'}",
            f"  Properties searched:   {self.properties_searched}",
            f"  Properties returned:   {self.properties_returned}",
            f"  Properties filtered:   {self.properties_filtered}",
            f"  Duplicates merged:     {self.duplicates_merged}",
            f"  Search calls:          {self.search_calls}",
            f"  Property-detail calls: {self.detail_calls}",
            f"  Owner calls:           {self.owner_calls}",
            f"  Distress calls:        {self.distress_calls}",
            f"  Comp calls:            {self.comp_calls}",
            f"  Skip-trace calls:      {self.skip_trace_calls}",
            f"  API errors:            {self.errors}",
            f"  Estimated API calls:   {self.estimated_api_calls}",
        ]
        if self.stages:
            lines.append("  FUNNEL")
            for name, count in self.stages:
                lines.append(f"    {name:<28} {count}")
        if self.unsupported:
            lines.append("  Unsupported by this provider: " + ", ".join(self.unsupported))
        for message in self.error_messages:
            lines.append(f"  ERROR: {message}")
        return "\n".join(lines)
