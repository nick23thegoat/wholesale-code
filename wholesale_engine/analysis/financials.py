"""Pure deal math.

Every function here is a plain calculation with no I/O and no hidden state,
which is what makes the unit tests in ``tests/test_financials.py`` meaningful.
The headline formula is::

    MAO = (ARV x 70%) - repairs - wholesale fee
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..config import DEFAULT_CONFIG, EngineConfig
from ..models.results import MAOScenario


def seventy_percent_arv(arv: float, config: EngineConfig = DEFAULT_CONFIG) -> float:
    """The ARV x 70% line item (the percentage itself is configurable)."""
    return arv * config.arv_percentage


def end_buyer_max_price(
    arv: float, repairs: float, config: EngineConfig = DEFAULT_CONFIG
) -> float:
    """(ARV x 70%) - repairs: the most a cash end buyer can pay.

    This is the ceiling your assignment price has to fit under. It is also the
    reason MAO and the fee are two different things: MAO is this ceiling less
    the fee you are reserving for yourself.
    """
    return seventy_percent_arv(arv, config) - repairs


def maximum_allowable_offer(
    arv: float,
    repairs: float,
    config: EngineConfig = DEFAULT_CONFIG,
    wholesale_fee: Optional[float] = None,
) -> float:
    """MAO = (ARV x 70%) - repairs - target wholesale fee.

    Equivalently: ``end_buyer_max_price() - target_wholesale_fee``. Buying at
    exactly MAO yields exactly the target fee and nothing more; buying below it
    yields the target fee **plus** the cushion.

    The result can legitimately be negative: a low ARV against heavy repairs
    means there is no price at which this deal works. Callers must not clamp
    that to zero — a negative MAO is the answer.
    """
    fee = config.target_wholesale_fee if wholesale_fee is None else wholesale_fee
    return end_buyer_max_price(arv, repairs, config) - fee


def assignment_price(
    purchase_price: float,
    config: EngineConfig = DEFAULT_CONFIG,
    wholesale_fee: Optional[float] = None,
) -> float:
    """What the end buyer would pay: purchase price + the assignment fee."""
    fee = config.target_wholesale_fee if wholesale_fee is None else wholesale_fee
    return purchase_price + fee


def gross_spread(mao: float, purchase_price: float) -> float:
    """Deal cushion = MAO - recommended purchase price.

    **This is not the wholesale fee.** MAO already reserves the target fee, so
    this is the extra room on top of it. Use :func:`potential_wholesale_fee`
    for the fee itself.
    """
    return mao - purchase_price


def potential_wholesale_fee(
    arv: float,
    repairs: float,
    purchase_price: float,
    config: EngineConfig = DEFAULT_CONFIG,
) -> float:
    """The assignment fee this deal actually supports at ``purchase_price``.

    ``end_buyer_max_price - purchase_price``: what is left between what you pay
    and the most an end buyer can pay. Buy at MAO and this equals the target
    fee exactly; buy below MAO and it is the target fee plus the cushion; pay
    above MAO and it falls below target.
    """
    return end_buyer_max_price(arv, repairs, config) - purchase_price


def buyer_margin(
    arv: float, repairs: float, assignment_price: float, config: EngineConfig = DEFAULT_CONFIG
) -> float:
    """Room left for the END BUYER at your assignment price.

    Negative means no buyer following the same 70% rule can take the deal at
    that price, however good your own numbers look.
    """
    return end_buyer_max_price(arv, repairs, config) - assignment_price


def classify_wholesale_fee(
    fee: Optional[float], config: EngineConfig = DEFAULT_CONFIG
) -> "WholesaleFeeStatus":
    """MEETS TARGET / BELOW TARGET / UNKNOWN for an achievable fee."""
    from ..models.enums import WholesaleFeeStatus

    if fee is None:
        return WholesaleFeeStatus.UNKNOWN
    if fee >= config.required_wholesale_fee:
        return WholesaleFeeStatus.MEETS_TARGET
    return WholesaleFeeStatus.BELOW_TARGET


def binding_purchase_price(
    recommended_offer: Optional[float], asking_price: Optional[float]
) -> Optional[float]:
    """The price the fee test has to be measured against.

    A recommended offer is a proposal; the asking price is what is actually on
    the table. When the seller is asking more than you plan to offer, the fee
    that matters for a GO is the fee at THEIR number — otherwise every deal
    would qualify on the strength of a discount the seller never agreed to.
    """
    if recommended_offer is None:
        return asking_price
    if asking_price is None:
        return recommended_offer
    return max(recommended_offer, asking_price)


def round_offer_down(amount: float, config: EngineConfig = DEFAULT_CONFIG) -> float:
    """Round an offer down to a clean increment. Never rounds up in your favour."""
    step = config.offer_rounding
    if step <= 0:
        return amount
    if amount <= 0:
        return 0.0
    return math.floor(amount / step) * step


def recommended_offer(
    mao: float,
    risk_discount: float,
    asking_price: Optional[float] = None,
    config: EngineConfig = DEFAULT_CONFIG,
) -> float:
    """Recommended purchase price: MAO less a risk haircut, never above asking.

    The engine deliberately does not recommend paying full MAO. ``risk_discount``
    is a fraction (0.10 = offer 10% below MAO) produced by
    :func:`offer_risk_discount`; it is clamped to the configured band.
    """
    if mao <= 0:
        return 0.0
    discount = min(max(risk_discount, config.min_offer_discount), config.max_offer_discount)
    offer = mao * (1.0 - discount)
    if asking_price is not None and asking_price > 0:
        offer = min(offer, asking_price)
    return round_offer_down(max(offer, 0.0), config)


def offer_risk_discount(
    risk_points: Sequence[Tuple[str, float]],
    config: EngineConfig = DEFAULT_CONFIG,
) -> Tuple[float, List[str]]:
    """Turn labelled risk contributions into a single discount below MAO.

    Returns the clamped discount fraction and the human-readable reasons that
    produced it, so the report can explain why the offer sits where it does.
    """
    total = sum(points for _, points in risk_points)
    reasons = [label for label, points in risk_points if points > 0]
    discount = min(max(total, config.min_offer_discount), config.max_offer_discount)
    return discount, reasons


def discount_from_arv(price: float, arv: float) -> Optional[float]:
    """How far below ARV a price sits, as a fraction (0.35 = 35% below ARV)."""
    if arv <= 0:
        return None
    return (arv - price) / arv


def equity_position(arv: float, price: float, repairs: float) -> float:
    """Dollars of ARV left over after buying and repairing at ``price``."""
    return arv - price - repairs


def repair_ratio(repairs: float, arv: float) -> Optional[float]:
    """Repairs as a fraction of ARV — the capital-intensity of the project."""
    if arv <= 0:
        return None
    return repairs / arv


def rent_to_value_ratio(monthly_rent: float, arv: float) -> Optional[float]:
    """Monthly rent divided by ARV (the "1% rule" ratio) for the landlord exit."""
    if arv <= 0:
        return None
    return monthly_rent / arv


def build_mao_scenarios(
    arv: float,
    repair_low: Optional[float],
    repair_mid: Optional[float],
    repair_high: Optional[float],
    asking_price: Optional[float] = None,
    config: EngineConfig = DEFAULT_CONFIG,
) -> List[MAOScenario]:
    """MAO recomputed under the low / mid / high rehab scenarios."""
    scenarios: List[MAOScenario] = []
    for name, repairs in (
        ("Low rehab", repair_low),
        ("Mid rehab", repair_mid),
        ("High rehab", repair_high),
    ):
        if repairs is None:
            continue
        mao = maximum_allowable_offer(arv, repairs, config)
        spread = None if asking_price is None else mao - asking_price
        scenarios.append(
            MAOScenario(name=name, repairs=repairs, mao=mao, spread_vs_asking=spread)
        )
    return scenarios


def implied_arv_for_offer(
    purchase_price: float,
    repairs: float,
    config: EngineConfig = DEFAULT_CONFIG,
    wholesale_fee: Optional[float] = None,
) -> float:
    """The ARV that would be required to justify ``purchase_price``.

    Used to answer "what would have to be true for the asking price to work?"
    — the reverse of the MAO formula.
    """
    fee = config.wholesale_fee if wholesale_fee is None else wholesale_fee
    return (purchase_price + repairs + fee) / config.arv_percentage


def max_repairs_for_offer(
    arv: float,
    purchase_price: float,
    config: EngineConfig = DEFAULT_CONFIG,
    wholesale_fee: Optional[float] = None,
) -> float:
    """The largest rehab budget that still supports ``purchase_price``."""
    fee = config.wholesale_fee if wholesale_fee is None else wholesale_fee
    return seventy_percent_arv(arv, config) - purchase_price - fee
