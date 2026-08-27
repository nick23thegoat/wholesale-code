"""Ranked tables read straight from the lead database.

``--top-deals``, ``--hot-leads``, ``--search`` and ``--watchlist`` all render
through here, so one row always means the same thing whichever command printed
it. Every figure carries the price or basis it was measured at — a bare fee
next to an offer that was never accepted is the reporting bug this engine has
already been bitten by once.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..formatting import money
from ..storage import StoredLead

WIDTH = 132

#: Columns exported by ``--export-top-deals`` / ``--export-hot`` /
#: ``--export-watchlist``. One row per stored lead.
DEAL_COLUMNS: List[str] = [
    "rank",
    "property_id",
    "address",
    "city",
    "state",
    "county",
    "zip",
    "property_type",
    "status",
    "lead_score",
    "deal_score",
    "priority_score",
    "priority_band",
    "arv",
    "arv_confidence",
    "comp_confidence",
    "repair_estimate",
    "asking_price",
    "mao",
    "recommended_offer",
    "target_wholesale_fee",
    "potential_fee",
    "fee_status",
    "equity_amount",
    "equity_percentage",
    "equity_status",
    "distress_count",
    "days_on_market",
    "final_decision",
    "first_seen",
    "last_seen",
    "times_seen",
    "source",
]


def deal_row(
    row: StoredLead, rank: int = 0, target_wholesale_fee: float = 18_000.0
) -> Dict[str, Any]:
    """Flatten one stored lead into an export row."""
    return {
        "rank": rank,
        "property_id": row.dedupe_key,
        "address": row.address,
        "city": row.city,
        "state": row.state,
        "county": row.county,
        "zip": row.zip_code,
        "property_type": row.property_type,
        "status": row.status,
        "lead_score": row.lead_score,
        "deal_score": row.deal_score,
        "priority_score": row.priority_score,
        "priority_band": row.priority_band,
        "arv": row.arv,
        "arv_confidence": row.arv_confidence,
        "comp_confidence": row.comp_confidence,
        "repair_estimate": row.repair_estimate,
        "asking_price": row.asking_price,
        "mao": row.mao,
        "recommended_offer": row.recommended_offer,
        "target_wholesale_fee": target_wholesale_fee,
        "potential_fee": row.potential_fee,
        "fee_status": row.fee_status,
        "equity_amount": row.equity_amount,
        "equity_percentage": (
            round(row.equity_percentage, 4) if row.equity_percentage is not None else None
        ),
        "equity_status": row.equity_status,
        "distress_count": row.distress_count,
        "days_on_market": row.days_on_market,
        "final_decision": row.final_decision,
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
        "times_seen": row.times_seen,
        "source": row.source,
    }


def deal_rows(
    rows: Sequence[StoredLead], target_wholesale_fee: float = 18_000.0
) -> List[Dict[str, Any]]:
    return [
        deal_row(row, index, target_wholesale_fee) for index, row in enumerate(rows, start=1)
    ]


# ---------------------------------------------------------------------------
# Console tables
# ---------------------------------------------------------------------------


def _score(value: Optional[float]) -> str:
    return "  —  " if value is None else f"{value:5.1f}"


def render_deal_table(
    rows: Sequence[StoredLead],
    title: str,
    subtitle: str = "",
    target_wholesale_fee: float = 18_000.0,
) -> str:
    """The ranked table: RANK / ADDRESS / scores / economics / decision."""
    lines = ["=" * WIDTH, title, "=" * WIDTH]
    if subtitle:
        lines.append(subtitle)
        lines.append("-" * WIDTH)
    lines.append(
        f"{'#':>3} {'ADDRESS':<26}{'ST':<4}{'LEAD':>6}{'DEAL':>6}{'PRIO':>6} "
        f"{'BAND':<12}{'ARV':>10}{'REPAIRS':>10}{'MAO':>10}{'OFFER':>10}"
        f"{'FEE':>10}  {'DECISION':<16}"
    )
    lines.append("-" * WIDTH)

    if not rows:
        lines.append("  Nothing matched. Run --hunt first, or widen the filters.")
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index:>3} {(row.address or row.display_id())[:25]:<26}"
            f"{row.state or '--':<4}"
            f"{_score(row.lead_score)}{_score(row.deal_score)}{_score(row.priority_score)} "
            f"{(row.priority_band or ''):<12}"
            f"{money(row.arv, unknown='—'):>10}"
            f"{money(row.repair_estimate, unknown='—'):>10}"
            f"{money(row.mao, unknown='—'):>10}"
            f"{money(row.recommended_offer, unknown='—'):>10}"
            f"{money(row.potential_fee, unknown='—'):>10}  "
            f"{(row.final_decision or row.status)[:15]:<16}"
        )

    lines.append("-" * WIDTH)
    lines.append(f"{len(rows)} propert{'y' if len(rows) == 1 else 'ies'}.")
    lines.append(
        "LEAD = worth a call. DEAL = worth a contract. PRIO = what to work first. "
        "Three separate tests."
    )
    lines.append(
        f"FEE is the assignment fee at the price on the table, against a "
        f"{money(target_wholesale_fee)} target. BELOW TARGET is a label, not a rejection."
    )
    lines.append("=" * WIDTH)
    return "\n".join(lines)


def render_watchlist(rows: Sequence[StoredLead], counts: Dict[str, int]) -> str:
    """The pipeline view: what sits at each status, furthest along first."""
    lines = ["=" * WIDTH, "DEAL WATCHLIST", "=" * WIDTH]
    live = " · ".join(f"{name} {count}" for name, count in counts.items() if count)
    lines.append(live or "nothing stored yet")
    lines.append("-" * WIDTH)
    lines.append(
        f"{'STATUS':<16}{'ADDRESS':<28}{'ST':<4}{'PRIO':>6} {'BAND':<12}"
        f"{'OFFER':>10}{'FEE':>10}  {'LAST SEEN':<12}"
    )
    lines.append("-" * WIDTH)
    if not rows:
        lines.append("  Nothing on the watchlist. Move a lead with --set-status.")
    for row in rows:
        lines.append(
            f"{row.status:<16}{(row.address or row.display_id())[:27]:<28}"
            f"{row.state or '--':<4}{_score(row.priority_score)} "
            f"{(row.priority_band or ''):<12}"
            f"{money(row.recommended_offer, unknown='—'):>10}"
            f"{money(row.potential_fee, unknown='—'):>10}  {row.last_seen:<12}"
        )
    lines.append("=" * WIDTH)
    return "\n".join(lines)
