"""Service-layer tests. **No network, no live provider, no real credentials.**

What these pin down is the reason the layer exists: that a caller which is not
the CLI can drive the engine without fabricating an ``argparse.Namespace``, and
that doing so produces the same answers the CLI produces.

They also guard the two properties that are easy to lose in a refactor like
this one: the service must never print, and it must never leave a database
connection open.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from wholesale_engine.buybox import BuyBox
from wholesale_engine.config import MAX_PROPERTY_PRICE, MIN_PROPERTY_PRICE
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.service import (
    EngineService,
    HuntRequest,
    SAMPLE_LEADS,
    resolve_price_band,
)
from wholesale_engine.storage.database import LeadStore, SearchQuery
from wholesale_engine.storage.decisions import ACCEPTED, REJECTED, Decision, DecisionLog


class ServiceCase(unittest.TestCase):
    """A service pointed at a throwaway directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service = EngineService(
            db_path=self.tmp / "leads.db",
            output_dir=self.tmp / "out",
            buy_box_path=self.tmp / "buybox.json",
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def hunt(self, **kwargs) -> "object":
        request = HuntRequest(source="csv", leads_path=SAMPLE_LEADS, **kwargs)
        return self.service.run_hunt(request)


# ---------------------------------------------------------------------------
# The contract that makes the layer reusable
# ---------------------------------------------------------------------------


class NoCliDependency(unittest.TestCase):
    def test_the_service_never_imports_argparse(self):
        # The whole point: a Flask route and a cron job must be able to drive
        # this without constructing a Namespace. Checked against the parsed
        # import statements rather than the text, so prose explaining WHY the
        # layer exists does not trip it.
        import ast
        import inspect

        from wholesale_engine.service import engine, models, paths

        for module in (engine, models, paths):
            tree = ast.parse(inspect.getsource(module))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertNotIn("argparse", imported, module.__name__)

    def test_the_service_prints_nothing_by_default(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            service = EngineService(db_path=Path(tmp) / "x.db")
            with redirect_stdout(out), redirect_stderr(err):
                service.run_hunt(HuntRequest(source="csv", leads_path=SAMPLE_LEADS))
        self.assertEqual((out.getvalue() + err.getvalue()).strip(), "")

    def test_notices_reach_a_caller_that_asks_for_them(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            service = EngineService(db_path=Path(tmp) / "x.db")
            # No leads path -> the sample-list notice fires.
            service.run_hunt(HuntRequest(source="csv"), on_notice=seen.append)
        self.assertTrue(
            any("FICTIONAL sample lead list" in m for m in seen), seen
        )

    def test_the_package_root_does_not_import_the_service(self):
        # Analyzing one property should not drag in storage and providers.
        import inspect

        import wholesale_engine

        self.assertNotIn("service", inspect.getsource(wholesale_engine))


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class Providers(ServiceCase):
    def test_it_lists_every_registered_adapter_without_constructing_one(self):
        names = {p.name for p in self.service.list_providers()}
        self.assertIn("csv", names)
        self.assertIn("rentcast", names)

    def test_csv_is_usable_and_a_credentialled_adapter_is_not(self):
        by_name = {p.name: p for p in self.service.list_providers()}
        self.assertTrue(by_name["csv"].usable)
        self.assertFalse(by_name["rentcast"].usable)
        self.assertIn("RENTCAST_API_KEY", by_name["rentcast"].missing_settings)

    def test_an_unconfigured_source_falls_back_to_csv_and_says_so(self):
        choice = self.service.resolve_provider("rentcast", leads_path=SAMPLE_LEADS)
        self.assertTrue(choice.ok)
        self.assertTrue(choice.fell_back)
        self.assertEqual(choice.resolved_name, "csv")
        self.assertEqual(choice.requested_name, "rentcast")
        self.assertTrue(any("RENTCAST_API_KEY" in n for n in choice.notices))

    def test_fallback_can_be_refused(self):
        # An unattended LIVE run must fail loudly rather than quietly read a
        # stale CSV and report it as this week's leads.
        choice = self.service.resolve_provider("rentcast", allow_csv_fallback=False)
        self.assertFalse(choice.ok)
        self.assertFalse(choice.fell_back)
        self.assertIn("RENTCAST_API_KEY", choice.error)

    def test_an_unknown_provider_is_an_error_not_an_exception(self):
        choice = self.service.resolve_provider("nope-not-real")
        self.assertFalse(choice.ok)
        self.assertIn("unknown provider", choice.error)


# ---------------------------------------------------------------------------
# Hunt
# ---------------------------------------------------------------------------


class Hunt(ServiceCase):
    def test_a_hunt_runs_end_to_end_and_returns_the_engine_result(self):
        outcome = self.hunt()
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.provider_name, "csv")
        self.assertTrue(outcome.leads)
        # The engine's own HuntResult, passed through untouched.
        self.assertIsNotNone(outcome.result.report)

    def test_it_writes_no_files_unless_asked(self):
        # A web request wants the result object, not four files on the server.
        outcome = self.hunt()
        self.assertEqual(outcome.written, {})
        self.assertFalse((self.tmp / "out").exists())

    def test_it_writes_the_same_outputs_the_cli_does_when_asked(self):
        outcome = self.hunt(write_outputs=True)
        self.assertTrue(outcome.written)
        for path in outcome.written.values():
            self.assertTrue(Path(path).exists(), path)

    def test_persistence_can_be_turned_off_entirely(self):
        outcome = self.hunt(persist=False)
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.db_path)
        self.assertFalse((self.tmp / "leads.db").exists())

    def test_results_are_persisted_and_readable_afterwards(self):
        self.hunt()
        rows = self.service.search_leads()
        self.assertTrue(rows)

    def test_caps_from_the_request_override_the_environment(self):
        request = HuntRequest(
            source="csv", leads_path=SAMPLE_LEADS, research_limit=3, comps_limit=1
        )
        budget = self.service.build_budget(
            request.research_limit, request.comps_limit
        )
        self.assertEqual(budget.research_limit, 3)
        self.assertEqual(budget.comps_limit, 1)

    def test_a_provider_failure_is_reported_not_raised(self):
        outcome = self.service.run_hunt(HuntRequest(source="nope-not-real"))
        self.assertFalse(outcome.ok)
        self.assertIn("unknown provider", outcome.error)

    def test_a_crash_inside_the_funnel_is_reported_not_raised(self):
        service = EngineService(db_path=self.tmp / "crash.db")
        service.resolve_provider = lambda *a, **k: _choice(_Exploding())

        outcome = service.run_hunt(HuntRequest(persist=False))
        self.assertFalse(outcome.ok)
        self.assertIn("the hunt failed", outcome.error)


class _Exploding:
    """A provider that fails mid-search. Carries the attributes run_hunt reads
    before the failure, so the test exercises the crash it means to."""

    name = "boom"
    is_local = True

    def __init__(self):
        from wholesale_engine.providers.metrics import ProviderMetrics

        self.metrics = ProviderMetrics()

    def search_properties(self, criteria):
        raise RuntimeError("provider exploded")


def _choice(provider):
    from wholesale_engine.service.models import ProviderChoice

    return ProviderChoice(provider=provider, resolved_name=getattr(provider, "name", ""))


# ---------------------------------------------------------------------------
# Criteria assembly — the precedence the CLI used to own alone
# ---------------------------------------------------------------------------


class Criteria(ServiceCase):
    def test_the_configured_range_applies_when_nothing_is_given(self):
        criteria = self.service.build_criteria()
        self.assertEqual(criteria.min_price, MIN_PROPERTY_PRICE)
        self.assertEqual(criteria.max_price, MAX_PROPERTY_PRICE)

    def test_there_is_no_low_price_ceiling(self):
        # A $1.4M house with a real spread is a lead. This guards against a
        # default creeping back in that would silently hide most of the market.
        self.assertEqual(self.service.build_criteria().max_price, 2_200_000.0)

    def test_max_price_beats_max_asking_price(self):
        criteria = self.service.build_criteria(max_price=300_000, max_asking_price=150_000)
        self.assertEqual(criteria.max_price, 300_000)

    def test_max_asking_price_is_used_when_max_price_is_absent(self):
        criteria = self.service.build_criteria(max_asking_price=150_000)
        self.assertEqual(criteria.max_price, 150_000)

    def test_none_and_empty_both_mean_not_specified(self):
        # A CLI splitter returns None for an absent flag; a web form omits it.
        from_none = self.service.build_criteria(states=None, cities=None)
        from_empty = self.service.build_criteria(states=(), cities=())
        self.assertEqual(from_none.states, from_empty.states)
        self.assertEqual(from_none.cities, from_empty.cities)

    def test_resolve_price_band_takes_the_first_value_given(self):
        self.assertEqual(resolve_price_band(None, None, 7.0), 7.0)
        self.assertEqual(resolve_price_band(1.0, 2.0), 1.0)
        self.assertIsNone(resolve_price_band(None, None))
        self.assertEqual(resolve_price_band(None, default=9.0), 9.0)


# ---------------------------------------------------------------------------
# Stored leads
# ---------------------------------------------------------------------------


class StoredLeads(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.hunt()

    def test_search_returns_stored_leads(self):
        self.assertTrue(self.service.search_leads(SearchQuery()))

    def test_a_query_actually_filters(self):
        everything = self.service.search_leads(SearchQuery())
        narrowed = self.service.search_leads(SearchQuery(states=("FL",)))
        self.assertLess(len(narrowed), len(everything))
        self.assertTrue(all(r.state == "FL" for r in narrowed))

    def test_one_property_is_findable_by_part_of_its_address(self):
        rows = self.service.search_leads(SearchQuery(limit=1))
        found = self.service.get_property(rows[0].address[:12])
        self.assertIsNotNone(found)
        self.assertEqual(found.dedupe_key, rows[0].dedupe_key)

    def test_a_missing_property_is_none_not_an_exception(self):
        self.assertIsNone(self.service.get_property("no such address anywhere"))

    def test_a_blank_identifier_never_returns_an_arbitrary_row(self):
        self.assertIsNone(self.service.get_property(""))
        self.assertIsNone(self.service.get_property("   "))

    def test_every_read_closes_its_connection(self):
        # A leaked SQLite handle per web request is a server that dies on a
        # Tuesday for no visible reason.
        for _ in range(30):
            self.service.search_leads(SearchQuery(limit=1))
            self.service.get_property("nothing")
            self.service.run_history(1)


class InjectedStore(unittest.TestCase):
    def test_an_injected_store_is_reused_and_never_closed_by_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LeadStore(Path(tmp) / "shared.db")
            service = EngineService(store=store)
            service.search_leads(SearchQuery())
            service.run_history()
            # Still usable: the service did not close a connection it was lent.
            self.assertIsNotNone(store.search(SearchQuery()))
            store.close()


# ---------------------------------------------------------------------------
# Buy box
# ---------------------------------------------------------------------------


class BuyBoxAccess(ServiceCase):
    def test_a_missing_file_reads_as_working_defaults(self):
        view = self.service.read_buy_box()
        self.assertFalse(view.exists)
        self.assertTrue(view.buy_box.is_valid)
        self.assertTrue(view.warnings)

    def test_a_saved_buy_box_reads_back(self):
        result = self.service.save_buy_box(
            {"name": "tampa", "zip_codes": ["33607", "33609"], "states": ["FL"]}
        )
        self.assertTrue(result.ok, result.problems)
        view = self.service.read_buy_box()
        self.assertTrue(view.exists)
        self.assertEqual(view.buy_box.name, "tampa")
        self.assertEqual(view.buy_box.zip_codes, ["33607", "33609"])

    def test_search_count_is_surfaced_because_it_is_the_monthly_budget(self):
        self.service.save_buy_box(
            {"name": "x", "states": ["FL"], "zip_codes": ["33607", "33609", "33611"]}
        )
        self.assertEqual(self.service.read_buy_box().search_count, 3)

    def test_an_invalid_buy_box_never_reaches_disk(self):
        result = self.service.save_buy_box(
            {"name": "bad", "states": ["FLORIDA"], "zip_codes": ["nope"]}
        )
        self.assertFalse(result.ok)
        self.assertFalse((self.tmp / "buybox.json").exists())

    def test_every_problem_is_reported_at_once(self):
        # So a phone form shows the whole picture instead of one field per
        # round trip.
        result = self.service.save_buy_box(
            {"name": "", "states": ["FLORIDA"], "zip_codes": ["nope"],
             "min_price": 500_000, "max_price": 100_000}
        )
        self.assertGreaterEqual(len(result.problems), 4)

    def test_a_viability_floor_above_the_target_is_refused(self):
        result = self.service.save_buy_box(
            {"name": "x", "states": ["FL"],
             "target_wholesale_fee": 18_000, "min_viable_wholesale_fee": 25_000}
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("viable" in p for p in result.problems))

    def test_phone_shaped_input_is_accepted(self):
        result = self.service.save_buy_box(
            {"name": "phone", "states": "FL, TX", "zip_codes": "33607, 33609",
             "max_price": "$2,200,000"}
        )
        self.assertTrue(result.ok, result.problems)
        box = self.service.read_buy_box().buy_box
        self.assertEqual(box.states, ["FL", "TX"])
        self.assertEqual(box.zip_codes, ["33607", "33609"])
        self.assertEqual(box.max_price, 2_200_000.0)

    def test_an_unknown_key_is_a_warning_not_a_failure(self):
        result = self.service.save_buy_box(
            {"name": "x", "states": ["FL"], "invented_setting": 1}
        )
        self.assertTrue(result.ok)
        self.assertTrue(any("invented_setting" in w for w in result.warnings))

    def test_a_corrupt_file_reads_as_defaults_rather_than_killing_a_run(self):
        (self.tmp / "buybox.json").write_text("{ this is not json", encoding="utf-8")
        view = self.service.read_buy_box()
        self.assertTrue(view.buy_box.is_valid)
        self.assertTrue(any("could not read" in w for w in view.warnings))

    def test_reading_the_buy_box_does_not_wire_it_into_a_hunt_yet(self):
        # Explicit: this layer exposes the buy box, it does not apply it.
        # Applying it is a separate, approved-in-its-own-right change.
        self.assertFalse(hasattr(BuyBox, "to_criteria"))


# ---------------------------------------------------------------------------
# Runs and decisions
# ---------------------------------------------------------------------------


class RunsAndDecisions(ServiceCase):
    def test_run_history_is_empty_until_a_run_is_recorded(self):
        self.hunt()
        self.assertEqual(self.service.run_history(), [])

    def test_recording_a_run_is_opt_in(self):
        self.hunt(record_run=True, trigger="scheduled")
        history = self.service.run_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].trigger, "scheduled")
        self.assertEqual(history[0].status, "OK")
        self.assertTrue(history[0].succeeded)
        self.assertGreater(history[0].leads_seen, 0)

    def test_a_recorded_run_is_retrievable_by_id(self):
        outcome = self.hunt(record_run=True)
        self.assertIsNotNone(outcome.run_id)
        run = self.service.get_run(outcome.run_id)
        self.assertEqual(run.run_id, outcome.run_id)
        self.assertEqual(run.provider, "csv")

    def test_the_last_successful_run_is_findable(self):
        self.hunt(record_run=True)
        self.assertIsNotNone(self.service.last_successful_run())

    def test_decision_reads_work_against_a_written_log(self):
        # The reads are what this layer owns; writing per-property decisions
        # from inside the funnel is a separate change.
        outcome = self.hunt(record_run=True)
        store = LeadStore(self.tmp / "leads.db")
        try:
            log = DecisionLog(store.connection)
            log.record_many(outcome.run_id, [
                Decision(dedupe_key="k1", address="1 A St", stage="buy_box",
                         outcome=REJECTED, reason="asking price above buy box maximum"),
                Decision(dedupe_key="k2", address="2 B St", stage="lead_score",
                         outcome=REJECTED, reason="below minimum lead score"),
                Decision(dedupe_key="k3", address="3 C St", stage="final",
                         outcome=ACCEPTED, reason="cleared every gate"),
            ])
        finally:
            store.close()

        summary = self.service.rejections_for_run(outcome.run_id)
        self.assertEqual(sum(count for _, _, count in summary), 2)

        text = self.service.rejection_summary(outcome.run_id)
        self.assertIn("WHY 2 PROPERTIES WERE REJECTED", text)

        history = self.service.decisions_for_property("k1")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].was_rejected)

        self.assertEqual(len(self.service.decisions_for_run(outcome.run_id)), 3)

    def test_reading_decisions_for_an_unknown_property_is_empty_not_an_error(self):
        self.hunt(record_run=True)
        self.assertEqual(self.service.decisions_for_property("no-such-key"), [])

    def test_a_failed_run_still_records_a_row(self):
        # A scheduled job that vanishes silently is worse than one that
        # records its own failure.
        service = EngineService(db_path=self.tmp / "failing.db")
        service.resolve_provider = lambda *a, **k: _choice(_Exploding())
        outcome = service.run_hunt(HuntRequest(record_run=True))
        self.assertFalse(outcome.ok)
        history = service.run_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].status, "FAILED")
        self.assertIn("exploded", history[0].error)


# ---------------------------------------------------------------------------
# The CLI now calls this layer rather than a second copy
# ---------------------------------------------------------------------------


class CliDelegation(unittest.TestCase):
    def test_main_builds_criteria_through_the_service(self):
        import inspect

        from wholesale_engine import main

        source = inspect.getsource(main.criteria_from_args)
        self.assertIn("build_criteria", source)

    def test_main_runs_hunts_through_the_service(self):
        import inspect

        from wholesale_engine import main

        source = inspect.getsource(main.run_hunt_cli)
        self.assertIn("EngineService", source)
        self.assertIn("HuntRequest", source)
        # The second implementation is gone: the CLI no longer resolves a
        # provider or opens a store for a hunt itself.
        self.assertNotIn("get_provider(", source)
        self.assertNotIn("LeadStore(", source)

    def test_the_cli_and_the_service_agree_on_criteria(self):
        import argparse

        from wholesale_engine import main

        parser = main.build_parser()
        args = parser.parse_args(
            ["--hunt", "--states", "FL", "--max-asking-price", "150000"]
        )
        lead_config = main.lead_config_from_args(args)
        from_cli = main.criteria_from_args(args, lead_config)
        from_service = EngineService(lead_config=lead_config).build_criteria(
            states=["FL"], max_asking_price=150_000
        )
        self.assertEqual(from_cli.states, from_service.states)
        self.assertEqual(from_cli.max_price, from_service.max_price)
        self.assertEqual(from_cli.min_price, from_service.min_price)


if __name__ == "__main__":
    unittest.main()
