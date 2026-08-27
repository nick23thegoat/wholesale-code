"""A local ledger of billable requests, so a monthly cap is enforceable.

RentCast's free plan allows 50 successful requests per month, and exceeding a
plan's limit incurs a per-request overage fee. The vendor knows the true count;
this ledger is our own conservative shadow copy, kept so the engine can refuse
to start a run it cannot afford instead of discovering the limit by being
billed for it.

Three rules it follows:

* **only successful requests are recorded** — RentCast bills successes only, so
  a 401 or a timeout must not decrement the budget, and a cache hit costs
  nothing and is never recorded
* **the month rolls over on its own** — the ledger is keyed by calendar month
  in UTC, so a new month starts fresh without anyone remembering to reset it
* **when the count is uncertain, it over-counts** — a recorded request that
  turned out not to be billed costs you nothing; an unrecorded one that *was*
  billed is how an overage happens

The ledger is advisory, not authoritative. Your real usage lives on the vendor's
dashboard, and :meth:`QuotaLedger.render` says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

#: Where the ledger is kept. Git-ignored — it is machine-local state.
from ..paths import ledger_path as _ledger_path

DEFAULT_LEDGER = _ledger_path()

#: Environment overrides, per provider slot.
LIMIT_VARS = {"rentcast": "MAX_RENTCAST"}

#: Plan limits we ship with. RentCast's free tier is 50 successful requests
#: per month; raise it in .env when you upgrade the plan, never in source.
DEFAULT_LIMITS = {"rentcast": 50}


def current_period(now: Optional[datetime] = None) -> str:
    """The billing period key, ``YYYY-MM`` in UTC."""
    moment = now or datetime.now(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


class QuotaExceeded(RuntimeError):
    """A request was refused because the monthly cap is spent."""


@dataclass
class QuotaLedger:
    """Requests spent this month, per provider, persisted to disk."""

    provider: str = "rentcast"
    limit: int = 50
    path: Path = DEFAULT_LEDGER
    #: period -> provider -> count
    _counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: Requests served from cache this run. Free, and reported to prove it.
    cache_hits: int = 0

    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        provider: str = "rentcast",
        path: Optional[Path] = None,
        limit: Optional[int] = None,
    ) -> "QuotaLedger":
        """Read the ledger, falling back to an empty one if it is unreadable.

        A corrupt ledger must not stop a run, but it must not silently read as
        "nothing spent" either — that would uncap the budget. It reads as the
        limit already spent, which fails safe.
        """
        target = Path(path) if path else DEFAULT_LEDGER
        resolved_limit = (
            limit
            if limit is not None
            else _env_int(LIMIT_VARS.get(provider, ""), DEFAULT_LIMITS.get(provider, 50))
        )
        ledger = cls(provider=provider, limit=resolved_limit, path=target)
        if not target.exists():
            return ledger
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                ledger._counts = {
                    str(period): {str(k): int(v) for k, v in counts.items()}
                    for period, counts in raw.items()
                    if isinstance(counts, dict)
                }
        except (OSError, ValueError, TypeError):
            # Unreadable: assume fully spent rather than uncapped.
            ledger._counts = {current_period(): {provider: resolved_limit}}
        return ledger

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._counts, indent=2), encoding="utf-8")
        except OSError:
            # A ledger we cannot persist is still worth keeping in memory for
            # the rest of this run; losing it must not abort real work.
            pass

    # ------------------------------------------------------------------

    @property
    def used(self) -> int:
        return self._counts.get(current_period(), {}).get(self.provider, 0)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def can_spend(self, count: int = 1) -> bool:
        return self.used + count <= self.limit

    def record(self, count: int = 1) -> None:
        """Record ``count`` **successful** requests. Never called on failure."""
        if count <= 0:
            return
        period = current_period()
        self._counts.setdefault(period, {})
        self._counts[period][self.provider] = self.used + count
        self.save()

    def record_cache_hit(self, count: int = 1) -> None:
        """A response served from cache. Costs nothing and is never billed."""
        self.cache_hits += count

    def require(self, count: int = 1) -> None:
        """Raise :class:`QuotaExceeded` unless ``count`` requests are affordable."""
        if not self.can_spend(count):
            raise QuotaExceeded(
                f"{self.provider}: {self.used}/{self.limit} requests already used "
                f"this month ({current_period()}); this run needs {count} more. "
                f"Raise {LIMIT_VARS.get(self.provider, 'the limit')} in .env if you "
                "have upgraded the plan, or wait for the month to roll over. "
                "Check real usage at https://app.rentcast.io/app/api"
            )

    # ------------------------------------------------------------------

    def render(self) -> str:
        lines = [
            f"{self.provider.upper()} QUOTA — {current_period()}",
            f"  Used this month     {self.used}",
            f"  Remaining           {self.remaining}   (cap {self.limit})",
        ]
        if self.cache_hits:
            lines.append(
                f"  Served from cache   {self.cache_hits}   (free — not billed)"
            )
        lines.append(
            "  This ledger is local and advisory. Your billable count lives at"
        )
        lines.append("  https://app.rentcast.io/app/api")
        return "\n".join(lines)


def _env_int(name: str, default: int) -> int:
    if not name:
        return default
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default
