"""Display helpers for the templates.

These format; they never compute. ``money`` is imported from the package's
existing helper so a dollar figure reads the same on a phone as it does in the
terminal reports, sign outside the symbol and all.

The recurring decision here is what to show for ``None``. Everywhere else in
this engine an unknown is displayed as an unknown rather than a zero, and that
matters more on a dashboard than anywhere: a blank ARV rendered as ``$0``
looks like a property worth nothing instead of one nobody has valued yet.
"""

from __future__ import annotations

from typing import Any, Optional

from ..formatting import money as _money

#: What an unknown looks like. Short, because it appears in narrow columns.
DASH = "—"


def money(value: Optional[float]) -> str:
    """Dollars, or a dash. Never ``$0`` standing in for "not known"."""
    return DASH if value is None else _money(value)


def score(value: Optional[float]) -> str:
    """A score to one decimal, or a dash. Zero is a real score and shows as 0."""
    return DASH if value is None else f"{value:.0f}"


def number(value: Optional[float]) -> str:
    return DASH if value is None else f"{value:,.0f}"


def percent(value: Optional[float]) -> str:
    return DASH if value is None else f"{value:.0f}%"


def text(value: Any) -> str:
    """A string, or a dash for blank. Keeps empty cells from looking broken."""
    rendered = "" if value is None else str(value).strip()
    return rendered or DASH


def when(value: Any) -> str:
    """An ISO timestamp trimmed to minutes, which is all a person reads."""
    raw = text(value)
    if raw == DASH:
        return DASH
    return raw.replace("T", " ")[:16]


def band_class(band: Any) -> str:
    """A CSS class for a priority band, by keyword rather than by emoji."""
    label = str(band or "").upper()
    for keyword, name in (
        ("HOT", "hot"), ("HIGH", "high"), ("REVIEW", "review"),
        ("STRONG", "high"), ("REJECT", "reject"), ("PASS", "reject"),
    ):
        if keyword in label:
            return name
    return "neutral"


def outcome_class(outcome: Any) -> str:
    return {
        "ACCEPTED": "ok", "REJECTED": "reject", "INCOMPLETE": "review",
    }.get(str(outcome or "").upper(), "neutral")


def status_class(status: Any) -> str:
    label = str(status or "").upper()
    if label in ("OK",):
        return "ok"
    if label in ("FAILED",):
        return "reject"
    if label in ("PARTIAL", "RUNNING"):
        return "review"
    return "neutral"


#: Registered onto the Jinja environment by ``create_app``.
FILTERS = {
    "money": money,
    "score": score,
    "number": number,
    "percent": percent,
    "text": text,
    "when": when,
    "band_class": band_class,
    "outcome_class": outcome_class,
    "status_class": status_class,
}
