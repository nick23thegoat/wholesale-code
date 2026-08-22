"""Shared display helpers.

Kept at package root so the analysis layer and the report layer format money
the same way without either importing the other.
"""

from __future__ import annotations

from typing import Optional

UNKNOWN = "NOT PROVIDED"


def money(value: Optional[float], unknown: str = UNKNOWN) -> str:
    """Format dollars with the sign outside the symbol: ``-$18,200``, not ``$-18,200``."""
    if value is None:
        return unknown
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"
