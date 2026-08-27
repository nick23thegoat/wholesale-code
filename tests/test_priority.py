"""The PRIORITY SCORE — a third, separate ranking metric.

What these tests hold in place: priority reads the other two scores and never
writes to them, a below-target fee is never disqualifying, and unverifiable
data ranks below verified data at the same deal score.
"""

from __future__ import annotations

import unittest

from wholesale_engine.models.enums import ARVConfidence, CompConfidence
from wholesale_engine.priority import (
    PRIORITY_BANDS,
    PRIORITY_WEIGHTS,
    PriorityBand,
    PriorityEngine,
    classify_priority,
)


class BandTests(unittest.TestCase):
    def test_all_five_bands_exist(self):
        self.assertEqual(
            {b.value for b in PriorityBand},
            {"🔥 PRIORITY", "🟠 HIGH", "🟡 REVIEW", "🔵 LOW", "❌ REJECT"},
        )

    def test_the_bands_are_ordered_and_contiguous(self):
        self.assertEqual(classify_priority(100), PriorityBand.PRIORITY)
        self.assertEqual(classify_priority(PRIORITY_BANDS["PRIORITY"]), PriorityBand.PRIORITY)
        self.assertEqual(classify_priority(PRIORITY_BANDS["HIGH"]), PriorityBand.HIGH)
        self.assertEqual(classify_priority(PRIORITY_BANDS["REVIEW"]), PriorityBand.REVIEW)
        self.assertEqual(classify_priority(PRIORITY_BANDS["LOW"]), PriorityBand.LOW)
        self.assertEqual(classify_priority(0), PriorityBand.REJECT)

    def test_weights_sum_to_one_hundred(self):
        self.assertAlmostEqual(sum(PRIORITY_WEIGHTS.values()), 100.0)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.engine = PriorityEngine()

    def strong(self, **overrides):
        base = dict(
            lead_score=90.0, deal_score=85.0, wholesale_fee=25_000.0,
            data_confidence=0.9, arv_confidence=ARVConfidence.VERIFIED_SUPPORTED,
            comp_confidence=CompConfidence.HIGH, distress_count=3,
            urgent_distress_count=1, equity_percentage=0.45,
            equity_is_calculated=True, days_on_market=120, decision="🔥 GO",
        )
        base.update(overrides)
        return self.engine.score(**base)

    def test_a_strong_deal_scores_in_the_top_bands(self):
        result = self.strong()
        self.assertGreaterEqual(result.total, PRIORITY_BANDS["HIGH"])
        self.assertTrue(result.is_actionable)

    def test_the_score_is_bounded(self):
        self.assertLessEqual(self.strong(change_bump=100).total, 100.0)
        self.assertGreaterEqual(self.engine.score().total, 0.0)

    def test_every_component_is_reported(self):
        result = self.strong()
        self.assertEqual(len(result.components), len(PRIORITY_WEIGHTS))
        for component in result.components:
            self.assertLessEqual(component.points, component.weight)

    # --- the fee is a target, not a gate --------------------------------

    def test_a_below_target_fee_still_scores(self):
        result = self.strong(wholesale_fee=13_000.0)
        component = result.component("wholesale fee")
        self.assertGreater(component.points, 0.0)

    def test_a_below_target_fee_can_still_reach_a_top_band(self):
        self.assertTrue(self.strong(wholesale_fee=13_000.0).is_actionable)

    def test_fee_credit_is_proportional(self):
        low = self.strong(wholesale_fee=9_000.0).component("wholesale fee").points
        mid = self.strong(wholesale_fee=18_000.0).component("wholesale fee").points
        high = self.strong(wholesale_fee=29_000.0).component("wholesale fee").points
        self.assertLess(low, mid)
        self.assertLess(mid, high)

    def test_no_fee_at_all_scores_zero_but_does_not_reject(self):
        result = self.strong(wholesale_fee=-5_000.0)
        self.assertEqual(result.component("wholesale fee").points, 0.0)
        self.assertGreater(result.total, 0.0)

    def test_the_target_is_configurable(self):
        engine = PriorityEngine(target_wholesale_fee=40_000.0)
        result = engine.score(wholesale_fee=20_000.0)
        self.assertIn("$40,000 target", result.component("wholesale fee").note)

    # --- confidence -----------------------------------------------------

    def test_unverified_data_ranks_below_verified_at_the_same_deal_score(self):
        verified = self.strong()
        unverified = self.strong(
            data_confidence=0.2,
            arv_confidence=ARVConfidence.USER_PROVIDED,
            comp_confidence=CompConfidence.NONE,
        )
        self.assertLess(unverified.total, verified.total)

    def test_missing_confidence_inputs_score_low_not_zero(self):
        result = self.engine.score(deal_score=80.0)
        self.assertGreater(result.component("data confidence").points, 0.0)

    # --- distress -------------------------------------------------------

    def test_urgent_distress_outranks_the_same_count_of_static_signals(self):
        urgent = self.strong(distress_count=3, urgent_distress_count=3)
        static = self.strong(distress_count=3, urgent_distress_count=0)
        self.assertGreater(urgent.total, static.total)

    def test_no_distress_scores_zero_on_that_component(self):
        self.assertEqual(self.strong(distress_count=0).component("distress").points, 0.0)

    # --- equity ---------------------------------------------------------

    def test_a_derived_spread_earns_less_than_calculated_equity(self):
        calculated = self.strong(equity_is_calculated=True)
        derived = self.strong(equity_is_calculated=False)
        self.assertLess(
            derived.component("equity").points, calculated.component("equity").points
        )

    def test_unknown_equity_scores_neutral_not_zero(self):
        result = self.strong(equity_percentage=None)
        self.assertGreater(result.component("equity").points, 0.0)

    # --- price movement -------------------------------------------------

    def test_a_price_drop_raises_priority(self):
        flat = self.strong(price_drop_percentage=None)
        dropped = self.strong(price_drop_percentage=0.17)
        self.assertGreater(dropped.total, flat.total)

    def test_a_bigger_drop_raises_it_further(self):
        small = self.strong(price_drop_percentage=0.03).total
        large = self.strong(price_drop_percentage=0.20).total
        self.assertGreater(large, small)

    def test_a_price_increase_scores_zero_on_that_component(self):
        result = self.strong(price_drop_percentage=-0.10)
        self.assertEqual(result.component("price movement").points, 0.0)

    # --- days on market -------------------------------------------------

    def test_a_stale_listing_outranks_a_fresh_one(self):
        fresh = self.strong(days_on_market=3)
        stale = self.strong(days_on_market=300)
        self.assertGreater(stale.total, fresh.total)

    # --- the PASS cap ---------------------------------------------------

    def test_a_pass_decision_is_capped_below_low(self):
        result = self.strong(decision="❌ PASS")
        self.assertIs(result.band, PriorityBand.REJECT)
        self.assertIn("PASS", result.rejected_because)

    def test_a_go_decision_is_not_capped(self):
        self.assertNotEqual(self.strong(decision="🔥 GO").band, PriorityBand.REJECT)

    # --- separation from the other scores -------------------------------

    def test_priority_is_not_simply_the_deal_score(self):
        result = self.strong()
        self.assertNotEqual(result.total, 85.0)

    def test_an_unanalyzed_lead_still_gets_a_score(self):
        result = self.engine.score(lead_score=90.0)
        self.assertGreater(result.total, 0.0)
        self.assertIsNotNone(result.band)

    def test_a_high_lead_score_alone_does_not_reach_the_top_band(self):
        # A hot lead can still be a bad deal. Priority must not forget that.
        result = self.engine.score(lead_score=100.0)
        self.assertNotEqual(result.band, PriorityBand.PRIORITY)

    def test_the_render_shows_every_component(self):
        text = self.strong().render()
        self.assertIn("PRIORITY", text)
        for name in ("deal score", "lead score", "wholesale fee", "distress"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
