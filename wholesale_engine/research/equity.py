"""Equity: how much of the property the seller actually owns.

    Estimated Equity = Estimated Value - Mortgage Balance - Known Liens

Equity is the single most misused number in wholesaling, because the mortgage
balance is the hard part and it is the part lead lists leave out. This module
refuses to paper over that:

* full inputs (value, mortgage, liens) -> ``CALCULATED``
* a source reported an equity figure -> ``REPORTED`` (their claim, not ours)
* value and asking price only -> ``DERIVED`` — a **spread**, not equity
* no mortgage information at all -> ``UNKNOWN``

The derived case is the trap. Value minus asking price is what the seller is
leaving on the table, which equals equity only if the property is free and
clear. It is useful, it is not equity, and it is never labelled as though a
mortgage had been checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from ..formatting import money
from .facts import Confidence, Fact, SOURCE_DERIVED


class EquityStatus(Enum):
    """How the equity figure was arrived at."""

    #: Value minus a known mortgage balance minus known liens.
    CALCULATED = "CALCULATED"
    #: A source handed us an equity number. Their claim, unverified.
    REPORTED = "REPORTED"
    #: Value minus asking price. A spread, not equity. Mortgage unknown.
    DERIVED = "DERIVED (mortgage unknown)"
    #: Not enough information.
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


@dataclass
class EquityAssessment:
    """The equity picture, with its own honesty about how solid it is."""

    equity_amount: Optional[float] = None
    equity_percentage: Optional[float] = None  # of estimated value, 0.0-1.0
    equity_status: EquityStatus = EquityStatus.UNKNOWN
    equity_confidence: Confidence = Confidence.UNKNOWN

    estimated_value: Optional[float] = None
    mortgage_balance: Optional[float] = None
    liens_total: Optional[float] = None
    asking_price: Optional[float] = None

    basis: str = "no equity information available"
    caveats: List[str] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.equity_amount is not None

    @property
    def is_verified_enough_to_lean_on(self) -> bool:
        """True only when a real mortgage balance went into the number."""
        return self.equity_status is EquityStatus.CALCULATED

    @property
    def is_high_equity(self) -> bool:
        """35%+ of value, by the lead-hunter's threshold."""
        return self.equity_percentage is not None and self.equity_percentage >= 0.35

    def as_fact(self) -> Fact[float]:
        if self.equity_amount is None:
            return Fact.unknown(self.basis)
        if self.equity_status is EquityStatus.DERIVED:
            return Fact.derived(self.equity_amount, self.basis, self.equity_confidence)
        return Fact(
            value=self.equity_amount,
            source=SOURCE_DERIVED if self.equity_status is EquityStatus.CALCULATED else "lead_list",
            confidence=self.equity_confidence,
            note=self.basis,
        )

    def describe(self) -> str:
        if self.equity_amount is None:
            return f"UNKNOWN — {self.basis}"
        pct = f" ({self.equity_percentage * 100:.0f}% of value)" if self.equity_percentage is not None else ""
        return f"{money(self.equity_amount)}{pct} [{self.equity_status}]"


def assess_equity(
    estimated_value: Optional[float] = None,
    mortgage_balance: Optional[float] = None,
    liens: Optional[float] = None,
    reported_equity: Optional[float] = None,
    asking_price: Optional[float] = None,
    value_confidence: Confidence = Confidence.MEDIUM,
    mortgage_confidence: Confidence = Confidence.UNKNOWN,
) -> EquityAssessment:
    """Work out equity from whatever is actually known.

    The order is deliberate: a real calculation beats a source's claim, which
    beats a derived spread. Nothing here invents a mortgage balance, and
    ``mortgage_balance=None`` never becomes ``0`` — "no mortgage reported" and
    "no mortgage" are different facts and the engine cannot tell them apart.
    """
    assessment = EquityAssessment(
        estimated_value=estimated_value,
        mortgage_balance=mortgage_balance,
        liens_total=liens,
        asking_price=asking_price,
    )

    def percentage(amount: float) -> Optional[float]:
        if not estimated_value:
            return None
        return amount / estimated_value

    # --- 1. the real calculation ---------------------------------------
    if estimated_value is not None and mortgage_balance is not None:
        encumbrances = mortgage_balance + (liens or 0.0)
        amount = estimated_value - encumbrances
        assessment.equity_amount = amount
        assessment.equity_percentage = percentage(amount)
        assessment.equity_status = EquityStatus.CALCULATED
        # Never more confident than the weaker of the two inputs.
        assessment.equity_confidence = min(
            value_confidence,
            mortgage_confidence if mortgage_confidence is not Confidence.UNKNOWN else value_confidence,
            key=lambda c: c.rank,
        )
        parts = [f"{money(estimated_value)} value less {money(mortgage_balance)} mortgage"]
        if liens:
            parts.append(f"less {money(liens)} in known liens")
        assessment.basis = " ".join(parts)
        if liens is None:
            assessment.caveats.append(
                "No lien search has been run. Unrecorded or junior liens would reduce this."
            )
        if amount < 0:
            assessment.caveats.append(
                "Negative equity: the debt reported exceeds the value. A short sale, "
                "not a wholesale, unless the numbers are wrong."
            )
        return assessment

    # --- 2. a source's own equity figure --------------------------------
    if reported_equity is not None:
        assessment.equity_amount = reported_equity
        assessment.equity_percentage = percentage(reported_equity)
        assessment.equity_status = EquityStatus.REPORTED
        assessment.equity_confidence = Confidence.LOW
        assessment.basis = "equity figure supplied by the lead source, not verified here"
        assessment.caveats.append(
            "This is the source's number. No mortgage balance was checked against it."
        )
        return assessment

    # --- 3. the spread, honestly labelled --------------------------------
    if estimated_value is not None and asking_price is not None:
        amount = estimated_value - asking_price
        assessment.equity_amount = amount
        assessment.equity_percentage = percentage(amount)
        assessment.equity_status = EquityStatus.DERIVED
        assessment.equity_confidence = Confidence.LOW
        assessment.basis = (
            f"{money(estimated_value)} value less the {money(asking_price)} asking price"
        )
        assessment.caveats.append(
            "This is the spread between value and asking price, NOT equity. It equals "
            "equity only if the property is free and clear. No mortgage balance is known."
        )
        return assessment

    # --- 4. nothing ------------------------------------------------------
    missing = []
    if estimated_value is None:
        missing.append("an estimated value")
    if mortgage_balance is None:
        missing.append("a mortgage balance")
    assessment.basis = "need " + " and ".join(missing) if missing else "no inputs available"
    assessment.caveats.append(
        "Mortgage balances come from public records, which this engine does not have "
        "access to. It will not guess one."
    )
    return assessment
