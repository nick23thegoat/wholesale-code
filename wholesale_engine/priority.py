"""PRIORITY SCORE — the third score, and the one that orders your day.

Three scores, three questions, deliberately never merged:

===============  ==================================================
LEAD SCORE       is this worth a phone call?      (Wave 2 signals)
DEAL SCORE       is this worth a contract?        (Wave 1 underwriting)
PRIORITY SCORE   what do I work on first?         (this module)
===============  ==================================================

Priority is a *ranking* metric. It reads the other two and never writes to
them: nothing here can change a lead score or a deal score, and a high
priority never turns a bad deal into a good one. What it adds is everything
the other two deliberately ignore — how confident the data is, how urgent the
seller's situation is, whether the price just moved, and how long it has sat.

A deal you cannot verify ranks below one you can, at the same deal score.
That is the point: priority is about where your next hour goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .models.enums import ARVConfidence, CompConfidence

# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------


class PriorityBand(Enum):
    """What to do with this, right now."""

    PRIORITY = "🔥 PRIORITY"
    HIGH = "🟠 HIGH"
    REVIEW = "🟡 REVIEW"
    LOW = "🔵 LOW"
    REJECT = "❌ REJECT"

    def __str__(self) -> str:
        return self.value


#: Lower bound (inclusive) of each band.
PRIORITY_BANDS: Dict[str, float] = {
    "PRIORITY": 80.0,
    "HIGH": 65.0,
    "REVIEW": 50.0,
    "LOW": 30.0,
}

#: Component weights, summing to 100.
PRIORITY_WEIGHTS: Dict[str, float] = {
    "deal_score": 26.0,       # is the deal real
    "lead_score": 16.0,       # is the seller reachable and motivated
    "wholesale_fee": 14.0,    # how much this pays
    "data_confidence": 12.0,  # can any of it be trusted
    "distress": 10.0,         # how urgent the seller's situation is
    "equity": 8.0,            # is there room to negotiate into
    "price_movement": 8.0,    # did something just change
    "days_on_market": 6.0,    # how tired the listing is
}

#: A fee at or above this fraction of the target scores full marks. The target
#: itself is NOT a floor here — a below-target fee scores proportionally, it is
#: never zeroed and never disqualifying.
FEE_FULL_CREDIT_RATIO = 1.6

#: Days on market at which staleness credit maxes out.
STALE_DOM_DAYS = 180


@dataclass(frozen=True)
class PriorityComponent:
    """One weighted contribution to the priority score."""

    name: str
    weight: float
    score: float  # 0.0 - 1.0
    note: str

    @property
    def points(self) -> float:
        return self.weight * self.score


@dataclass
class PriorityScore:
    """The priority verdict for one property."""

    total: float = 0.0
    band: PriorityBand = PriorityBand.LOW
    components: List[PriorityComponent] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    change_bump: float = 0.0
    rejected_because: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.band in (PriorityBand.PRIORITY, PriorityBand.HIGH)

    def component(self, name: str) -> Optional[PriorityComponent]:
        return next((c for c in self.components if c.name == name), None)

    def render(self) -> str:
        lines = [f"PRIORITY  {self.total:.1f} / 100   {self.band}"]
        if self.rejected_because:
            lines.append(f"  {self.rejected_because}")
        for component in self.components:
            lines.append(
                f"  {component.name:<22}{component.points:5.1f} / {component.weight:4.1f}   "
                f"{component.note}"
            )
        if self.change_bump:
            lines.append(f"  {'recent change':<22}+{self.change_bump:.1f} (movement bonus)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def classify_priority(score: float) -> PriorityBand:
    if score >= PRIORITY_BANDS["PRIORITY"]:
        return PriorityBand.PRIORITY
    if score >= PRIORITY_BANDS["HIGH"]:
        return PriorityBand.HIGH
    if score >= PRIORITY_BANDS["REVIEW"]:
        return PriorityBand.REVIEW
    if score >= PRIORITY_BANDS["LOW"]:
        return PriorityBand.LOW
    return PriorityBand.REJECT


class PriorityEngine:
    """Ranks leads for your attention. Reads the other scores, writes neither."""

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        target_wholesale_fee: float = 18_000.0,
    ) -> None:
        self.weights = dict(weights or PRIORITY_WEIGHTS)
        self.target_wholesale_fee = target_wholesale_fee

    def weight(self, name: str) -> float:
        return self.weights.get(name, 0.0)

    # ------------------------------------------------------------------

    def score(
        self,
        lead_score: Optional[float] = None,
        deal_score: Optional[float] = None,
        wholesale_fee: Optional[float] = None,
        data_confidence: Optional[float] = None,
        arv_confidence: Optional[ARVConfidence] = None,
        comp_confidence: Optional[CompConfidence] = None,
        distress_count: int = 0,
        urgent_distress_count: int = 0,
        equity_percentage: Optional[float] = None,
        equity_is_calculated: bool = False,
        price_drop_percentage: Optional[float] = None,
        days_on_market: Optional[int] = None,
        change_bump: float = 0.0,
        decision: Optional[str] = None,
    ) -> PriorityScore:
        """Combine every input into a 0-100 priority.

        Every argument is optional. A missing input scores its component at
        the neutral middle rather than zero — an unresearched lead should sink
        below a good one, not below a bad one.
        """
        components = [
            self._deal(deal_score),
            self._lead(lead_score),
            self._fee(wholesale_fee),
            self._confidence(data_confidence, arv_confidence, comp_confidence),
            self._distress(distress_count, urgent_distress_count),
            self._equity(equity_percentage, equity_is_calculated),
            self._price_movement(price_drop_percentage),
            self._days_on_market(days_on_market),
        ]
        total_weight = sum(c.weight for c in components)
        raw = sum(c.points for c in components)
        base = (raw / total_weight * 100.0) if total_weight else 0.0
        total = _clamp(base + change_bump, 0.0, 100.0)

        result = PriorityScore(
            total=round(total, 1),
            components=components,
            change_bump=round(change_bump, 1),
        )

        # A PASS is a PASS. Priority ranks what is worth working, and an
        # analyzer that says pass has already answered that.
        if decision and "PASS" in decision:
            result.total = min(result.total, PRIORITY_BANDS["LOW"] - 0.1)
            result.rejected_because = (
                "The analyzer returned PASS, so this is capped below LOW no matter "
                "how the other inputs look."
            )

        result.band = classify_priority(result.total)
        result.reasons = [c.note for c in sorted(components, key=lambda c: -c.points)[:3]]
        return result

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------

    def _deal(self, deal_score: Optional[float]) -> PriorityComponent:
        weight = self.weight("deal_score")
        if deal_score is None:
            return PriorityComponent(
                "deal score", weight, 0.35,
                "Not analyzed yet — scored neutral-low until it is.",
            )
        return PriorityComponent(
            "deal score", weight, _clamp(deal_score / 100.0),
            f"Deal score {deal_score:.0f}/100.",
        )

    def _lead(self, lead_score: Optional[float]) -> PriorityComponent:
        weight = self.weight("lead_score")
        if lead_score is None:
            return PriorityComponent("lead score", weight, 0.35, "No lead score available.")
        return PriorityComponent(
            "lead score", weight, _clamp(lead_score / 100.0),
            f"Lead score {lead_score:.0f}/100.",
        )

    def _fee(self, fee: Optional[float]) -> PriorityComponent:
        """Fee credit is proportional and never disqualifying.

        The target is a target: a $13,000 fee against an $18,000 target scores
        well over half marks and keeps competing on everything else.
        """
        weight = self.weight("wholesale_fee")
        if fee is None:
            return PriorityComponent(
                "wholesale fee", weight, 0.35, "No fee computed — needs an ARV and repairs."
            )
        if fee <= 0:
            return PriorityComponent(
                "wholesale fee", weight, 0.0,
                f"No fee available at the price on the table ({fee:,.0f}).",
            )
        ceiling = max(self.target_wholesale_fee * FEE_FULL_CREDIT_RATIO, 1.0)
        share = _clamp(fee / ceiling)
        against = fee / self.target_wholesale_fee if self.target_wholesale_fee else 0
        return PriorityComponent(
            "wholesale fee", weight, share,
            f"${fee:,.0f} of fee — {against * 100:.0f}% of the ${self.target_wholesale_fee:,.0f} target.",
        )

    def _confidence(
        self,
        data_confidence: Optional[float],
        arv_confidence: Optional[ARVConfidence],
        comp_confidence: Optional[CompConfidence],
    ) -> PriorityComponent:
        """Can any of this be trusted? Three readings, averaged.

        This is what stops an unverifiable 90-scoring lead outranking a
        verified 80-scoring one.
        """
        weight = self.weight("data_confidence")
        parts: List[float] = []
        notes: List[str] = []

        if data_confidence is not None:
            parts.append(_clamp(data_confidence))
            notes.append(f"data {data_confidence * 100:.0f}%")

        arv_scores = {
            ARVConfidence.VERIFIED_SUPPORTED: 1.0,
            ARVConfidence.ESTIMATED: 0.6,
            ARVConfidence.USER_PROVIDED: 0.3,
            ARVConfidence.INSUFFICIENT_DATA: 0.0,
        }
        if arv_confidence is not None:
            parts.append(arv_scores.get(arv_confidence, 0.0))
            notes.append(f"ARV {arv_confidence}")

        comp_scores = {
            CompConfidence.HIGH: 1.0,
            CompConfidence.MEDIUM: 0.7,
            CompConfidence.LOW: 0.35,
            CompConfidence.NONE: 0.0,
        }
        if comp_confidence is not None:
            parts.append(comp_scores.get(comp_confidence, 0.0))
            notes.append(f"comps {comp_confidence}")

        if not parts:
            return PriorityComponent(
                "data confidence", weight, 0.2,
                "Nothing verified — no ARV basis, no comps, no research pass.",
            )
        return PriorityComponent(
            "data confidence", weight, sum(parts) / len(parts), "; ".join(notes) + "."
        )

    def _distress(self, count: int, urgent: int) -> PriorityComponent:
        """Urgency. Three signals is a motivated seller; one is a maybe."""
        weight = self.weight("distress")
        if count <= 0:
            return PriorityComponent(
                "distress", weight, 0.0, "No distress signals confirmed."
            )
        breadth = _clamp(count / 4.0)
        urgency = _clamp(urgent / 2.0)
        score = _clamp(breadth * 0.55 + urgency * 0.45)
        note = f"{count} signal(s) confirmed"
        if urgent:
            note += f", {urgent} of them time-sensitive (foreclosure/tax/probate)"
        return PriorityComponent("distress", weight, score, note + ".")

    def _equity(
        self, equity_percentage: Optional[float], is_calculated: bool
    ) -> PriorityComponent:
        """Room to negotiate into. A derived spread counts for less."""
        weight = self.weight("equity")
        if equity_percentage is None:
            return PriorityComponent(
                "equity", weight, 0.3,
                "Equity unknown — no mortgage balance available.",
            )
        share = _clamp(equity_percentage / 0.60)
        if not is_calculated:
            # Value-minus-asking is a spread, not equity. Half credit.
            share *= 0.5
            return PriorityComponent(
                "equity", weight, share,
                f"{equity_percentage * 100:.0f}% spread, but the mortgage is unknown "
                "so this is not verified equity.",
            )
        return PriorityComponent(
            "equity", weight, share,
            f"{equity_percentage * 100:.0f}% calculated equity.",
        )

    def _price_movement(self, drop_percentage: Optional[float]) -> PriorityComponent:
        """A price cut is the clearest motivation signal there is."""
        weight = self.weight("price_movement")
        if not drop_percentage:
            return PriorityComponent(
                "price movement", weight, 0.25, "No price change since the last sighting."
            )
        if drop_percentage < 0:
            return PriorityComponent(
                "price movement", weight, 0.0,
                f"Price went UP {abs(drop_percentage) * 100:.0f}% — moving away from you.",
            )
        return PriorityComponent(
            "price movement", weight, _clamp(drop_percentage / 0.20),
            f"Price dropped {drop_percentage * 100:.1f}% — the seller is moving.",
        )

    def _days_on_market(self, days: Optional[int]) -> PriorityComponent:
        """A stale listing has a seller who has stopped believing the price."""
        weight = self.weight("days_on_market")
        if days is None:
            return PriorityComponent(
                "days on market", weight, 0.3, "Days on market unknown."
            )
        return PriorityComponent(
            "days on market", weight, _clamp(days / STALE_DOM_DAYS),
            f"{days} days on market.",
        )


#: Shared default instance.
DEFAULT_PRIORITY_ENGINE = PriorityEngine()
