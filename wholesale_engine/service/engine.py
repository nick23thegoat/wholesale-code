"""The orchestration layer: everything the CLI does, minus the CLI.

Before this existed, "run a hunt" meant `run_hunt_cli(args, config)` — provider
resolution, credential fallback, sample-path defaults, budget assembly, store
lifecycle and output writing, all reachable only by constructing an
``argparse.Namespace``. A scheduled job or a web request had two bad options:
fabricate a fake Namespace, or reimplement the sequence and let the two copies
drift. This module is that sequence, callable by anything.

What it is not
--------------

It is **not** a second engine. Every decision about what a property is worth
still happens in :mod:`wholesale_engine.analysis`, every filter in
:mod:`wholesale_engine.hunt`, every query in
:mod:`wholesale_engine.storage.database`. This layer chooses inputs, manages
lifecycles and reports outcomes. If you find deal logic in this file, it is in
the wrong file.

It also holds no opinion about output. Nothing here prints. Callers that want
progress as it happens pass ``on_notice``; the CLI passes a stderr printer and
so keeps its exact message ordering, while a web request collects the same
strings as data.

Store lifecycle
---------------

Each operation opens the database, uses it, and closes it, unless a store is
injected. That is the behaviour a Flask worker needs — a SQLite connection is
not safe to share across threads — and it is what the CLI already did per
command. Injecting a store is for tests and for a caller batching several
operations under one connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from ..budget import ApiBudget
from ..buybox import BuyBox, config_path as buybox_config_path
from ..config import (
    DEFAULT_CONFIG,
    DEFAULT_LEAD_CONFIG,
    MAX_PROPERTY_PRICE,
    MIN_PROPERTY_PRICE,
    EngineConfig,
    LeadHunterConfig,
)
from ..hunt import HuntBudget, run_hunt as _run_hunt
from ..providers.base import ProviderNotConfigured
from ..providers.criteria import HuntCriteria
from ..providers.registry import (
    get_provider,
    registered_names,
    registration,
)
from ..reports.hunt_report import write_hunt_outputs
from ..settings import NO_PROVIDER_MESSAGE, ProviderSettings
from ..storage.database import DEFAULT_DB_PATH, LeadStore, SearchQuery, StoredLead
from ..storage.decisions import ACCEPTED, Decision, DecisionLog, RunRecord
from .models import (
    BuyBoxView,
    HuntOutcome,
    HuntRequest,
    ProviderChoice,
    ProviderStatus,
    SaveResult,
)
from .paths import DEFAULT_OUTPUT_DIR, SAMPLE_LEAD_COMPS, SAMPLE_LEADS

if TYPE_CHECKING:  # imported lazily at runtime, so the acquisitions stack
    from ..acquisitions.models import Buyer  # is not loaded for a plain hunt

#: A callback for progress messages. The CLI prints them; a web request
#: collects them. Default is silence, which is what a library should do.
Notice = Callable[[str], None]


def _silent(message: str) -> None:
    return None


def resolve_price_band(
    *values: Optional[float], default: Optional[float] = None
) -> Optional[float]:
    """The first bound that was actually given, else ``default``.

    Extracted verbatim from the CLI's ``_first_set``. The precedence matters:
    an explicit ``--min-price``/``--max-price`` wins, then the older
    ``--max-asking-price``, then the configured search range. That range is a
    BUYER-CAPACITY ceiling, not a deal rule — everything inside it is still
    underwritten normally.
    """
    for value in values:
        if value is not None:
            return value
    return default


class EngineService:
    """Programmatic access to the engine. No argparse, no printing, no globals."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        settings: Optional[ProviderSettings] = None,
        engine_config: EngineConfig = DEFAULT_CONFIG,
        lead_config: LeadHunterConfig = DEFAULT_LEAD_CONFIG,
        buy_box_path: Optional[Path] = None,
        store: Optional[LeadStore] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        self.engine_config = engine_config
        self.lead_config = lead_config
        self._settings = settings
        self._buy_box_path = Path(buy_box_path) if buy_box_path else None
        #: An injected store is owned by the caller and never closed here.
        self._store = store

    # ------------------------------------------------------------------
    # Lifecycles
    # ------------------------------------------------------------------

    @property
    def settings(self) -> ProviderSettings:
        """Credentials, read once per service instance."""
        if self._settings is None:
            self._settings = ProviderSettings.from_env()
        return self._settings

    def _open_store(self) -> tuple:
        """``(store, should_close)``. An injected store is never closed here."""
        return self._open_store_at(self.db_path)

    def _open_store_at(self, db_path: Path) -> tuple:
        """``(store, should_close)`` for one path.

        An injected store wins regardless of path: a caller that handed us a
        connection owns it, and opening a second one behind their back is how
        two writers end up on one SQLite file.
        """
        if self._store is not None:
            return self._store, False
        return LeadStore(db_path), True

    def buy_box_path(self) -> Path:
        return self._buy_box_path or buybox_config_path()

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------

    def list_providers(self) -> List[ProviderStatus]:
        """Every registered adapter and whether it is usable right now.

        Reads the registry; constructs nothing, so it costs no credentials
        and makes no network call.
        """
        statuses: List[ProviderStatus] = []
        for name in registered_names():
            entry = registration(name)
            if entry is None:
                continue
            statuses.append(
                ProviderStatus(
                    name=entry.name,
                    description=entry.description,
                    is_local=entry.is_local,
                    configured=entry.is_configured(self.settings),
                    missing_settings=entry.missing_settings(self.settings),
                    capabilities=[str(c) for c in entry.capabilities],
                    documentation=entry.documentation,
                )
            )
        return statuses

    def resolve_provider(
        self,
        source: str = "csv",
        leads_path: Optional[Path] = None,
        comps_path: Optional[Path] = None,
        allow_csv_fallback: bool = True,
        on_notice: Optional[Notice] = None,
    ) -> ProviderChoice:
        """Build the named adapter, falling back to CSV when it cannot be.

        The fallback is the CLI's long-standing behaviour and is preserved
        exactly, including the wording: each adapter declares the variables it
        needs, so the message names the right ones rather than a generic pair.

        ``allow_csv_fallback=False`` turns the fallback off, which is what an
        unattended LIVE run wants — quietly reading yesterday's CSV file is a
        worse failure than stopping.
        """
        say = on_notice or _silent
        requested = (source or "csv").strip().lower()
        choice = ProviderChoice(requested_name=requested, resolved_name=requested)

        entry = registration(requested)
        missing = entry.missing_settings(self.settings) if entry else []
        if entry is not None and missing:
            detail = (
                f"  '{requested}' needs {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )
            if not allow_csv_fallback:
                choice.error = f"{requested} is NOT CONNECTED: {', '.join(missing)} not set."
                choice.notices.extend([NO_PROVIDER_MESSAGE, detail])
                return choice
            say(NO_PROVIDER_MESSAGE)
            say(detail)
            say("  Falling back to the local CSV source for this run.")
            choice.notices.extend([
                NO_PROVIDER_MESSAGE, detail,
                "  Falling back to the local CSV source for this run.",
            ])
            choice.fell_back = True
            requested = "csv"
            choice.resolved_name = requested

        # Resolved after the fallback, so an unconfigured live source lands on
        # a working CSV run rather than an error about a missing file.
        resolved_leads = leads_path or (SAMPLE_LEADS if requested == "csv" else None)
        resolved_comps = comps_path or (
            SAMPLE_LEAD_COMPS if resolved_leads == SAMPLE_LEADS else None
        )
        if requested == "csv" and resolved_leads == SAMPLE_LEADS and not leads_path:
            sample_notice = (
                "No --leads file given; hunting the bundled FICTIONAL sample lead list."
            )
            say(sample_notice)
            choice.notices.append(sample_notice)

        try:
            choice.provider = get_provider(
                requested,
                settings=self.settings,
                csv_path=resolved_leads,
                comps_path=resolved_comps,
            )
        except ProviderNotConfigured as exc:
            choice.error = str(exc)
        return choice

    # ------------------------------------------------------------------
    # Hunt
    # ------------------------------------------------------------------

    def build_criteria(
        self,
        states: Optional[Sequence[str]] = None,
        counties: Optional[Sequence[str]] = None,
        cities: Optional[Sequence[str]] = None,
        zip_codes: Optional[Sequence[str]] = None,
        property_types: Optional[Sequence[str]] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        max_asking_price: Optional[float] = None,
        min_equity: Optional[float] = None,
        required_signals: Optional[Sequence[str]] = None,
        min_lead_score: Optional[float] = None,
        min_deal_score: Optional[float] = None,
        limit: Optional[int] = None,
        buy_box: Optional[BuyBox] = None,
    ) -> HuntCriteria:
        """Assemble criteria from plain values, applying the same defaults.

        The price band resolution is the part worth centralising: three
        different flags can set the ceiling and they have a defined
        precedence. Anything building criteria — CLI, scheduler, web form —
        gets the same answer from here.

        Every sequence accepts ``None`` as well as an empty one, and both mean
        "not specified". A CLI splitter that returns ``None`` for an absent
        flag and a web form that omits the field should not need to know the
        difference.

        ``buy_box`` supplies a default for anything not specified here. The
        precedence is one rule applied uniformly: **an explicit value wins,
        the buy box fills the rest, and the engine's own defaults fill what is
        left.** So a saved buy box is the standing configuration and a flag is
        a deliberate override of it for one run, which is the only ordering
        that makes a scheduled run and a hand-typed one predictable.

        This is the single place that ordering exists. The CLI does not
        re-implement it and neither should anything else.
        """
        source = buy_box.to_criteria() if buy_box is not None else None

        def pick(explicit, from_box):
            """Explicit value, else the buy box's, else nothing."""
            if explicit is not None:
                return explicit
            return from_box

        def pick_seq(explicit, from_box):
            """Same rule for sequences, where empty also means unspecified."""
            if explicit:
                return tuple(explicit)
            return tuple(from_box or ())

        return HuntCriteria(
            states=(
                pick_seq(states, source.states if source else ())
                or self.lead_config.target_states
            ),
            counties=pick_seq(counties, source.counties if source else ()),
            cities=pick_seq(cities, source.cities if source else ()),
            zip_codes=pick_seq(zip_codes, source.zip_codes if source else ()),
            property_types=(
                pick_seq(property_types, source.property_types if source else ())
                or self.lead_config.preferred_property_types
            ),
            min_price=resolve_price_band(
                min_price,
                source.min_price if source else None,
                default=MIN_PROPERTY_PRICE,
            ),
            max_price=resolve_price_band(
                max_price,
                max_asking_price,
                source.max_price if source else None,
                default=MAX_PROPERTY_PRICE,
            ),
            min_equity=pick(min_equity, source.min_equity if source else None),
            required_signals=pick_seq(
                required_signals, source.required_signals if source else ()
            ),
            min_lead_score=pick(
                min_lead_score, source.min_lead_score if source else None
            ) or 0.0,
            min_deal_score=pick(
                min_deal_score, source.min_deal_score if source else None
            ) or 0.0,
            limit=limit,
        )

    def build_budget(
        self,
        research_limit: Optional[int] = None,
        comps_limit: Optional[int] = None,
    ) -> HuntBudget:
        """Environment caps, with explicit overrides on top."""
        budget = HuntBudget.from_api_budget(ApiBudget.from_env())
        if research_limit is not None:
            budget.research_limit = research_limit
        if comps_limit is not None:
            budget.comps_limit = comps_limit
        return budget

    def run_hunt(
        self,
        request: Optional[HuntRequest] = None,
        on_notice: Optional[Notice] = None,
    ) -> HuntOutcome:
        """The full funnel: resolve a provider, search, score, persist, report.

        Never raises for an expected failure. An unusable provider, a missing
        credential or a search the vendor refused all come back as an
        :class:`HuntOutcome` with ``error`` set — a scheduled job at 3am must
        record why it did nothing rather than dying with a traceback.
        """
        request = request or HuntRequest()
        say = on_notice or _silent
        outcome = HuntOutcome()

        choice = self.resolve_provider(
            request.source,
            request.leads_path,
            request.comps_path,
            allow_csv_fallback=request.allow_csv_fallback,
            on_notice=say,
        )
        outcome.notices.extend(choice.notices)
        outcome.provider_name = choice.resolved_name
        outcome.fell_back = choice.fell_back
        if not choice.ok:
            outcome.error = choice.error
            return outcome

        criteria = request.criteria or self.build_criteria()
        budget = request.budget or self.build_budget(
            request.research_limit, request.comps_limit
        )

        db_path = Path(request.db_path) if request.db_path else self.db_path
        store: Optional[LeadStore] = None
        should_close = False
        log: Optional[DecisionLog] = None
        run: Optional[RunRecord] = None

        if request.persist:
            store, should_close = self._open_store_at(db_path)
            outcome.db_path = db_path
            if request.record_run:
                log = DecisionLog(store.connection)
                run = log.start_run(
                    trigger=request.trigger,
                    provider=choice.resolved_name,
                    mode=request.mode,
                )
                outcome.run_id = run.run_id

        # One try/finally owns the store for the whole operation. Splitting it
        # around the output write is how a connection gets closed twice, or
        # leaked when the write raises.
        try:
            try:
                result = _run_hunt(
                    choice.provider,
                    criteria,
                    engine_config=self.engine_config,
                    lead_config=self.lead_config,
                    budget=budget,
                    store=store,
                    # The service opened this run, so it passes the id in and
                    # closes it below. run_hunt records the per-property
                    # decisions against it rather than opening a second one.
                    decisions=log,
                    run_id=run.run_id if run is not None else None,
                )
            except Exception as exc:  # noqa: BLE001 - a run reports, never vanishes
                outcome.error = f"the hunt failed: {exc}"
                if log is not None and run is not None:
                    log.finish_run(run, status="FAILED", error=str(exc))
                return outcome

            outcome.result = result
            outcome.notices.extend(result.warnings)

            if log is not None and run is not None:
                # Counted from the decisions actually written, not guessed at
                # from the result: those two disagreeing is exactly the kind
                # of thing a run history is supposed to settle.
                recorded = log.for_run(run.run_id)
                log.finish_run(
                    run,
                    status="OK",
                    leads_seen=len(recorded),
                    leads_accepted=sum(1 for d in recorded if d.outcome == ACCEPTED),
                    leads_rejected=sum(1 for d in recorded if d.was_rejected),
                    api_requests_spent=(
                        result.usage.research_calls + result.usage.comp_calls
                    ),
                )

            if request.write_outputs:
                # Writing is last: a failure here loses four files, not the run.
                outcome.written = write_hunt_outputs(
                    result,
                    Path(request.output_dir) if request.output_dir else self.output_dir,
                    write_json=request.write_json,
                )
            return outcome
        finally:
            if store is not None and should_close:
                store.close()

    # ------------------------------------------------------------------
    # Stored leads
    # ------------------------------------------------------------------

    def search_leads(self, query: Optional[SearchQuery] = None) -> List[StoredLead]:
        """Filter the stored leads. The query object is the storage layer's own."""
        store, should_close = self._open_store()
        try:
            return store.search(query or SearchQuery())
        finally:
            if should_close:
                store.close()

    def get_property(self, identifier: str) -> Optional[StoredLead]:
        """One stored lead by lead id, or by part of its address."""
        if not (identifier or "").strip():
            return None
        store, should_close = self._open_store()
        try:
            return store.find_one(identifier)
        finally:
            if should_close:
                store.close()

    def recent_activity(self, limit: int = 50) -> List[Dict[str, Any]]:
        store, should_close = self._open_store()
        try:
            return store.recent_activity(limit)
        finally:
            if should_close:
                store.close()

    # ------------------------------------------------------------------
    # Buyers
    # ------------------------------------------------------------------

    def matching_buyers_for_property(self, row: StoredLead) -> List["Buyer"]:
        """End buyers whose buy box fits this property.

        Delegates entirely to :meth:`AcquisitionStore.matching_buyers`, which
        delegates to :meth:`Buyer.matches`. No rule is evaluated here and none
        is duplicated: this method exists so a web route and the deal room can
        both ask the question without either one reaching for the database.

        The price passed is ``recommended_offer``, which is what the existing
        caller in ``automation/daily_priority`` passes. Matching semantics are
        deliberately not changed by surfacing them somewhere new.

        A property with no recommended offer yet still matches on state and
        type — :meth:`Buyer.matches` treats an unknown attribute as "does not
        rule anyone out", because the point is to shortlist people to call,
        not to filter them away on a blank field.
        """
        from ..acquisitions.store import AcquisitionStore

        if row is None:
            return []
        store, should_close = self._open_store()
        try:
            return AcquisitionStore(store).matching_buyers(
                state=row.state or "",
                property_type=row.property_type or "",
                price=row.recommended_offer,
            )
        finally:
            if should_close:
                store.close()

    def all_buyers(self) -> List["Buyer"]:
        """Every buyer on file, for "no matches — is anyone on file at all?"."""
        from ..acquisitions.store import AcquisitionStore

        store, should_close = self._open_store()
        try:
            return AcquisitionStore(store).all_buyers()
        finally:
            if should_close:
                store.close()

    # ------------------------------------------------------------------
    # Buy box
    # ------------------------------------------------------------------

    def read_buy_box(self, path: Optional[Path] = None) -> BuyBoxView:
        """The buy box as it is on disk. Never raises.

        A missing file is not an error: it means "use the defaults", which is
        a working configuration. An unreadable one is reported as a warning
        and the defaults stand in, because a scheduled run must not die
        because a field was edited badly from a phone.
        """
        target = Path(path) if path else self.buy_box_path()
        box, warnings = BuyBox.load(target)
        return BuyBoxView(
            buy_box=box, path=target, warnings=warnings, exists=target.exists(),
            unsupported=box.unsupported_settings(),
        )

    def save_buy_box(
        self, values: Dict[str, Any], path: Optional[Path] = None
    ) -> SaveResult:
        """Validate and write a buy box. Returns every problem at once.

        Nothing reaches disk unless it is valid — an invalid buy box saved now
        is a scheduled run that fails at 3am. Unknown keys are warnings rather
        than failures, so a config written by a newer version of the engine
        does not lock an older one out.
        """
        target = Path(path) if path else self.buy_box_path()
        box, warnings = BuyBox.from_dict(values or {})
        problems = box.validate()
        if problems:
            return SaveResult(
                saved=False, path=target, problems=problems,
                warnings=warnings, buy_box=box,
            )
        try:
            written = box.save(target)
        except (OSError, ValueError) as exc:
            return SaveResult(
                saved=False, path=target, problems=[str(exc)],
                warnings=warnings, buy_box=box,
            )
        return SaveResult(
            saved=True, path=written, warnings=warnings, buy_box=box
        )

    # ------------------------------------------------------------------
    # Runs and decisions
    # ------------------------------------------------------------------

    def run_history(self, limit: int = 20) -> List[RunRecord]:
        """Recent runs, newest first."""
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).recent_runs(limit)
        finally:
            if should_close:
                store.close()

    def get_run(self, run_id: int) -> Optional[RunRecord]:
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).get_run(run_id)
        finally:
            if should_close:
                store.close()

    def last_successful_run(self) -> Optional[RunRecord]:
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).last_successful_run()
        finally:
            if should_close:
                store.close()

    def rejections_for_run(self, run_id: int) -> List[tuple]:
        """``(stage, reason, count)`` for one run, commonest first.

        The view that says which rule is actually doing the throwing away,
        which is what you tune a buy box against.
        """
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).rejection_summary(run_id)
        finally:
            if should_close:
                store.close()

    def rejection_summary(self, run_id: int) -> str:
        """The rejection breakdown as text."""
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).render_summary(run_id)
        finally:
            if should_close:
                store.close()

    def run_outcome_counts(self, run_id: int) -> Dict[str, int]:
        """``{outcome: count}`` for one run — ACCEPTED, REJECTED, INCOMPLETE.

        The ``runs`` table carries accepted and rejected but not incomplete,
        and "analyzed but missing data" is a different answer from either. It
        is grouped in SQL so the number stays right on a run larger than one
        page of decisions.
        """
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).outcome_counts(run_id)
        finally:
            if should_close:
                store.close()

    def decisions_for_run(self, run_id: int, limit: int = 500) -> List[Decision]:
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).for_run(run_id, limit=limit)
        finally:
            if should_close:
                store.close()

    def decisions_for_property(
        self, dedupe_key: str, limit: int = 100
    ) -> List[Decision]:
        """Every decision ever made about one property, newest first.

        This is the "why is this not in my list?" answer, and it spans runs —
        a property rejected three weeks running for the same reason is a buy
        box problem, not a property problem.
        """
        store, should_close = self._open_store()
        try:
            return DecisionLog(store.connection).for_property(dedupe_key, limit=limit)
        finally:
            if should_close:
                store.close()

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close an injected store, if this service was given one to own."""
        if self._store is not None:
            self._store.close()
            self._store = None
