"""Buy box config and the decision log.

Both exist because the engine now runs unattended on a server: the buy box has
to be editable without a deploy, and a rejection has to still be explainable
days later from a phone.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wholesale_engine.buybox import ALLOWED_PROPERTY_TYPES, BuyBox, config_path
from wholesale_engine.storage.database import LeadStore
from wholesale_engine.storage.decisions import (
    ACCEPTED,
    INCOMPLETE,
    REJECTED,
    STAGE_BUY_BOX,
    STAGE_FINAL,
    STAGE_LEAD_SCORE,
    Decision,
    DecisionLog,
)


class BuyBoxDefaultsTests(unittest.TestCase):

    def test_the_default_buy_box_is_valid_and_usable(self):
        box = BuyBox()
        self.assertEqual(box.validate(), [])
        self.assertTrue(box.is_valid)

    def test_the_default_price_range_has_no_low_ceiling(self):
        box = BuyBox()
        self.assertEqual(box.min_price, 0.0)
        self.assertEqual(box.max_price, 2_200_000.0)

    def test_the_target_fee_is_not_a_minimum(self):
        """Regression: the viability floor and the target are different things."""
        box = BuyBox()
        self.assertEqual(box.target_wholesale_fee, 18_000.0)
        self.assertLess(box.min_viable_wholesale_fee, box.target_wholesale_fee)

    def test_search_count_is_the_monthly_cost_of_one_run(self):
        self.assertEqual(BuyBox(zip_codes=["33607", "33609"]).search_count, 2)
        self.assertEqual(BuyBox(zip_codes=[], cities=["tampa"]).search_count, 1)
        # No geography still costs at least one request to find that out.
        self.assertEqual(BuyBox().search_count, 1)


class BuyBoxValidationTests(unittest.TestCase):

    def test_every_problem_is_reported_at_once_not_one_at_a_time(self):
        box = BuyBox(
            name="", states=["FLORIDA"], zip_codes=["ABCDE"],
            required_signals=["nonsense"], min_price=500, max_price=100,
        )
        problems = box.validate()
        self.assertGreaterEqual(len(problems), 5, problems)

    def test_an_inverted_range_is_caught(self):
        problems = BuyBox(min_price=300_000, max_price=100_000).validate()
        self.assertTrue(any("nothing can match" in p for p in problems))

    def test_an_unknown_signal_lists_the_valid_ones(self):
        problems = BuyBox(required_signals=["unicorn"]).validate()
        self.assertTrue(any("unicorn" in p and "vacant" in p for p in problems))

    def test_a_property_type_the_analyzer_cannot_underwrite_is_refused(self):
        problems = BuyBox(property_types=["land"]).validate()
        self.assertTrue(any("underwritable" in p for p in problems))
        self.assertNotIn("land", ALLOWED_PROPERTY_TYPES)

    def test_a_viability_floor_above_the_target_is_incoherent(self):
        problems = BuyBox(
            target_wholesale_fee=18_000, min_viable_wholesale_fee=25_000
        ).validate()
        self.assertTrue(any("viability floor" in p for p in problems))

    def test_no_geography_at_all_is_a_problem(self):
        problems = BuyBox(states=[], zip_codes=[], cities=[], counties=[]).validate()
        self.assertTrue(any("nowhere to look" in p for p in problems))

    def test_a_score_outside_zero_to_a_hundred_is_refused(self):
        self.assertTrue(BuyBox(min_lead_score=150).validate())
        self.assertTrue(BuyBox(min_deal_score=-1).validate())


class BuyBoxParsingTests(unittest.TestCase):
    """A buy box edited from a phone form arrives as strings."""

    def test_comma_separated_strings_become_lists(self):
        box, _ = BuyBox.from_dict({"zip_codes": "33607, 33609", "states": "fl, tx"})
        self.assertEqual(box.zip_codes, ["33607", "33609"])
        self.assertEqual(box.states, ["FL", "TX"])

    def test_numbers_arriving_as_formatted_strings_are_read(self):
        box, _ = BuyBox.from_dict({"max_price": "$2,200,000", "min_sqft": "1,000"})
        self.assertEqual(box.max_price, 2_200_000.0)
        self.assertEqual(box.min_sqft, 1000)

    def test_a_blank_number_means_no_constraint_not_zero(self):
        box, _ = BuyBox.from_dict({"max_price": ""})
        self.assertIsNone(box.max_price, "blank must mean unbounded, never 0")

    def test_an_unparseable_value_warns_and_keeps_the_default(self):
        box, warnings = BuyBox.from_dict({"min_lead_score": "not a number"})
        self.assertEqual(box.min_lead_score, 0.0)
        self.assertTrue(any("min_lead_score" in w for w in warnings))

    def test_an_unknown_key_warns_rather_than_failing(self):
        box, warnings = BuyBox.from_dict({"typo_field": 1, "name": "kept"})
        self.assertEqual(box.name, "kept")
        self.assertTrue(any("typo_field" in w for w in warnings))

    def test_booleans_accept_form_values(self):
        for raw, expected in (("on", True), ("true", True), ("", False), ("no", False)):
            box, _ = BuyBox.from_dict({"enabled": raw})
            self.assertIs(box.enabled, expected, raw)

    def test_a_round_trip_through_json_preserves_everything(self):
        original = BuyBox(
            name="Tampa core", zip_codes=["33607"], required_signals=["vacant"],
            min_lead_score=60, max_price=450_000,
        )
        restored, warnings = BuyBox.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(warnings, [])
        self.assertEqual(restored.to_dict(), original.to_dict())


class BuyBoxDiskTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "buybox.json"

    def test_a_missing_file_means_defaults_not_a_crash(self):
        box, warnings = BuyBox.load(self.path)
        self.assertTrue(box.is_valid)
        self.assertTrue(any("using defaults" in w for w in warnings))

    def test_corrupt_json_never_kills_the_run(self):
        """A 3am scheduled run must not die because a field was edited badly."""
        self.path.write_text("{ this is not json", encoding="utf-8")
        box, warnings = BuyBox.load(self.path)
        self.assertTrue(box.is_valid, "must fall back to a working config")
        self.assertTrue(any("could not read" in w for w in warnings))

    def test_a_json_array_instead_of_an_object_is_handled(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        box, warnings = BuyBox.load(self.path)
        self.assertTrue(box.is_valid)
        self.assertTrue(any("not a JSON object" in w for w in warnings))

    def test_save_then_load_round_trips(self):
        BuyBox(name="Tampa", zip_codes=["33607"], min_lead_score=55).save(self.path)
        box, warnings = BuyBox.load(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(box.name, "Tampa")
        self.assertEqual(box.zip_codes, ["33607"])
        self.assertEqual(box.min_lead_score, 55)

    def test_an_invalid_buy_box_never_reaches_disk(self):
        with self.assertRaises(ValueError) as ctx:
            BuyBox(min_price=500, max_price=100).save(self.path)
        self.assertIn("refusing to save", str(ctx.exception))
        self.assertFalse(self.path.exists())

    def test_saving_leaves_no_temporary_file_behind(self):
        BuyBox(name="Tampa", zip_codes=["33607"]).save(self.path)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["buybox.json"])

    def test_load_reports_validation_problems_from_a_hand_edited_file(self):
        self.path.write_text(json.dumps({"zip_codes": ["BAD"]}), encoding="utf-8")
        _, warnings = BuyBox.load(self.path)
        self.assertTrue(any("not a 5-digit ZIP" in w for w in warnings))

    def test_the_path_is_overridable_so_git_pull_cannot_clobber_it(self):
        with mock.patch.dict(os.environ, {"BUYBOX_PATH": "/srv/wholesale/bb.json"}):
            self.assertEqual(config_path(), Path("/srv/wholesale/bb.json"))


class DecisionLogTests(unittest.TestCase):

    def setUp(self):
        self.store = LeadStore(":memory:")
        self.log = DecisionLog(self.store.connection)

    def tearDown(self):
        self.store.close()

    def seed(self):
        run = self.log.start_run(trigger="scheduled", provider="rentcast")
        self.log.record_many(run.run_id, [
            Decision(address="1 A St", stage=STAGE_BUY_BOX, outcome=REJECTED,
                     reason="asking price above buy box maximum"),
            Decision(address="2 B St", stage=STAGE_LEAD_SCORE, outcome=REJECTED,
                     reason="below minimum lead score", lead_score=42.0),
            Decision(address="3 C St", stage=STAGE_LEAD_SCORE, outcome=REJECTED,
                     reason="below minimum lead score", lead_score=51.0),
            Decision(address="4 D St", stage=STAGE_FINAL, outcome=ACCEPTED,
                     reason="cleared every gate", lead_score=88.0, deal_score=81.0),
        ])
        return run

    def test_a_run_is_recorded_and_can_be_closed_out(self):
        run = self.log.start_run(trigger="scheduled")
        self.assertEqual(run.status, "RUNNING")
        self.log.finish_run(run, status="OK", leads_seen=10, leads_accepted=2)
        stored = self.log.get_run(run.run_id)
        self.assertEqual(stored.status, "OK")
        self.assertEqual(stored.leads_seen, 10)
        self.assertTrue(stored.finished_at)

    def test_a_failed_run_still_leaves_a_record(self):
        """A scheduled job that vanishes silently is worse than one that says so."""
        run = self.log.start_run(trigger="scheduled")
        self.log.finish_run(run, status="FAILED", error="RentCast returned 503")
        stored = self.log.get_run(run.run_id)
        self.assertEqual(stored.status, "FAILED")
        self.assertIn("503", stored.error)
        self.assertFalse(stored.succeeded)

    def test_accepted_and_rejected_are_both_recorded(self):
        run = self.seed()
        self.assertEqual(len(self.log.for_run(run.run_id)), 4)
        self.assertEqual(len(self.log.for_run(run.run_id, outcome=REJECTED)), 3)
        self.assertEqual(len(self.log.for_run(run.run_id, outcome=ACCEPTED)), 1)

    def test_the_rejection_summary_groups_by_reason_commonest_first(self):
        run = self.seed()
        summary = self.log.rejection_summary(run.run_id)
        self.assertEqual(summary[0][1], "below minimum lead score")
        self.assertEqual(summary[0][2], 2)

    def test_the_summary_says_which_rule_is_doing_the_throwing_away(self):
        run = self.seed()
        text = self.log.render_summary(run.run_id)
        self.assertIn("below minimum lead score", text)
        self.assertIn("[lead_score]", text)
        self.assertIn("67%", text)

    def test_a_run_with_no_rejections_says_so_rather_than_rendering_nothing(self):
        run = self.log.start_run()
        self.assertIn("No rejections", self.log.render_summary(run.run_id))

    def test_one_property_can_be_traced_across_runs(self):
        """A property rejected every week for the same reason is a buy box problem."""
        for _ in range(3):
            run = self.log.start_run(trigger="scheduled")
            self.log.record_many(run.run_id, [
                Decision(dedupe_key="1-a-st|tampa|fl|33607", address="1 A St",
                         stage=STAGE_LEAD_SCORE, outcome=REJECTED,
                         reason="below minimum lead score", lead_score=42.0),
            ])
        history = self.log.for_property("1-a-st|tampa|fl|33607")
        self.assertEqual(len(history), 3)
        self.assertTrue(all(d.was_rejected for d in history))

    def test_incomplete_is_not_a_rejection(self):
        run = self.log.start_run()
        self.log.record_many(run.run_id, [
            Decision(address="5 E St", outcome=INCOMPLETE,
                     reason="no ARV could be established"),
        ])
        self.assertEqual(self.log.rejection_summary(run.run_id), [])
        decision = self.log.for_run(run.run_id)[0]
        self.assertFalse(decision.was_rejected)

    def test_recent_runs_are_newest_first(self):
        first = self.log.start_run(buy_box="first")
        second = self.log.start_run(buy_box="second")
        runs = self.log.recent_runs()
        self.assertEqual(runs[0].run_id, second.run_id)
        self.assertEqual(runs[1].run_id, first.run_id)

    def test_last_successful_run_skips_failures(self):
        good = self.log.start_run()
        self.log.finish_run(good, status="OK")
        bad = self.log.start_run()
        self.log.finish_run(bad, status="FAILED", error="boom")
        self.assertEqual(self.log.last_successful_run().run_id, good.run_id)

    def test_a_decision_renders_readably(self):
        decision = Decision(
            address="2 B St", outcome=REJECTED,
            reason="below minimum lead score", detail="42.0 below minimum 60",
        )
        text = decision.render()
        self.assertIn("✗", text)
        self.assertIn("2 B St", text)
        self.assertIn("42.0 below minimum 60", text)


if __name__ == "__main__":
    unittest.main()
