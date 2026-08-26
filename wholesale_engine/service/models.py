"""Request and result objects for the service layer.

Two rules shape what is in this file, and what deliberately is not.

**Nothing here duplicates a type the engine already has.** A hunt is described
by :class:`HuntCriteria`, a stored-lead query by :class:`SearchQuery`, a
result by :class:`HuntResult`, a buy box by :class:`BuyBox`. Those are already
plain dataclasses with no CLI in them, so the service passes them straight
through. Re-declaring them here would create two definitions of the same idea
and guarantee they drift.

**What IS here is the orchestration that previously existed only inside
argparse.** Which provider to use, where the CSV fallback should look, what
the caps are for this run, whether to write output files — all of that lived
as ``args.source``, ``args.leads``, ``args.research_limit`` and so on, which
meant a scheduled job or a web request could not express it without
fabricating a ``Namespace``. :class:`HuntRequest` is that same information as
an ordinary object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..buybox import BuyBox
from ..hunt import HuntBudget, HuntResult
from ..providers.base import PropertyDataProvider
from ..providers.criteria import HuntCriteria


@dataclass
class HuntRequest:
    """Everything one hunt needs that is not already in the criteria.

    Defaults match what the CLI does with no flags, so
    ``service.run_hunt(HuntRequest())`` and a bare ``--hunt`` behave the same.
    """

    #: What to look for. The criteria object the funnel already understands.
    criteria: Optional[HuntCriteria] = None

    #: Registered provider name (``DATA_PROVIDER`` / ``--source``).
    source: str = "csv"
    #: Local lead list, for the csv provider. ``None`` falls back to the
    #: bundled fictional sample, which is announced rather than assumed.
    leads_path: Optional[Path] = None
    comps_path: Optional[Path] = None

    #: Caps for this run. ``None`` means "read the environment", which is
    #: what the CLI does when no cap flag is given.
    budget: Optional[HuntBudget] = None
    research_limit: Optional[int] = None
    comps_limit: Optional[int] = None

    #: Where results are persisted. ``":memory:"`` disables persistence.
    db_path: Optional[Path] = None
    #: ``False`` runs the funnel without touching the database at all.
    persist: bool = True

    #: Output files. Off by default for programmatic callers — a web request
    #: wants the result object, not four files on the server's disk.
    write_outputs: bool = False
    output_dir: Optional[Path] = None
    write_json: bool = True

    #: Record a row in the ``runs`` table for this hunt. Off by default so
    #: behaviour is unchanged; the per-property decision log is a separate
    #: piece of work and is not written here.
    record_run: bool = False
    #: How this run was started, for the run history. manual | scheduled | api
    trigger: str = "manual"
    #: Recorded on the run row. TEST or LIVE — the caller knows which it is.
    mode: str = "TEST"

    #: Allow falling back to the local CSV provider when the requested one has
    #: no credentials. The CLI does this; a scheduled LIVE job should not,
    #: because silently reading yesterday's CSV is worse than failing loudly.
    allow_csv_fallback: bool = True


@dataclass
class ProviderStatus:
    """One registered adapter, and whether it can actually be used."""

    name: str
    description: str = ""
    is_local: bool = False
    configured: bool = False
    missing_settings: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    documentation: str = ""

    @property
    def usable(self) -> bool:
        return self.is_local or self.configured


@dataclass
class ProviderChoice:
    """The outcome of asking for a provider by name.

    ``provider`` is ``None`` when nothing usable could be built; ``error``
    then says why, in a sentence meant to be shown to a person.
    """

    provider: Optional[PropertyDataProvider] = None
    #: What was actually built, which may differ from what was asked for.
    resolved_name: str = ""
    requested_name: str = ""
    #: True when the requested provider was unusable and CSV stood in.
    fell_back: bool = False
    notices: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.provider is not None


@dataclass
class HuntOutcome:
    """What one hunt produced, plus what the run wants to tell you.

    ``result`` is the engine's own :class:`HuntResult`, untouched. Everything
    else is orchestration: which provider actually ran, what got written, and
    any notice a caller should surface.
    """

    result: Optional[HuntResult] = None
    provider_name: str = ""
    fell_back: bool = False
    notices: List[str] = field(default_factory=list)
    error: str = ""
    #: label -> path, for whatever was written. Empty when nothing was.
    written: Dict[str, Path] = field(default_factory=dict)
    db_path: Optional[Path] = None
    run_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.error

    @property
    def leads(self) -> List[Any]:
        """The prioritized entries, or an empty list on failure."""
        return list(self.result.prioritized) if self.result is not None else []


@dataclass
class BuyBoxView:
    """The buy box as it is on disk right now.

    ``warnings`` is never a failure: a missing or unreadable file yields
    working defaults plus an explanation, because a scheduled run must not die
    over a badly typed field.
    """

    buy_box: BuyBox
    path: Path
    warnings: List[str] = field(default_factory=list)
    #: False when the file does not exist and these are the defaults.
    exists: bool = False

    @property
    def search_count(self) -> int:
        """API searches one full run of this buy box costs."""
        return self.buy_box.search_count


@dataclass
class SaveResult:
    """The outcome of an attempted buy-box save.

    ``problems`` carries every validation failure at once, so a form can show
    the complete picture instead of one field per round trip.
    """

    saved: bool = False
    path: Optional[Path] = None
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    buy_box: Optional[BuyBox] = None

    @property
    def ok(self) -> bool:
        return self.saved
