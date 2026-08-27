"""What changed since the last time we saw this property.

A property that reappears is not news. A property that reappears $20,000
cheaper, or newly vacant, or newly in pre-foreclosure, is the whole point of
running the hunt on a schedule.

Each detected change carries a priority bump. The bumps are additive and
capped, and they raise the lead's *working priority* — never its lead score or
deal score, which stay exactly what the scoring rules say they are. A change
tells you where to look first; it does not re-underwrite the deal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..formatting import money
from .database import TRACKED_SIGNALS, StoredLead

#: Human labels for the tracked signals.
SIGNAL_LABELS: Dict[str, str] = {
    "absentee_owner": "absentee owner",
    "vacant": "vacancy",
    "high_equity": "high equity",
    "pre_foreclosure": "pre-foreclosure",
    "foreclosure": "foreclosure",
    "tax_delinquent": "tax delinquency",
    "probate": "probate",
    "inherited": "inherited",
    "code_violation": "code violation",
    "tired_landlord": "tired landlord",
}

#: Priority added when a signal turns on. Distress that starts a clock —
#: foreclosure, tax delinquency — outranks a static attribute.
SIGNAL_PRIORITY: Dict[str, float] = {
    "foreclosure": 25.0,
    "pre_foreclosure": 25.0,
    "tax_delinquent": 15.0,
    "probate": 12.0,
    "inherited": 12.0,
    "vacant": 15.0,
    "code_violation": 10.0,
    "absentee_owner": 6.0,
    "high_equity": 8.0,
    "tired_landlord": 6.0,
}

#: A price move smaller than this is noise, not a signal.
PRICE_DROP_THRESHOLD = 0.02
#: Priority for the largest price drops.
MAX_PRICE_DROP_PRIORITY = 25.0
#: Ceiling on the total bump, so one lead cannot dominate on changes alone.
MAX_PRIORITY_BUMP = 60.0

# Change kinds
PRICE_DROP = "PRICE DROP"
PRICE_INCREASE = "PRICE INCREASE"
NEW_SIGNAL = "NEW SIGNAL"
SIGNAL_CLEARED = "SIGNAL CLEARED"
VALUE_CHANGE = "NEW ESTIMATED VALUE"
REPAIR_CHANGE = "NEW REPAIR ESTIMATE"
ARV_CHANGE = "NEW ARV"
DOM_CHANGE = "DAYS ON MARKET"
NEW_LISTING = "NEW LISTING"
LEAD_SCORE_CHANGE = "LEAD SCORE"
DEAL_SCORE_CHANGE = "DEAL SCORE"

#: Days on market that count as a meaningful jump since the last sighting.
DOM_JUMP_DAYS = 30
#: Priority for a listing that has gone materially staler.
DOM_PRIORITY = 6.0


@dataclass(frozen=True)
class Change:
    """One difference between the stored record and the current sighting."""

    kind: str
    field: str
    before: Optional[object]
    after: Optional[object]
    description: str
    priority: float = 0.0

    def __str__(self) -> str:
        return self.description


@dataclass
class ChangeSet:
    """Every change found for one property this run."""

    dedupe_key: str = ""
    address: str = ""
    is_new: bool = False
    changes: List[Change] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def priority_bump(self) -> float:
        """Total working-priority increase, capped."""
        return min(sum(c.priority for c in self.changes), MAX_PRIORITY_BUMP)

    def of_kind(self, kind: str) -> List[Change]:
        return [c for c in self.changes if c.kind == kind]

    @property
    def price_drop(self) -> Optional[Change]:
        drops = self.of_kind(PRICE_DROP)
        return drops[0] if drops else None

    @property
    def price_drop_amount(self) -> Optional[float]:
        """Dollars off since the last sighting. None when the price held."""
        drop = self.price_drop
        if drop is None or drop.before is None or drop.after is None:
            return None
        return float(drop.before) - float(drop.after)

    @property
    def price_drop_percentage(self) -> Optional[float]:
        """Fraction off since the last sighting, e.g. 0.168 for -16.8%."""
        drop = self.price_drop
        if drop is None or not drop.before:
            return None
        return (float(drop.before) - float(drop.after)) / float(drop.before)

    @property
    def is_price_drop(self) -> bool:
        return self.price_drop is not None

    @property
    def new_signals(self) -> List[str]:
        return [c.field for c in self.of_kind(NEW_SIGNAL)]

    def summary(self) -> str:
        """One-line summary for a CSV cell or a log."""
        if self.is_new:
            return "NEW"
        if not self.changes:
            return ""
        return " | ".join(c.description for c in self.changes)

    def render(self) -> str:
        """Multi-line summary for the console."""
        if self.is_new:
            return f"{self.address}: NEW"
        if not self.changes:
            return ""
        lines = [f"{self.address}:"]
        for change in self.changes:
            lines.append(f"  {change.description}")
        if self.priority_bump:
            lines.append(f"  PRIORITY +{self.priority_bump:.0f}")
        return "\n".join(lines)


def _price_drop_priority(before: float, after: float) -> float:
    """Scale priority with the size of the drop: 20%+ off earns the maximum."""
    if before <= 0:
        return 0.0
    fraction = (before - after) / before
    return round(min(fraction / 0.20, 1.0) * MAX_PRICE_DROP_PRIORITY, 1)


def _money_change(
    kind: str,
    field_name: str,
    label: str,
    before: Optional[float],
    after: Optional[float],
    priority: float = 0.0,
) -> Optional[Change]:
    if before is None or after is None or before == after:
        return None
    return Change(
        kind=kind,
        field=field_name,
        before=before,
        after=after,
        description=f"{label}: {money(before)} -> {money(after)}",
        priority=priority,
    )


def detect_changes(
    stored: Optional[StoredLead],
    *,
    address: str = "",
    asking_price: Optional[float] = None,
    estimated_value: Optional[float] = None,
    estimated_repairs: Optional[float] = None,
    arv: Optional[float] = None,
    days_on_market: Optional[int] = None,
    signals: Optional[Dict[str, Optional[bool]]] = None,
    lead_score: Optional[float] = None,
    deal_score: Optional[float] = None,
) -> ChangeSet:
    """Compare a fresh sighting against the stored record.

    ``stored is None`` means this is the first sighting: the result is marked
    new and carries no changes. Unknown-to-known is a change; known-to-unknown
    is not — a source that stopped reporting a field has not told you the fact
    went away.
    """
    result = ChangeSet(
        dedupe_key=stored.dedupe_key if stored else "",
        address=address or (stored.address if stored else ""),
        is_new=stored is None,
    )
    if stored is None:
        if asking_price is not None:
            result.changes.append(
                Change(
                    kind=NEW_LISTING,
                    field="asking_price",
                    before=None,
                    after=asking_price,
                    description=f"NEW LISTING at {money(asking_price)}",
                )
            )
        return result

    # --- price ---------------------------------------------------------
    if stored.asking_price is not None and asking_price is not None:
        if asking_price < stored.asking_price:
            drop = stored.asking_price - asking_price
            if drop / stored.asking_price >= PRICE_DROP_THRESHOLD:
                result.changes.append(
                    Change(
                        kind=PRICE_DROP,
                        field="asking_price",
                        before=stored.asking_price,
                        after=asking_price,
                        description=(
                            f"PRICE DROP: {money(stored.asking_price)} -> "
                            f"{money(asking_price)} (-{money(drop)}, "
                            f"{drop / stored.asking_price * 100:.0f}%)"
                        ),
                        priority=_price_drop_priority(stored.asking_price, asking_price),
                    )
                )
        elif asking_price > stored.asking_price:
            rise = asking_price - stored.asking_price
            if rise / stored.asking_price >= PRICE_DROP_THRESHOLD:
                result.changes.append(
                    Change(
                        kind=PRICE_INCREASE,
                        field="asking_price",
                        before=stored.asking_price,
                        after=asking_price,
                        description=(
                            f"PRICE INCREASE: {money(stored.asking_price)} -> "
                            f"{money(asking_price)} (+{money(rise)})"
                        ),
                    )
                )

    # --- signals -------------------------------------------------------
    current = signals or {}
    for name in TRACKED_SIGNALS:
        was = stored.signals.get(name)
        now = current.get(name)
        label = SIGNAL_LABELS.get(name, name.replace("_", " "))
        if now is True and was is not True:
            result.changes.append(
                Change(
                    kind=NEW_SIGNAL,
                    field=name,
                    before=was,
                    after=True,
                    description=f"NEW {label.upper()}"
                    + ("" if was is False else " (was unknown)"),
                    priority=SIGNAL_PRIORITY.get(name, 5.0),
                )
            )
        elif now is False and was is True:
            result.changes.append(
                Change(
                    kind=SIGNAL_CLEARED,
                    field=name,
                    before=True,
                    after=False,
                    description=f"{label} no longer reported",
                )
            )

    # --- valuation inputs ----------------------------------------------
    for change in (
        _money_change(
            VALUE_CHANGE, "estimated_value", "Estimated value",
            stored.estimated_value, estimated_value,
        ),
        _money_change(
            REPAIR_CHANGE, "estimated_repairs", "Repair estimate",
            stored.estimated_repairs, estimated_repairs,
        ),
        _money_change(ARV_CHANGE, "arv", "ARV", stored.arv, arv),
    ):
        if change is not None:
            result.changes.append(change)

    # --- days on market --------------------------------------------------
    # A listing that has sat another month is a seller who has had another
    # month of nobody calling.
    if (
        stored.days_on_market is not None
        and days_on_market is not None
        and days_on_market - stored.days_on_market >= DOM_JUMP_DAYS
    ):
        result.changes.append(
            Change(
                kind=DOM_CHANGE,
                field="days_on_market",
                before=stored.days_on_market,
                after=days_on_market,
                description=(
                    f"DAYS ON MARKET: {stored.days_on_market} -> {days_on_market} "
                    f"(+{days_on_market - stored.days_on_market} days)"
                ),
                priority=DOM_PRIORITY,
            )
        )

    # --- scores ---------------------------------------------------------
    for kind, field_name, label, before, after in (
        (LEAD_SCORE_CHANGE, "lead_score", "LEAD SCORE", stored.lead_score, lead_score),
        (DEAL_SCORE_CHANGE, "deal_score", "DEAL SCORE", stored.deal_score, deal_score),
    ):
        if before is None or after is None or abs(after - before) < 0.05:
            continue
        result.changes.append(
            Change(
                kind=kind,
                field=field_name,
                before=before,
                after=after,
                description=f"{label}: {before:.0f} -> {after:.0f}",
                priority=5.0 if after > before else 0.0,
            )
        )

    return result
