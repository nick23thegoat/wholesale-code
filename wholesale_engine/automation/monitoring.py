"""Watch the leads you already have, and notice when one turns into a deal.

Most of a wholesaling pipeline is properties that were not quite right. The
interesting event is the one that *changes*: the price comes down, a
foreclosure gets filed, the ARV is corroborated — and something that scored 58
last week scores 79 today.

This module compares each lead against its stored history and reports what
moved. It never re-scores anything: the scores come from the engines that own
them. What it adds is the delta and the judgement about whether the delta
matters enough to interrupt you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..formatting import money
from ..storage import LeadStore, StoredLead

#: A deal-score jump of at least this much is worth a look.
DEAL_SCORE_JUMP = 10.0
#: A priority jump of at least this much is worth a look.
PRIORITY_JUMP = 15.0
#: A deal reaching this score after being below it is an improvement event.
STRONG_DEAL_SCORE = 70.0
#: Price move below this fraction is noise.
PRICE_MOVE_THRESHOLD = 0.02


@dataclass
class Movement:
    """One thing that changed about a property since the last run."""

    field: str
    before: Any
    after: Any
    description: str
    is_improvement: bool = False
    weight: float = 0.0


@dataclass
class DealChange:
    """Everything that moved on one property, and whether it now matters."""

    property_id: str
    address: str = ""
    movements: List[Movement] = field(default_factory=list)
    deal_score_before: Optional[float] = None
    deal_score_after: Optional[float] = None
    priority_before: Optional[float] = None
    priority_after: Optional[float] = None

    @property
    def has_movement(self) -> bool:
        return bool(self.movements)

    @property
    def improvements(self) -> List[Movement]:
        return [m for m in self.movements if m.is_improvement]

    @property
    def is_improvement(self) -> bool:
        """Did this go from 'not worth it' to 'worth a call'?"""
        return bool(self.improvements)

    @property
    def score(self) -> float:
        """How loudly this deserves to be surfaced."""
        return sum(m.weight for m in self.movements)

    def render(self) -> str:
        lines = [f"{self.address or self.property_id}:"]
        if self.is_improvement:
            lines.append("  DEAL IMPROVEMENT DETECTED")
        for movement in self.movements:
            lines.append(f"    {movement.description}")
        return "\n".join(lines)


def _delta(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None:
        return None
    return after - before


def compare_history(
    row: StoredLead, history: Sequence[Dict[str, Any]]
) -> DealChange:
    """Diff a lead's current state against its previous recorded sighting.

    ``history`` is the ``lead_history`` rows, oldest first. The comparison is
    against the *previous* entry, not the first, so a slow drift over months
    shows up one step at a time rather than as one dramatic jump.
    """
    change = DealChange(property_id=row.dedupe_key, address=row.address)
    if len(history) < 2:
        return change

    previous, current = history[-2], history[-1]
    change.deal_score_before = previous.get("deal_score")
    change.deal_score_after = current.get("deal_score")
    change.priority_before = row.priority_score if len(history) < 2 else previous.get("deal_score")
    change.priority_after = row.priority_score

    # --- price ---------------------------------------------------------
    before_price, after_price = previous.get("asking_price"), current.get("asking_price")
    if before_price and after_price and before_price != after_price:
        fraction = abs(after_price - before_price) / before_price
        if fraction >= PRICE_MOVE_THRESHOLD:
            dropped = after_price < before_price
            change.movements.append(
                Movement(
                    field="asking_price", before=before_price, after=after_price,
                    description=(
                        f"{'PRICE REDUCTION' if dropped else 'PRICE INCREASE'}: "
                        f"{money(before_price)} -> {money(after_price)} "
                        f"({'-' if dropped else '+'}{money(abs(after_price - before_price))}, "
                        f"{fraction * 100:.1f}%)"
                    ),
                    is_improvement=dropped and fraction >= 0.05,
                    weight=25.0 * fraction / 0.20 if dropped else 0.0,
                )
            )

    # --- valuation inputs ------------------------------------------------
    for key, label in (
        ("estimated_value", "ARV / estimated value"),
        ("estimated_repairs", "Repair estimate"),
    ):
        before, after = previous.get(key), current.get(key)
        if before and after and before != after:
            improved = (after > before) if key == "estimated_value" else (after < before)
            change.movements.append(
                Movement(
                    field=key, before=before, after=after,
                    description=f"{label}: {money(before)} -> {money(after)}",
                    is_improvement=improved,
                    weight=8.0 if improved else 0.0,
                )
            )

    # --- distress signals ------------------------------------------------
    import json

    before_signals = json.loads(previous.get("signals_json") or "{}")
    after_signals = json.loads(current.get("signals_json") or "{}")
    for name, value in after_signals.items():
        if value is True and before_signals.get(name) is not True:
            urgent = name in ("foreclosure", "pre_foreclosure", "tax_delinquent")
            change.movements.append(
                Movement(
                    field=name, before=before_signals.get(name), after=True,
                    description=f"NEW {name.replace('_', ' ').upper()}",
                    is_improvement=True,
                    weight=20.0 if urgent else 8.0,
                )
            )

    # --- scores ----------------------------------------------------------
    deal_delta = _delta(change.deal_score_before, change.deal_score_after)
    if deal_delta is not None and abs(deal_delta) >= 0.05:
        crossed = (
            change.deal_score_before is not None
            and change.deal_score_before < STRONG_DEAL_SCORE <= (change.deal_score_after or 0)
        )
        change.movements.append(
            Movement(
                field="deal_score",
                before=change.deal_score_before, after=change.deal_score_after,
                description=(
                    f"Deal Score: {change.deal_score_before:.0f} -> "
                    f"{change.deal_score_after:.0f}"
                ),
                is_improvement=deal_delta >= DEAL_SCORE_JUMP or crossed,
                weight=max(deal_delta, 0.0),
            )
        )
    return change


def monitor(
    store: LeadStore, limit: Optional[int] = None
) -> List[DealChange]:
    """Every property whose picture moved since its previous sighting.

    Ordered loudest-first: a foreclosure filing on a property that just came
    down 15% outranks a one-point score drift.
    """
    changes: List[DealChange] = []
    for row in store.search():
        history = store.history(row.lead_row_id)
        change = compare_history(row, history)
        if change.has_movement:
            changes.append(change)
    changes.sort(key=lambda c: (-int(c.is_improvement), -c.score))
    return changes[:limit] if limit else changes


def improvements(store: LeadStore, limit: Optional[int] = None) -> List[DealChange]:
    """Only the ones that got materially better."""
    return [c for c in monitor(store, limit) if c.is_improvement]


def render_monitor(changes: Sequence[DealChange]) -> str:
    if not changes:
        return "DEAL MONITORING\n  Nothing changed since the last run."
    lines = [f"DEAL MONITORING ({len(changes)} propert{'y' if len(changes) == 1 else 'ies'} moved)"]
    for change in changes:
        lines.append("  " + change.render().replace("\n", "\n  "))
    improved = [c for c in changes if c.is_improvement]
    if improved:
        lines.append("")
        lines.append(
            f"  {len(improved)} DEAL IMPROVEMENT(S) DETECTED — these were not worth "
            "pursuing before and may be now."
        )
    return "\n".join(lines)
