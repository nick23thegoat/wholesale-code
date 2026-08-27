"""The buy box, as editable data rather than code.

Everything about *what you are looking for* lives in one JSON file so it can be
changed from a phone, by a scheduled job, or by hand, without a deploy and
without touching Python. The engine reads it; nothing writes to it except an
explicit save.

Three properties this has to hold, because it runs unattended on a server:

* **a bad file never kills the run.** Invalid JSON, an unknown key, a string
  where a number belongs — each is reported as a warning and the run continues
  on the last-known-good or default values. A scheduled 3am job must not die
  because a field was edited badly from a phone.
* **validation happens before the save, not after.** :meth:`BuyBox.validate`
  returns every problem at once, so the web form can reject a bad edit and show
  all of it rather than failing one field at a time.
* **an unknown value never narrows the search.** Consistent with the rest of
  the engine: a missing bound means "no constraint", never zero.

The file lives outside the package (default ``config/buybox.json``, override
with ``BUYBOX_PATH``) so that ``git pull`` on the server never clobbers your
settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    DEFAULT_PROPERTY_TYPES,
    DEFAULT_TARGET_STATES,
    LEAD_SIGNALS,
    MAX_PROPERTY_PRICE,
    MIN_PROPERTY_PRICE,
    TARGET_WHOLESALE_FEE,
)
from .providers.criteria import HuntCriteria

#: Where the buy box lives. Outside the package on purpose.
from .paths import config_dir as _config_dir

DEFAULT_PATH = _config_dir() / "buybox.json"
PATH_VAR = "BUYBOX_PATH"

#: Property types the analyzer can underwrite. Land and commercial are absent
#: deliberately — they do not fit the ARV/rehab model.
ALLOWED_PROPERTY_TYPES = (
    "single_family", "duplex", "triplex", "fourplex", "townhouse", "condo",
    "multi_family", "mobile",
)

#: Settings :meth:`BuyBox.to_criteria` actually applies. Each one maps onto a
#: field :class:`HuntCriteria` already has and the funnel already filters on,
#: so the buy box supplies inputs to existing rules rather than adding rules.
APPLIED_FIELDS = (
    "states", "counties", "cities", "zip_codes", "property_types",
    "min_price", "max_price", "min_equity",
    "min_lead_score", "min_deal_score", "required_signals",
)

#: Settings that describe the buy box rather than filtering with it.
DESCRIPTIVE_FIELDS = ("name", "notes", "enabled")

#: Shape filters with no implementation anywhere in the engine yet. They are
#: valid to store and valid to save — the web form should let you set them —
#: but nothing filters on them, so a buy box carrying one is quieter than the
#: person setting it expects. :meth:`unsupported_settings` says so out loud.
NOT_IMPLEMENTED_FIELDS = (
    "min_beds", "max_beds", "min_baths", "min_sqft", "max_sqft",
    "min_year_built", "max_year_built",
)

#: Settings the engine *does* honour, but which reach it from somewhere other
#: than HuntCriteria — ``min_signal_count`` from LeadHunterConfig, the two fee
#: figures from EngineConfig. Putting them in the buy box does not move them,
#: so setting one here and expecting a hunt to change is a trap. Reported for
#: the same reason as the group above: a filter you think is on and is not is
#: worse than one you know is off.
NOT_ROUTED_FIELDS = (
    "min_signal_count", "target_wholesale_fee", "min_viable_wholesale_fee",
)


def config_path() -> Path:
    raw = os.environ.get(PATH_VAR, "").strip()
    return Path(raw) if raw else DEFAULT_PATH


@dataclass
class BuyBox:
    """What to look for, and what counts as worth your attention.

    Every field maps onto something the engine already understands. Nothing
    here re-implements scoring or analysis — it only supplies their inputs.
    """

    # --- identity ---------------------------------------------------------
    name: str = "default"
    #: Free-text note to yourself. Never interpreted.
    notes: str = ""
    enabled: bool = True

    # --- geography --------------------------------------------------------
    states: List[str] = field(default_factory=lambda: list(DEFAULT_TARGET_STATES))
    #: The searches the scheduler actually runs. Each ZIP is one API request,
    #: so this list IS your monthly search budget. Keep it short and good.
    zip_codes: List[str] = field(default_factory=list)
    cities: List[str] = field(default_factory=list)
    counties: List[str] = field(default_factory=list)

    # --- what kind of property -------------------------------------------
    property_types: List[str] = field(
        default_factory=lambda: list(DEFAULT_PROPERTY_TYPES)
    )
    min_beds: Optional[float] = None
    max_beds: Optional[float] = None
    min_baths: Optional[float] = None
    min_sqft: Optional[int] = None
    max_sqft: Optional[int] = None
    min_year_built: Optional[int] = None
    max_year_built: Optional[int] = None

    # --- price ------------------------------------------------------------
    #: A SEARCH bound and a buyer-capacity ceiling — not a claim that anything
    #: inside it is a deal. Every property still faces the full underwriting.
    min_price: Optional[float] = MIN_PROPERTY_PRICE
    max_price: Optional[float] = MAX_PROPERTY_PRICE

    # --- what makes it worth working -------------------------------------
    #: Signals that must be present (any-of). Empty means no requirement.
    #: A lead whose signal is UNKNOWN is never rejected for it.
    required_signals: List[str] = field(default_factory=list)
    min_signal_count: int = 0
    min_equity: Optional[float] = None

    # --- score gates ------------------------------------------------------
    min_lead_score: float = 0.0
    min_deal_score: float = 0.0

    # --- economics --------------------------------------------------------
    #: A TARGET, not a minimum. A deal below it is labelled, never rejected.
    target_wholesale_fee: float = TARGET_WHOLESALE_FEE
    #: The fee below which a deal stops being called a green light. This is an
    #: economic viability floor and is a different thing from the target.
    min_viable_wholesale_fee: float = 10_000.0

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        """Every problem with this buy box, or an empty list.

        Returns all of them at once so a form can show the complete picture
        rather than making you fix one field per round trip.
        """
        problems: List[str] = []

        if not str(self.name).strip():
            problems.append("name: cannot be blank")

        for signal in self.required_signals:
            if signal not in LEAD_SIGNALS:
                problems.append(
                    f"required_signals: '{signal}' is not a signal the engine "
                    f"knows. Valid: {', '.join(LEAD_SIGNALS)}"
                )

        for kind in self.property_types:
            if kind not in ALLOWED_PROPERTY_TYPES:
                problems.append(
                    f"property_types: '{kind}' is not underwritable. Valid: "
                    f"{', '.join(ALLOWED_PROPERTY_TYPES)}"
                )

        for state in self.states:
            if len(str(state).strip()) != 2:
                problems.append(f"states: '{state}' is not a 2-letter code")

        for zip_code in self.zip_codes:
            digits = str(zip_code).strip()
            if not digits.isdigit() or len(digits) != 5:
                problems.append(f"zip_codes: '{zip_code}' is not a 5-digit ZIP")

        problems.extend(self._check_ranges())

        for name, value in (
            ("min_lead_score", self.min_lead_score),
            ("min_deal_score", self.min_deal_score),
        ):
            if value is not None and not (0 <= float(value) <= 100):
                problems.append(f"{name}: must be between 0 and 100, got {value}")

        if self.min_signal_count < 0:
            problems.append("min_signal_count: cannot be negative")
        if self.min_signal_count > len(LEAD_SIGNALS):
            problems.append(
                f"min_signal_count: {self.min_signal_count} exceeds the "
                f"{len(LEAD_SIGNALS)} signals that exist"
            )

        if self.target_wholesale_fee <= 0:
            problems.append("target_wholesale_fee: must be positive")
        if self.min_viable_wholesale_fee < 0:
            problems.append("min_viable_wholesale_fee: cannot be negative")
        if self.min_viable_wholesale_fee > self.target_wholesale_fee:
            problems.append(
                f"min_viable_wholesale_fee (${self.min_viable_wholesale_fee:,.0f}) "
                f"is above target_wholesale_fee (${self.target_wholesale_fee:,.0f}). "
                "The viability floor must sit at or below the target — otherwise "
                "no deal can ever reach the target without already clearing it."
            )

        if not (self.zip_codes or self.cities or self.counties or self.states):
            problems.append(
                "no geography set: give at least one of states, zip_codes, "
                "cities or counties, or the search has nowhere to look"
            )
        return problems

    def _check_ranges(self) -> List[str]:
        """Every low/high pair, checked the same way."""
        problems: List[str] = []
        pairs = (
            ("price", self.min_price, self.max_price),
            ("beds", self.min_beds, self.max_beds),
            ("sqft", self.min_sqft, self.max_sqft),
            ("year_built", self.min_year_built, self.max_year_built),
        )
        for label, low, high in pairs:
            if low is not None and float(low) < 0:
                problems.append(f"min_{label}: cannot be negative")
            if high is not None and float(high) < 0:
                problems.append(f"max_{label}: cannot be negative")
            if low is not None and high is not None and float(low) > float(high):
                problems.append(
                    f"min_{label} ({low}) is above max_{label} ({high}) — "
                    "nothing can match that range"
                )
        if self.min_equity is not None and float(self.min_equity) < 0:
            problems.append("min_equity: cannot be negative")
        return problems

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ------------------------------------------------------------------
    # Conversion into what the engine already understands
    # ------------------------------------------------------------------

    def to_criteria(self, limit: Optional[int] = None) -> HuntCriteria:
        """This buy box as :class:`HuntCriteria`.

        The conversion is deliberately dull: each of the eleven applied
        settings maps onto a criteria field the funnel already filters on. No
        rule is implemented here and none is duplicated — the buy box supplies
        inputs to ``cheap_filter`` and ``apply_filters``, which keep owning the
        decisions.

        The seven shape filters in :data:`NOT_IMPLEMENTED_FIELDS` are **not**
        carried across, because there is nowhere to carry them to yet.
        :meth:`unsupported_settings` reports any that are set rather than
        letting them look applied.
        """
        return HuntCriteria(
            states=tuple(self.states),
            counties=tuple(self.counties),
            cities=tuple(self.cities),
            zip_codes=tuple(self.zip_codes),
            property_types=tuple(self.property_types),
            min_price=self.min_price,
            max_price=self.max_price,
            min_equity=self.min_equity,
            required_signals=tuple(self.required_signals),
            min_lead_score=self.min_lead_score or 0.0,
            min_deal_score=self.min_deal_score or 0.0,
            limit=limit,
        )

    def unsupported_settings(self) -> List[str]:
        """Settings this buy box carries that no hunt will act on.

        Only fields actually **set** are reported, so a buy box that leaves
        them alone produces no noise. Both groups are legitimate to store and
        legitimate to save; what would not be legitimate is letting someone
        set a minimum bedroom count, watch a hunt run, and assume it was
        applied.
        """
        blank = BuyBox()
        problems: List[str] = []

        for name in NOT_IMPLEMENTED_FIELDS:
            value = getattr(self, name, None)
            if value is not None and value != getattr(blank, name, None):
                problems.append(
                    f"{name}={value} is NOT APPLIED: no filter for it exists yet. "
                    "It is stored and will start working when one does."
                )

        for name in NOT_ROUTED_FIELDS:
            value = getattr(self, name, None)
            if value is not None and value != getattr(blank, name, None):
                problems.append(
                    f"{name}={value} is NOT APPLIED by the buy box: the engine "
                    "honours it, but from its own configuration rather than "
                    "from here."
                )
        return problems

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def field_names(cls) -> Tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> Tuple["BuyBox", List[str]]:
        """Build from a mapping. Returns ``(buy_box, warnings)``.

        An unknown key is a warning, not a failure: a config written by a newer
        version of the engine must not stop an older one from running, and a
        typo should be reported rather than silently dropped.
        """
        warnings: List[str] = []
        known = set(cls.field_names())
        clean: Dict[str, Any] = {}

        for key, value in (raw or {}).items():
            if key not in known:
                warnings.append(f"ignored unknown setting '{key}'")
                continue
            clean[key] = value

        box = cls()
        for key, value in clean.items():
            current = getattr(box, key)
            try:
                setattr(box, key, _coerce(key, value, current))
            except (TypeError, ValueError):
                warnings.append(
                    f"'{key}': could not read {value!r}; kept the default "
                    f"{current!r}"
                )
        return box, warnings

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> Tuple["BuyBox", List[str]]:
        """Read the buy box. Never raises — returns ``(buy_box, warnings)``.

        A missing file is not an error: it means "use the defaults", which is
        a working configuration.
        """
        target = Path(path) if path else config_path()
        if not target.exists():
            return cls(), [
                f"no buy box at {target}; using defaults. Save one to change it."
            ]
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return cls(), [
                f"could not read {target} ({exc}); using defaults so the run "
                "can continue. Fix the file and the next run picks it up."
            ]
        if not isinstance(raw, dict):
            return cls(), [f"{target} is not a JSON object; using defaults"]

        box, warnings = cls.from_dict(raw)
        warnings.extend(f"buy box: {p}" for p in box.validate())
        # Not validation failures — these settings are valid to store. They
        # are reported here because the existing warnings channel is what
        # every caller already prints, and a filter silently not running is
        # exactly the kind of thing that channel is for.
        warnings.extend(f"buy box: {p}" for p in box.unsupported_settings())
        return box, warnings

    def save(self, path: Optional[Path] = None) -> Path:
        """Write the buy box. Raises :class:`ValueError` if it is invalid.

        Validation happens here so an invalid buy box can never reach disk and
        break a scheduled run at 3am.
        """
        problems = self.validate()
        if problems:
            raise ValueError(
                "refusing to save an invalid buy box:\n  - "
                + "\n  - ".join(problems)
            )
        target = Path(path) if path else config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename, so an interrupted save cannot leave a half-written
        # file that the next scheduled run would fail to parse.
        staging = target.with_suffix(".json.tmp")
        staging.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        staging.replace(target)
        return target

    # ------------------------------------------------------------------

    def describe(self) -> str:
        parts = []
        if self.zip_codes:
            parts.append(f"ZIPs {', '.join(self.zip_codes)}")
        if self.cities:
            parts.append(f"cities {', '.join(self.cities)}")
        if self.counties:
            parts.append(f"counties {', '.join(self.counties)}")
        if self.states:
            parts.append("/".join(self.states))
        if self.min_price is not None or self.max_price is not None:
            low = f"${self.min_price:,.0f}" if self.min_price is not None else "any"
            high = f"${self.max_price:,.0f}" if self.max_price is not None else "any"
            parts.append(f"price {low}–{high}")
        if self.required_signals:
            parts.append("signals: " + ", ".join(self.required_signals))
        if self.min_lead_score:
            parts.append(f"lead ≥ {self.min_lead_score:g}")
        if self.min_deal_score:
            parts.append(f"deal ≥ {self.min_deal_score:g}")
        return "; ".join(parts) or "no constraints"

    @property
    def search_count(self) -> int:
        """How many API searches one full run of this buy box costs.

        One request per ZIP, per city, or per county — this is the number that
        eats the monthly quota, so it is surfaced everywhere the buy box is.
        """
        return max(len(self.zip_codes) + len(self.cities) + len(self.counties), 1)


#: Fields that are lists of plain strings, normalized on load.
_UPPER_LISTS = ("states",)
_LOWER_LISTS = ("property_types", "required_signals", "cities", "counties")
_PLAIN_LISTS = ("zip_codes",)


def _coerce(key: str, value: Any, current: Any) -> Any:
    """Read one setting, tolerating the shapes a hand-edited file produces."""
    if key in _UPPER_LISTS:
        return [str(v).strip().upper() for v in _as_list(value) if str(v).strip()]
    if key in _LOWER_LISTS:
        return [str(v).strip().lower() for v in _as_list(value) if str(v).strip()]
    if key in _PLAIN_LISTS:
        return [str(v).strip() for v in _as_list(value) if str(v).strip()]
    if isinstance(current, bool) or key in ("enabled",):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "y", "1", "on")
    if isinstance(current, str):
        return str(value)
    if value is None or value == "":
        # An explicitly blank number means "no constraint", which is a real
        # answer and different from the field being absent.
        return None
    if key in ("min_signal_count", "min_sqft", "max_sqft",
               "min_year_built", "max_year_built"):
        return int(float(str(value).replace(",", "")))
    return float(str(value).replace(",", "").replace("$", "").strip())


def _as_list(value: Any) -> List[Any]:
    """Accept a list, or a comma-separated string typed into a phone form."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [part for part in str(value).split(",")]
