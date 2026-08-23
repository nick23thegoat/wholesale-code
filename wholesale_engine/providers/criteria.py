"""Search criteria for a hunt.

One immutable object describes everything a provider is being asked for:
geography, property shape, price band, and which distress signals matter. It
is passed to the provider (which narrows server-side where it can) and then
re-applied locally, because a provider that ignores a filter must never widen
the funnel by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from ..config import DEFAULT_PROPERTY_TYPES, DEFAULT_TARGET_STATES, LEAD_SIGNALS
from ..data.sources import SearchCriteria as LegacySearchCriteria


def _upper(values) -> Tuple[str, ...]:
    return tuple(v.strip().upper() for v in values if v and v.strip())


def _lower(values) -> Tuple[str, ...]:
    return tuple(v.strip().lower() for v in values if v and v.strip())


@dataclass(frozen=True)
class HuntCriteria:
    """What to look for. Every field is optional; empty means "no constraint"."""

    # --- geography --------------------------------------------------------
    states: Tuple[str, ...] = DEFAULT_TARGET_STATES
    counties: Tuple[str, ...] = ()
    cities: Tuple[str, ...] = ()
    zip_codes: Tuple[str, ...] = ()

    # --- property shape ---------------------------------------------------
    property_types: Tuple[str, ...] = DEFAULT_PROPERTY_TYPES

    # --- money ------------------------------------------------------------
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_equity: Optional[float] = None

    # --- distress / opportunity signals -----------------------------------
    #: Signals that must be present. A lead whose signal is UNKNOWN is never
    #: rejected here — unknown is a gap to fill, not a disqualification.
    required_signals: Tuple[str, ...] = ()

    # --- score gates (applied by the funnel, not the provider) ------------
    min_lead_score: float = 0.0
    min_deal_score: float = 0.0

    #: Cap on how many raw leads to pull. Cost control starts at the source.
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", _upper(self.states))
        object.__setattr__(self, "counties", _lower(self.counties))
        object.__setattr__(self, "cities", _lower(self.cities))
        object.__setattr__(self, "zip_codes", tuple(z.strip() for z in self.zip_codes if z.strip()))
        object.__setattr__(self, "property_types", _lower(self.property_types))
        signals = _lower(self.required_signals)
        unknown = [s for s in signals if s not in LEAD_SIGNALS]
        if unknown:
            raise ValueError(
                f"unknown signal(s): {', '.join(unknown)}. "
                f"Known signals: {', '.join(LEAD_SIGNALS)}"
            )
        object.__setattr__(self, "required_signals", signals)

    # ------------------------------------------------------------------
    # Matching — used to filter whatever a provider returns
    # ------------------------------------------------------------------

    def matches_geography(self, state: str, county: str, city: str, zip_code: str) -> bool:
        """True when the location clears every geography constraint set."""
        if self.states and (state or "").strip().upper() not in self.states:
            return False
        if self.counties and (county or "").strip().lower() not in self.counties:
            return False
        if self.cities and (city or "").strip().lower() not in self.cities:
            return False
        if self.zip_codes and (zip_code or "").strip() not in self.zip_codes:
            return False
        return True

    def matches_price(self, price: Optional[float]) -> bool:
        """Unknown price never rejects — it becomes a gap to fill."""
        if price is None:
            return True
        if self.min_price is not None and price < self.min_price:
            return False
        if self.max_price is not None and price > self.max_price:
            return False
        return True

    def matches_property_type(self, property_type: str) -> bool:
        if not self.property_types:
            return True
        value = (property_type or "").strip().lower()
        if not value or value == "unknown":
            return True
        return value in self.property_types

    # ------------------------------------------------------------------
    # Interop
    # ------------------------------------------------------------------

    def to_legacy(self) -> LegacySearchCriteria:
        """The Wave 1/2 :class:`SearchCriteria` shape, for existing sources."""
        return LegacySearchCriteria(
            city=self.cities[0] if self.cities else None,
            state=self.states[0] if len(self.states) == 1 else None,
            states=self.states,
            county=self.counties[0] if self.counties else None,
            zip_codes=self.zip_codes,
            max_price=self.max_price,
            min_price=self.min_price,
            property_types=self.property_types,
        )

    def describe(self) -> str:
        parts = []
        if self.states:
            parts.append("/".join(self.states))
        if self.counties:
            parts.append(f"counties: {', '.join(self.counties)}")
        if self.cities:
            parts.append(f"cities: {', '.join(self.cities)}")
        if self.zip_codes:
            parts.append(f"zips: {', '.join(self.zip_codes)}")
        if self.min_price is not None or self.max_price is not None:
            low = f"${self.min_price:,.0f}" if self.min_price is not None else "any"
            high = f"${self.max_price:,.0f}" if self.max_price is not None else "any"
            parts.append(f"price {low}-{high}")
        if self.min_equity is not None:
            parts.append(f"equity >= ${self.min_equity:,.0f}")
        if self.required_signals:
            parts.append("signals: " + ", ".join(self.required_signals))
        if self.min_lead_score:
            parts.append(f"lead score >= {self.min_lead_score:g}")
        if self.min_deal_score:
            parts.append(f"deal score >= {self.min_deal_score:g}")
        return "; ".join(parts) or "no constraints"
