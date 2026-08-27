"""Web dashboard tests. **Read-only, no server bound, no network.**

"The page loaded" is not the bar. What these check is that the numbers on the
page are the numbers in the database — a dashboard that renders beautifully
and shows a stale or wrong ARV is worse than no dashboard, because you would
act on it.

The other thing held here is the architectural boundary this milestone exists
to prove: the web layer calls the service and does nothing else. No SQL, no
argparse, no scoring, and above all no hunt — loading a page must never spend
an API request.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wholesale_engine.hunt import HuntBudget, run_hunt
from wholesale_engine.providers import CsvProvider
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.service import EngineService
from wholesale_engine.service.paths import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.storage import DecisionLog, LeadStore, SearchQuery
from wholesale_engine.web import create_app
from wholesale_engine.web.formatting import money, percent, score, text
from wholesale_engine.web.app import SAFE_HOSTS, run_dev_server


class Seeded(unittest.TestCase):
    """A real hunt over the bundled fictional list, then the app over its database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "leads.db"
        self.buybox_path = self.tmp / "buybox.json"

        store = LeadStore(self.db)
        run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            HuntCriteria(min_lead_score=50),
            store=store,
            budget=HuntBudget(),
            decisions=DecisionLog(store.connection),
        )
        store.close()

        self.service = EngineService(db_path=self.db, buy_box_path=self.buybox_path)
        self.app = create_app(service=self.service)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def html(self, url: str, expect: int = 200) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, expect, url)
        return response.get_data(as_text=True)

    def a_row(self):
        """A stored lead with real economics on it."""
        rows = self.service.search_leads(SearchQuery(sort_by="priority_score"))
        return next(r for r in rows if r.arv is not None and r.mao is not None)


# ---------------------------------------------------------------------------
# 1. app creation
# ---------------------------------------------------------------------------


class AppCreation(unittest.TestCase):
    def test_the_factory_builds_an_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=Path(tmp) / "x.db")
            self.assertTrue(app.config["ENGINE_SERVICE"])

    def test_a_service_can_be_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = EngineService(db_path=Path(tmp) / "x.db")
            self.assertIs(create_app(service=service).config["ENGINE_SERVICE"], service)

    def test_autoescaping_is_on(self):
        # Addresses and owner names are provider text. Rendering provider text
        # raw is how a lead list becomes a script-injection surface.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(create_app(db_path=Path(tmp) / "x.db").jinja_env.autoescape)

    def test_healthz_touches_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = create_app(db_path=Path(tmp) / "nonexistent.db").test_client()
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertFalse((Path(tmp) / "nonexistent.db").exists())


# ---------------------------------------------------------------------------
# 2-5. every route answers, with the right data on it
# ---------------------------------------------------------------------------


class Routes(Seeded):
    def test_the_lead_list_returns_and_lists_stored_leads(self):
        body = self.html("/leads")
        rows = self.service.search_leads(SearchQuery())
        self.assertTrue(rows)
        self.assertIn(rows[0].address, body)

    def test_the_root_redirects_to_leads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/leads", response.headers["Location"])

    def test_the_property_detail_returns(self):
        row = self.a_row()
        self.assertIn(row.address, self.html(f"/leads/{row.dedupe_key}"))

    def test_run_history_returns(self):
        run = self.service.run_history(1)[0]
        self.assertIn(f"Run {run.run_id}", self.html("/runs"))

    def test_run_detail_and_its_rejection_summary_return(self):
        run = self.service.run_history(1)[0]
        body = self.html(f"/runs/{run.run_id}")
        rejections = self.service.rejections_for_run(run.run_id)
        self.assertTrue(rejections)
        for _, reason, _ in rejections:
            self.assertIn(reason, body)

    def test_the_buy_box_page_returns(self):
        self.assertIn("Buy box", self.html("/buybox"))

    def test_every_page_links_to_the_other_sections(self):
        for url in ("/leads", "/runs", "/buybox"):
            body = self.html(url)
            for target in ("/leads", "/runs", "/buybox"):
                self.assertIn(f'href="{target}"', body, f"{url} -> {target}")


# ---------------------------------------------------------------------------
# THE REGRESSION REQUIREMENT: shown values == stored values
# ---------------------------------------------------------------------------


class ShownValuesMatchStoredValues(Seeded):
    def test_the_list_shows_this_property_s_real_figures(self):
        row = self.a_row()
        body = self.html("/leads")
        for label, rendered in (
            ("address", row.address),
            ("arv", money(row.arv)),
            ("mao", money(row.mao)),
            ("repairs", money(row.repair_estimate)),
            ("offer", money(row.recommended_offer)),
            ("fee", money(row.potential_fee)),
            ("deal score", score(row.deal_score)),
            ("fee status", row.fee_status),
            ("priority band", row.priority_band),
        ):
            self.assertIn(rendered, body, f"{label} missing from the lead list")

    def test_the_detail_page_shows_every_required_field(self):
        row = self.a_row()
        body = self.html(f"/leads/{row.dedupe_key}")
        for label, rendered in (
            ("lead score", score(row.lead_score)),
            ("deal score", score(row.deal_score)),
            ("priority", score(row.priority_score)),
            ("arv", money(row.arv)),
            ("repairs", money(row.repair_estimate)),
            ("mao", money(row.mao)),
            ("offer", money(row.recommended_offer)),
            ("fee", money(row.potential_fee)),
            ("fee status", row.fee_status),
            ("equity amount", money(row.equity_amount)),
            ("equity status", row.equity_status),
            ("arv confidence", row.arv_confidence),
            ("comp confidence", row.comp_confidence),
            ("status", row.status),
        ):
            self.assertTrue(rendered, f"{label} was blank in the fixture")
            self.assertIn(rendered, body, f"{label} missing from the detail page")

    def test_the_detail_page_shows_the_recorded_decision_history(self):
        row = self.a_row()
        decisions = self.service.decisions_for_property(row.dedupe_key)
        self.assertTrue(decisions, "the seeded hunt should have recorded decisions")
        body = self.html(f"/leads/{row.dedupe_key}")
        for decision in decisions:
            self.assertIn(decision.outcome, body)
            self.assertIn(decision.reason, body)

    def test_run_counters_on_the_page_match_the_run_record(self):
        run = self.service.run_history(1)[0]
        counts = self.service.run_outcome_counts(run.run_id)
        body = self.html(f"/runs/{run.run_id}")
        self.assertIn(str(run.leads_seen), body)
        self.assertIn(run.status, body)
        # Accepted, rejected AND incomplete — the third is why the service
        # grew run_outcome_counts, since the runs table has no column for it.
        for key in ("ACCEPTED", "REJECTED", "INCOMPLETE"):
            self.assertGreater(counts.get(key, 0), 0, key)
            self.assertIn(str(counts[key]), body)

    def test_equity_percentage_is_rendered_as_a_ratio_not_as_a_bare_number(self):
        # equity_percentage is stored 0.0-1.0. Formatting it directly showed
        # 68% equity as "1%", which understates a high-equity lead a hundred
        # times over — the kind of number someone passes on a deal over.
        from wholesale_engine.web.formatting import ratio

        row = next(
            r for r in self.service.search_leads(SearchQuery())
            if r.equity_percentage
        )
        self.assertLessEqual(row.equity_percentage, 1.0)
        body = self.html(f"/leads/{row.dedupe_key}")
        self.assertIn(ratio(row.equity_percentage), body)
        self.assertEqual(ratio(0.6767), "68%")
        self.assertEqual(ratio(None), "—")

    def test_the_web_and_the_deal_room_agree_on_equity(self):
        from wholesale_engine.web.formatting import ratio

        row = next(
            r for r in self.service.search_leads(SearchQuery())
            if r.equity_percentage
        )
        deal_room = f"{row.equity_percentage * 100:.0f}%"
        self.assertEqual(ratio(row.equity_percentage), deal_room)

    def test_an_unknown_number_renders_as_a_dash_not_as_zero(self):
        # A blank ARV shown as $0 reads as a worthless property rather than an
        # unvalued one, which is the difference between passing and not calling.
        self.assertEqual(money(None), "—")
        self.assertEqual(score(None), "—")
        self.assertEqual(percent(None), "—")
        self.assertEqual(percent(68), "68%")   # already out of a hundred
        self.assertEqual(text(""), "—")
        self.assertEqual(money(0), "$0")
        self.assertEqual(score(0), "0")

    def test_a_negative_fee_keeps_the_sign_outside_the_symbol(self):
        self.assertEqual(money(-18200), "-$18,200")


# ---------------------------------------------------------------------------
# Server-side filtering, through SearchQuery
# ---------------------------------------------------------------------------


class Filtering(Seeded):
    def test_a_state_filter_narrows_the_list(self):
        everything = self.html("/leads")
        florida = self.html("/leads?states=FL")
        texas_row = next(
            (r for r in self.service.search_leads(SearchQuery()) if r.state == "TX"), None
        )
        self.assertIsNotNone(texas_row)
        self.assertIn(texas_row.address, everything)
        self.assertNotIn(texas_row.address, florida)

    def test_the_filter_agrees_with_the_service(self):
        body = self.html("/leads?states=FL")
        for row in self.service.search_leads(SearchQuery(states=("FL",))):
            self.assertIn(row.address, body)

    def test_a_malformed_filter_is_ignored_rather_than_a_500(self):
        # The URL bar is where a phone user edits filters. A typo should widen
        # the search, not break the page.
        self.assertEqual(self.client.get("/leads?min_fee=abc").status_code, 200)
        self.assertEqual(self.client.get("/leads?limit=notanumber").status_code, 200)
        self.assertEqual(self.client.get("/leads?sort_by=;DROP TABLE").status_code, 200)

    def test_an_unknown_sort_key_falls_back_rather_than_reaching_sql(self):
        from wholesale_engine.web.app import build_query

        with self.app.test_request_context("/leads?sort_by=evil"):
            self.assertEqual(build_query().sort_by, "priority_score")

    def test_the_row_limit_is_bounded(self):
        from wholesale_engine.web.app import MAX_LIMIT, build_query

        with self.app.test_request_context(f"/leads?limit={MAX_LIMIT * 100}"):
            self.assertEqual(build_query().limit, MAX_LIMIT)
        with self.app.test_request_context("/leads?limit=0"):
            self.assertEqual(build_query().limit, 1)


# ---------------------------------------------------------------------------
# 6. the buy box page must not imply an inert setting is filtering
# ---------------------------------------------------------------------------


class BuyBoxPage(Seeded):
    def write_box(self, **values) -> None:
        self.buybox_path.write_text(
            json.dumps({"name": "tampa", "states": ["FL"], **values}), encoding="utf-8"
        )

    def test_applied_settings_are_shown_as_active(self):
        self.write_box(zip_codes=["33607"], min_lead_score=70)
        body = self.html("/buybox")
        self.assertIn("Active", body)
        self.assertIn("33607", body)

    def test_the_seven_shape_filters_are_shown_as_not_filtering(self):
        from wholesale_engine.buybox import NOT_IMPLEMENTED_FIELDS

        self.write_box(min_beds=3, max_beds=5, min_baths=2, min_sqft=900,
                       max_sqft=3000, min_year_built=1950, max_year_built=2020)
        body = self.html("/buybox")
        self.assertIn("Saved but NOT filtering", body)
        for name in NOT_IMPLEMENTED_FIELDS:
            self.assertIn(name.replace("_", " "), body, name)
        # Their values appear, but marked as ignored — never as a live filter.
        self.assertIn("ignored", body)

    def test_the_three_unrouted_settings_are_shown_as_not_filtering(self):
        self.write_box(min_signal_count=2, target_wholesale_fee=25_000,
                       min_viable_wholesale_fee=5_000)
        body = self.html("/buybox")
        for name in ("min signal count", "target wholesale fee",
                     "min viable wholesale fee"):
            self.assertIn(name, body)
        self.assertIn("not from here", body)

    def test_an_inert_setting_never_appears_inside_the_active_section(self):
        # The check that actually matters: the split has to hold in the markup,
        # not just in a heading somewhere on the page.
        from wholesale_engine.buybox import NOT_IMPLEMENTED_FIELDS, NOT_ROUTED_FIELDS

        self.write_box(min_beds=3, target_wholesale_fee=25_000, zip_codes=["33607"])
        body = self.html("/buybox")
        active = body.split('class="group active"')[1].split("</section>")[0]
        self.assertIn("33607", active)
        for name in tuple(NOT_IMPLEMENTED_FIELDS) + tuple(NOT_ROUTED_FIELDS):
            self.assertNotIn(name.replace("_", " "), active, name)

    def test_the_warnings_from_the_buy_box_reach_the_page(self):
        self.write_box(min_beds=3)
        self.assertIn("NOT APPLIED", self.html("/buybox"))

    def test_a_missing_buy_box_renders_defaults_rather_than_failing(self):
        body = self.html("/buybox")
        self.assertIn("using defaults", body)

    def test_a_corrupt_buy_box_renders_rather_than_500(self):
        self.buybox_path.write_text("{ not json", encoding="utf-8")
        self.assertIn("could not read", self.html("/buybox"))


# ---------------------------------------------------------------------------
# 7-9. the architectural boundary
# ---------------------------------------------------------------------------


class ArchitecturalBoundary(Seeded):
    WEB_MODULES = ("wholesale_engine.web.app", "wholesale_engine.web.formatting",
                   "wholesale_engine.web.__init__")

    def _imports(self, module_name: str) -> set:
        import importlib

        module = importlib.import_module(module_name)
        tree = ast.parse(inspect.getsource(module))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    def test_the_web_layer_never_imports_argparse(self):
        for name in self.WEB_MODULES:
            self.assertNotIn("argparse", self._imports(name), name)

    def test_the_web_layer_never_imports_sqlite_or_the_cli(self):
        for name in self.WEB_MODULES:
            imports = self._imports(name)
            self.assertNotIn("sqlite3", imports, name)
            self.assertNotIn("subprocess", imports, name)
            self.assertNotIn("main", imports, name)

    def test_no_route_writes_sql(self):
        source = inspect.getsource(__import__(
            "wholesale_engine.web.app", fromlist=["app"]
        ))
        for fragment in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", ".execute("):
            self.assertNotIn(fragment, source, fragment)

    def test_no_route_reimplements_scoring_or_filtering(self):
        source = inspect.getsource(__import__(
            "wholesale_engine.web.app", fromlist=["app"]
        ))
        for fragment in ("score_lead", "analyze_property", "cheap_filter",
                         "apply_filters", "maximum_allowable_offer"):
            self.assertNotIn(fragment, source, fragment)

    def test_loading_a_page_never_runs_a_hunt(self):
        # The one that would actually cost money: a page that hunts on load
        # spends the monthly API budget on a refresh.
        with mock.patch.object(
            EngineService, "run_hunt", side_effect=AssertionError("a page ran a hunt")
        ):
            for url in ("/", "/leads", "/runs", "/buybox", "/healthz",
                        f"/leads/{self.a_row().dedupe_key}",
                        f"/runs/{self.service.run_history(1)[0].run_id}"):
                self.client.get(url)

    def test_every_route_is_read_only(self):
        # No POST, PUT, PATCH or DELETE exists anywhere in the app.
        for rule in self.app.url_map.iter_rules():
            self.assertEqual(
                rule.methods & {"POST", "PUT", "PATCH", "DELETE"}, set(), str(rule)
            )

    def test_the_routes_go_through_the_service(self):
        with mock.patch.object(
            EngineService, "search_leads", return_value=[]
        ) as spy:
            self.client.get("/leads")
        spy.assert_called_once()

    def test_the_detail_route_uses_get_property_and_decisions_for_property(self):
        row = self.a_row()
        with mock.patch.object(
            EngineService, "get_property", wraps=self.service.get_property
        ) as get, mock.patch.object(
            EngineService, "decisions_for_property",
            wraps=self.service.decisions_for_property,
        ) as decisions:
            self.client.get(f"/leads/{row.dedupe_key}")
        get.assert_called_once()
        decisions.assert_called_once()


# ---------------------------------------------------------------------------
# 8. missing things 404 rather than crashing
# ---------------------------------------------------------------------------


class NotFound(Seeded):
    def test_an_unknown_property_is_a_404(self):
        response = self.client.get("/leads/no-such-property-anywhere")
        self.assertEqual(response.status_code, 404)
        self.assertIn("No stored property", response.get_data(as_text=True))

    def test_an_unknown_run_is_a_404(self):
        response = self.client.get("/runs/999999")
        self.assertEqual(response.status_code, 404)

    def test_a_non_numeric_run_id_is_a_404_not_a_500(self):
        self.assertEqual(self.client.get("/runs/abc").status_code, 404)

    def test_an_odd_property_key_does_not_crash(self):
        for key in ("../../etc/passwd", "'; DROP TABLE leads;--", "%00", "a" * 500):
            self.assertIn(
                self.client.get(f"/leads/{key}").status_code, (404, 200), key
            )

    def test_an_empty_database_renders_empty_pages_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = create_app(db_path=Path(tmp) / "empty.db").test_client()
            for url in ("/leads", "/runs", "/buybox"):
                self.assertEqual(client.get(url).status_code, 200, url)
            self.assertIn("Nothing stored yet", client.get("/leads").get_data(as_text=True))


# ---------------------------------------------------------------------------
# Mobile and security posture
# ---------------------------------------------------------------------------


class MobileAndSecurity(Seeded):
    def test_every_page_declares_a_mobile_viewport(self):
        for url in ("/leads", "/runs", "/buybox"):
            self.assertIn("width=device-width", self.html(url))

    def test_no_frontend_framework_or_external_asset_is_pulled_in(self):
        body = self.html("/leads")
        self.assertNotIn("<script", body)
        self.assertNotIn("http://", body.replace("http://www.w3.org", ""))
        self.assertNotIn("cdn", body.lower())

    def test_the_stylesheet_is_served_locally(self):
        response = self.client.get("/static/style.css")
        try:
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()

    def test_tap_targets_and_ios_zoom_are_handled(self):
        response = self.client.get("/static/style.css")
        try:
            css = response.get_data(as_text=True)
        finally:
            response.close()
        self.assertIn("--tap: 44px", css)
        # 16px inputs are what stop iOS zooming the page on focus.
        self.assertIn("font-size: 16px", css)

    def test_a_public_bind_is_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.10"):
            with self.assertRaises(ValueError, msg=host):
                run_dev_server(host=host)

    def test_only_loopback_addresses_are_permitted(self):
        self.assertEqual(set(SAFE_HOSTS), {"127.0.0.1", "localhost", "::1"})

    def test_the_lack_of_authentication_is_stated_on_every_page(self):
        for url in ("/leads", "/runs", "/buybox"):
            self.assertIn("No authentication", self.html(url))

    def test_the_module_documents_the_exposure_risk(self):
        import wholesale_engine.web as web

        self.assertIn("MUST NOT BE EXPOSED", web.__doc__)


# ---------------------------------------------------------------------------
# 10. nothing else changed
# ---------------------------------------------------------------------------


class ExistingBehaviourUnchanged(unittest.TestCase):
    def test_the_cli_does_not_import_flask(self):
        # The engine stays stdlib-only; Flask is the web layer's dependency
        # alone, so a VPS running only scheduled hunts needs nothing installed.
        from wholesale_engine import main

        tree = ast.parse(inspect.getsource(main))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "flask")
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotEqual(node.module.split(".")[0], "flask")

    def test_the_engine_package_does_not_import_the_web_layer(self):
        import wholesale_engine

        self.assertNotIn("web", inspect.getsource(wholesale_engine))

    def test_flask_is_declared_in_requirements(self):
        from wholesale_engine.service.paths import PACKAGE_ROOT

        requirements = (PACKAGE_ROOT / "requirements.txt").read_text()
        self.assertIn("Flask>=3.0,<4.0", requirements)


if __name__ == "__main__":
    unittest.main()
