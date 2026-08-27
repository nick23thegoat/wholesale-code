"""Buyer matching, surfaced. **The matching rules themselves are untouched.**

Everything here checks that an answer the engine could already compute now
reaches the two screens where you would act on it — the deal room and the
property page — and that surfacing it changed neither the rules nor any
record. Matching is a question asked of the buyer list. Assigning is a
separate, deliberate act, and no test here should ever see one happen.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.acquisitions import AcquisitionStore, Buyer
from wholesale_engine.acquisitions.models import Assignment
from wholesale_engine.hunt import HuntBudget, run_hunt
from wholesale_engine.providers import CsvProvider
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.reports.deal_room import _matching_buyers, render_deal_room
from wholesale_engine.service import EngineService
from wholesale_engine.service.paths import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.storage import LeadStore, SearchQuery
from wholesale_engine.web import create_app


class Seeded(unittest.TestCase):
    """A real hunt, then buyers added through the existing CRUD."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "leads.db"

        store = LeadStore(self.db)
        run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            HuntCriteria(min_lead_score=50),
            store=store, budget=HuntBudget(),
        )
        self.acquisitions = AcquisitionStore(store)
        self.store = store

        self.service = EngineService(db_path=self.db)
        self.client = create_app(service=self.service).test_client()

        self.row = next(
            r for r in self.service.search_leads(SearchQuery())
            if r.recommended_offer is not None and r.state and r.property_type
        )

    def tearDown(self) -> None:
        self.service.close()
        self.store.close()
        self._tmp.cleanup()

    def add_buyer(self, **values) -> Buyer:
        defaults = {
            "name": "Jane Cash", "company": "Bay Capital LLC",
            "preferred_states": [self.row.state],
            "property_types": [self.row.property_type],
            "min_price": 0.0, "max_price": 5_000_000.0,
        }
        return self.acquisitions.save_buyer(Buyer(**{**defaults, **values}))

    def detail(self) -> str:
        response = self.client.get(f"/leads/{self.row.dedupe_key}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The rules themselves are not touched
# ---------------------------------------------------------------------------


class RulesUnchanged(unittest.TestCase):
    def test_the_service_delegates_rather_than_matching(self):
        # No comparison operators, no price arithmetic — just a call through.
        source = inspect.getsource(EngineService.matching_buyers_for_property)
        self.assertIn("matching_buyers", source)
        for forbidden in ("min_price", "max_price", "preferred_states", " < ", " > "):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_web_route_does_not_match_anything_itself(self):
        from wholesale_engine.web import app as web_app

        source = inspect.getsource(web_app)
        self.assertIn("matching_buyers_for_property", source)
        for forbidden in ("Buyer(", ".matches(", "preferred_states"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_buyer_matches_still_has_its_original_semantics(self):
        # A blank preference matches everything; an unknown property attribute
        # never rules a buyer out. Shortlisting people to call, not filtering
        # them away on an empty field.
        anyone = Buyer(name="Anyone")
        self.assertTrue(anyone.matches(state="FL", property_type="condo", price=1))
        self.assertTrue(anyone.matches())

        picky = Buyer(name="Picky", preferred_states=["FL"],
                      property_types=["single_family"], min_price=50_000,
                      max_price=200_000)
        self.assertTrue(picky.matches("FL", "single_family", 100_000))
        self.assertFalse(picky.matches("TX", "single_family", 100_000))
        self.assertFalse(picky.matches("FL", "condo", 100_000))
        self.assertFalse(picky.matches("FL", "single_family", 500_000))
        # Unknown price does not rule them out.
        self.assertTrue(picky.matches("FL", "single_family", None))


# ---------------------------------------------------------------------------
# The service method
# ---------------------------------------------------------------------------


class ServiceMethod(Seeded):
    def test_it_returns_the_buyers_the_store_returns(self):
        self.add_buyer()
        from_service = self.service.matching_buyers_for_property(self.row)
        from_store = self.acquisitions.matching_buyers(
            state=self.row.state,
            property_type=self.row.property_type,
            price=self.row.recommended_offer,
        )
        self.assertEqual(
            [b.buyer_id for b in from_service], [b.buyer_id for b in from_store]
        )

    def test_it_passes_the_recommended_offer_as_the_price(self):
        # The established semantics, matching automation/daily_priority. A
        # buyer whose ceiling sits just under the recommended offer must not
        # match; one just above must.
        offer = self.row.recommended_offer
        self.add_buyer(name="Too low", company="A", max_price=offer - 1)
        self.add_buyer(name="High enough", company="B", max_price=offer + 1)
        names = [b.name for b in self.service.matching_buyers_for_property(self.row)]
        self.assertIn("High enough", names)
        self.assertNotIn("Too low", names)

    def test_no_buyers_on_file_is_an_empty_list_not_an_error(self):
        self.assertEqual(self.service.matching_buyers_for_property(self.row), [])

    def test_a_buyer_in_the_wrong_state_does_not_match(self):
        other = "TX" if self.row.state != "TX" else "FL"
        self.add_buyer(name="Elsewhere", preferred_states=[other])
        self.assertEqual(self.service.matching_buyers_for_property(self.row), [])

    def test_a_property_without_a_recommended_offer_still_matches_on_the_rest(self):
        # An unknown price is a gap, not a disqualification.
        self.add_buyer(min_price=1_000, max_price=2_000)
        row = self.service.search_leads(SearchQuery())[0]
        object.__setattr__(row, "recommended_offer", None) if hasattr(
            row, "__dataclass_fields__"
        ) else None
        row.recommended_offer = None
        self.assertEqual(len(self.service.matching_buyers_for_property(row)), 1)

    def test_a_missing_row_returns_empty_rather_than_raising(self):
        self.assertEqual(self.service.matching_buyers_for_property(None), [])

    def test_it_closes_the_connection_it_opens(self):
        for _ in range(30):
            self.service.matching_buyers_for_property(self.row)
            self.service.all_buyers()


# ---------------------------------------------------------------------------
# Nothing is assigned, nothing is modified
# ---------------------------------------------------------------------------


class NothingIsAssigned(Seeded):
    def test_matching_creates_no_assignment(self):
        self.add_buyer()
        self.service.matching_buyers_for_property(self.row)
        self.detail()
        self.assertEqual(self.acquisitions.all_assignments(), [])

    def test_an_existing_assignment_is_untouched(self):
        buyer = self.add_buyer()
        saved = self.acquisitions.save_assignment(Assignment(
            property_id=self.row.dedupe_key, buyer_id=buyer.buyer_id,
            buyer_name=buyer.name, purchase_price=90_000.0,
            assignment_price=108_000.0,
        ))
        before = self.acquisitions.assignment_for(self.row.dedupe_key)

        self.service.matching_buyers_for_property(self.row)
        self.detail()

        after = self.acquisitions.assignment_for(self.row.dedupe_key)
        self.assertEqual(after.assignment_id, saved.assignment_id)
        self.assertEqual(after.buyer_name, before.buyer_name)
        self.assertEqual(after.purchase_price, before.purchase_price)
        self.assertEqual(after.assignment_price, before.assignment_price)
        self.assertEqual(str(after.status), str(before.status))

    def test_matching_does_not_modify_the_buyer_record(self):
        buyer = self.add_buyer()
        self.service.matching_buyers_for_property(self.row)
        again = self.acquisitions.all_buyers()[0]
        self.assertEqual(again.as_dict(), buyer.as_dict())

    def test_the_property_page_has_no_assign_control(self):
        self.add_buyer()
        body = self.detail()
        for forbidden in ("<form", "method=\"post\"", "Assign"):
            self.assertNotIn(forbidden, body, forbidden)


# ---------------------------------------------------------------------------
# The web page
# ---------------------------------------------------------------------------


class WebPage(Seeded):
    def test_matching_buyers_are_displayed(self):
        self.add_buyer(name="Jane Cash", company="Bay Capital LLC",
                       phone="8135550142", email="jane@example.invalid")
        body = self.detail()
        self.assertIn("Matching buyers", body)
        self.assertIn("Jane Cash", body)
        self.assertIn("Bay Capital LLC", body)
        self.assertIn("jane@example.invalid", body)

    def test_the_page_shows_exactly_what_the_store_would_return(self):
        self.add_buyer(name="Alpha", company="A Co")
        self.add_buyer(name="Beta", company="B Co")
        other = "TX" if self.row.state != "TX" else "FL"
        self.add_buyer(name="Gamma", company="C Co", preferred_states=[other])

        expected = self.acquisitions.matching_buyers(
            state=self.row.state, property_type=self.row.property_type,
            price=self.row.recommended_offer,
        )
        body = self.detail()
        self.assertEqual({b.name for b in expected}, {"Alpha", "Beta"})
        for buyer in expected:
            self.assertIn(buyer.name, body)
        self.assertNotIn("Gamma", body)

    def test_a_buyer_s_price_range_and_terms_are_shown(self):
        self.add_buyer(min_price=50_000, max_price=250_000, market="Tampa")
        body = self.detail()
        self.assertIn("$50,000-$250,000", body)
        self.assertIn("Tampa", body)

    def test_no_matches_is_an_explicit_state_not_a_missing_section(self):
        other = "TX" if self.row.state != "TX" else "FL"
        self.add_buyer(name="Elsewhere", preferred_states=[other])
        body = self.detail()
        self.assertIn("Matching buyers", body)
        self.assertIn("No buyer on file matches this property", body)
        self.assertIn("not an error", body)

    def test_no_buyers_at_all_says_so_differently(self):
        # "Nobody matches" and "you have not added anyone" are different
        # problems with different fixes.
        body = self.detail()
        self.assertIn("No buyers on file yet", body)
        self.assertIn("--add-buyer", body)

    def test_test_data_buyers_are_labelled(self):
        self.add_buyer(name="Fake Buyer", is_test_data=True)
        self.assertIn("TEST DATA", self.detail())

    def test_the_phone_number_is_a_tap_to_call_link(self):
        self.add_buyer(phone="8135550142")
        body = self.detail()
        self.assertIn('href="tel:8135550142"', body)
        # Displayed the way the reports already print it, dialled as digits.
        self.assertIn("(813) 555-0142", body)

    def test_the_count_reads_correctly_for_one_and_for_many(self):
        self.add_buyer(name="Solo")
        self.assertIn("1 matching buyer on file", " ".join(self.detail().split()))
        self.add_buyer(name="Second", company="Other")
        self.assertIn("2 matching buyers on file", " ".join(self.detail().split()))

    def test_the_route_uses_the_service(self):
        from unittest import mock

        self.add_buyer()
        with mock.patch.object(
            EngineService, "matching_buyers_for_property",
            wraps=self.service.matching_buyers_for_property,
        ) as spy:
            self.detail()
        spy.assert_called_once()

    def test_a_property_with_no_state_or_type_does_not_crash_the_page(self):
        self.add_buyer()
        row = self.row
        row.state = ""
        row.property_type = ""
        row.recommended_offer = None
        self.assertEqual(self.service.matching_buyers_for_property(row), [
            b for b in self.acquisitions.all_buyers()
        ])

    def test_a_property_page_with_sparse_data_still_renders(self):
        sparse = next(
            (r for r in self.service.search_leads(SearchQuery())
             if r.recommended_offer is None), None
        )
        if sparse is None:
            self.skipTest("the fixture has no sparse property")
        response = self.client.get(f"/leads/{sparse.dedupe_key}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Matching buyers", response.get_data(as_text=True))


# ---------------------------------------------------------------------------
# The deal room
# ---------------------------------------------------------------------------


class DealRoom(Seeded):
    def test_matching_buyers_appear_in_the_deal_room(self):
        buyer = self.add_buyer(name="Jane Cash", company="Bay Capital LLC")
        text = render_deal_room(row=self.row, matching_buyers=[buyer])
        self.assertIn("MATCHING BUYERS", text)
        self.assertIn("Jane Cash", text)
        self.assertIn("Bay Capital LLC", text)

    def test_an_empty_list_prints_the_explicit_empty_state(self):
        text = " ".join(render_deal_room(row=self.row, matching_buyers=[]).split())
        self.assertIn("MATCHING BUYERS", text)
        self.assertIn("No buyer on file matches this property", text)
        self.assertIn("That is an answer, not an error", text)

    def test_none_means_nobody_looked_and_the_section_is_absent(self):
        # The distinction that keeps every existing caller unchanged.
        self.assertNotIn("MATCHING BUYERS", render_deal_room(row=self.row))

    def test_the_existing_buyer_status_section_is_untouched(self):
        text = render_deal_room(row=self.row, matching_buyers=[])
        self.assertIn("BUYER STATUS", text)
        self.assertIn("No buyer process started.", text)

    def test_the_section_says_matching_is_not_assigning(self):
        buyer = self.add_buyer()
        # The renderer wraps to 78 columns, so a phrase can straddle a line.
        text = " ".join(
            render_deal_room(row=self.row, matching_buyers=[buyer]).split()
        )
        self.assertIn("Matching does not assign anything", text)
        self.assertIn("--assign", text)

    def test_it_reports_the_price_it_matched_on(self):
        buyer = self.add_buyer()
        text = render_deal_room(row=self.row, matching_buyers=[buyer])
        from wholesale_engine.formatting import money

        self.assertIn(money(self.row.recommended_offer), text)

    def test_the_cli_passes_the_recommended_offer(self):
        from wholesale_engine import main

        source = inspect.getsource(main._render_deal_room)
        self.assertIn("matching_buyers=", source)
        self.assertIn("price=row.recommended_offer", source)

    def test_a_test_data_buyer_is_flagged_in_the_deal_room(self):
        buyer = self.add_buyer(name="Fake", is_test_data=True)
        self.assertIn("[TEST DATA]", "\n".join(_matching_buyers(self.row, [buyer])))

    def test_a_buyer_with_no_company_still_renders(self):
        buyer = self.add_buyer(name="Solo", company="")
        text = "\n".join(_matching_buyers(self.row, [buyer]))
        self.assertIn("Solo", text)
        self.assertNotIn("—  ", text)


# ---------------------------------------------------------------------------
# Nothing else changed
# ---------------------------------------------------------------------------


class ExistingBehaviourUnchanged(Seeded):
    def test_the_rest_of_the_property_page_is_intact(self):
        body = self.detail()
        for heading in ("Scores", "Economics", "Equity", "Confidence",
                        "Decision history"):
            self.assertIn(heading, body)

    def test_the_other_dashboard_pages_are_unaffected(self):
        for url in ("/leads", "/runs", "/buybox", "/healthz"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_app_is_still_read_only(self):
        app = create_app(service=self.service)
        for rule in app.url_map.iter_rules():
            self.assertEqual(
                rule.methods & {"POST", "PUT", "PATCH", "DELETE"}, set(), str(rule)
            )

    def test_the_daily_priority_caller_is_unchanged(self):
        from wholesale_engine.automation import daily_priority

        source = inspect.getsource(daily_priority)
        self.assertIn("price=row.recommended_offer", source)


if __name__ == "__main__":
    unittest.main()
