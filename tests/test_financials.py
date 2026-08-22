"""Unit tests for the deal math.

Run with either runner::

    python -m unittest discover -s tests -v
    pytest tests -v
"""

from __future__ import annotations

import unittest

from wholesale_engine.analysis import financials as fin
from wholesale_engine.config import EngineConfig


class SeventyPercentARVTests(unittest.TestCase):
    def test_uses_the_configured_percentage(self):
        self.assertEqual(fin.seventy_percent_arv(200_000), 140_000)

    def test_percentage_is_overridable(self):
        aggressive = EngineConfig(arv_percentage=0.75)
        self.assertEqual(fin.seventy_percent_arv(200_000, aggressive), 150_000)

    def test_zero_arv(self):
        self.assertEqual(fin.seventy_percent_arv(0), 0)


class MAOTests(unittest.TestCase):
    def test_textbook_formula(self):
        # (200,000 x 0.70) - 30,000 - 18,000
        self.assertEqual(fin.maximum_allowable_offer(200_000, 30_000), 92_000)

    def test_zero_repairs(self):
        self.assertEqual(fin.maximum_allowable_offer(300_000, 0), 192_000)

    def test_mao_can_be_negative_and_is_not_clamped(self):
        # A low ARV against a heavy rehab has no viable purchase price. The
        # engine must report that honestly rather than flooring at zero.
        self.assertEqual(fin.maximum_allowable_offer(100_000, 90_000), -38_000)

    def test_custom_fee_overrides_the_default(self):
        self.assertEqual(
            fin.maximum_allowable_offer(200_000, 30_000, wholesale_fee=25_000), 85_000
        )

    def test_fee_and_percentage_together(self):
        config = EngineConfig(arv_percentage=0.65, wholesale_fee=10_000)
        self.assertEqual(fin.maximum_allowable_offer(400_000, 50_000, config), 200_000)

    def test_repairs_reduce_mao_dollar_for_dollar(self):
        base = fin.maximum_allowable_offer(250_000, 20_000)
        self.assertEqual(fin.maximum_allowable_offer(250_000, 30_000), base - 10_000)


class AssignmentAndSpreadTests(unittest.TestCase):
    def test_assignment_price_adds_the_fee(self):
        self.assertEqual(fin.assignment_price(80_000), 98_000)

    def test_assignment_price_honours_a_custom_fee(self):
        self.assertEqual(fin.assignment_price(80_000, wholesale_fee=30_000), 110_000)

    def test_gross_spread_is_mao_minus_purchase_price(self):
        self.assertEqual(fin.gross_spread(92_000, 80_000), 12_000)

    def test_gross_spread_is_zero_at_full_mao(self):
        self.assertEqual(fin.gross_spread(92_000, 92_000), 0)

    def test_gross_spread_goes_negative_above_mao(self):
        self.assertEqual(fin.gross_spread(92_000, 100_000), -8_000)

    def test_spread_and_assignment_reconcile(self):
        mao = fin.maximum_allowable_offer(200_000, 30_000)
        offer = 75_000
        self.assertEqual(
            fin.assignment_price(offer) + fin.gross_spread(mao, offer),
            mao + 18_000,
        )


class RoundingTests(unittest.TestCase):
    def test_rounds_down_to_the_configured_step(self):
        self.assertEqual(fin.round_offer_down(83_499), 83_000)

    def test_leaves_exact_multiples_alone(self):
        self.assertEqual(fin.round_offer_down(83_500), 83_500)

    def test_never_rounds_up(self):
        for amount in (1, 499, 501, 99_999):
            self.assertLessEqual(fin.round_offer_down(amount), amount)

    def test_negative_amounts_floor_at_zero(self):
        self.assertEqual(fin.round_offer_down(-5_000), 0.0)

    def test_step_of_zero_is_a_no_op(self):
        self.assertEqual(fin.round_offer_down(1_234.56, EngineConfig(offer_rounding=0)), 1_234.56)


class RecommendedOfferTests(unittest.TestCase):
    def test_offer_sits_below_mao(self):
        offer = fin.recommended_offer(100_000, 0.10)
        self.assertEqual(offer, 90_000)

    def test_minimum_haircut_is_always_applied(self):
        # Even with zero risk points the engine does not recommend full MAO.
        offer = fin.recommended_offer(100_000, 0.0)
        self.assertLess(offer, 100_000)
        self.assertEqual(offer, 97_000)  # 3% floor, rounded down to $500

    def test_discount_is_capped(self):
        config = EngineConfig()
        offer = fin.recommended_offer(100_000, 0.90, config=config)
        self.assertEqual(offer, 100_000 * (1 - config.max_offer_discount))

    def test_offer_never_exceeds_asking_price(self):
        offer = fin.recommended_offer(100_000, 0.05, asking_price=60_000)
        self.assertEqual(offer, 60_000)

    def test_negative_mao_produces_no_offer(self):
        self.assertEqual(fin.recommended_offer(-10_000, 0.10), 0.0)

    def test_offer_is_rounded_down(self):
        self.assertEqual(fin.recommended_offer(91_234, 0.10), 82_000)


class RiskDiscountTests(unittest.TestCase):
    def test_points_accumulate(self):
        discount, reasons = fin.offer_risk_discount(
            [("unverified ARV", 0.10), ("condition unknown", 0.05)]
        )
        self.assertAlmostEqual(discount, 0.15)
        self.assertEqual(reasons, ["unverified ARV", "condition unknown"])

    def test_no_points_still_returns_the_floor(self):
        config = EngineConfig()
        discount, reasons = fin.offer_risk_discount([])
        self.assertEqual(discount, config.min_offer_discount)
        self.assertEqual(reasons, [])

    def test_total_is_capped(self):
        config = EngineConfig()
        discount, _ = fin.offer_risk_discount([("a", 0.5), ("b", 0.5)])
        self.assertEqual(discount, config.max_offer_discount)

    def test_zero_weight_reasons_are_not_listed(self):
        _, reasons = fin.offer_risk_discount([("counts", 0.04), ("does not count", 0.0)])
        self.assertEqual(reasons, ["counts"])


class RatioTests(unittest.TestCase):
    def test_discount_from_arv(self):
        self.assertAlmostEqual(fin.discount_from_arv(140_000, 200_000), 0.30)

    def test_discount_from_arv_is_negative_above_arv(self):
        self.assertAlmostEqual(fin.discount_from_arv(220_000, 200_000), -0.10)

    def test_discount_from_arv_guards_zero(self):
        self.assertIsNone(fin.discount_from_arv(100_000, 0))

    def test_equity_position(self):
        self.assertEqual(fin.equity_position(200_000, 90_000, 30_000), 80_000)

    def test_repair_ratio(self):
        self.assertAlmostEqual(fin.repair_ratio(50_000, 200_000), 0.25)

    def test_repair_ratio_guards_zero(self):
        self.assertIsNone(fin.repair_ratio(50_000, 0))

    def test_rent_to_value_ratio(self):
        self.assertAlmostEqual(fin.rent_to_value_ratio(2_000, 200_000), 0.01)

    def test_rent_to_value_ratio_guards_zero(self):
        self.assertIsNone(fin.rent_to_value_ratio(2_000, 0))


class ScenarioTests(unittest.TestCase):
    def test_three_scenarios_are_built(self):
        scenarios = fin.build_mao_scenarios(200_000, 20_000, 30_000, 40_000)
        self.assertEqual([s.name for s in scenarios], ["Low rehab", "Mid rehab", "High rehab"])
        self.assertEqual([s.mao for s in scenarios], [102_000, 92_000, 82_000])

    def test_missing_scenarios_are_skipped(self):
        scenarios = fin.build_mao_scenarios(200_000, None, 30_000, None)
        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0].name, "Mid rehab")

    def test_spread_vs_asking_is_computed_when_asking_is_known(self):
        scenarios = fin.build_mao_scenarios(200_000, 20_000, 30_000, 40_000, asking_price=95_000)
        self.assertEqual([s.spread_vs_asking for s in scenarios], [7_000, -3_000, -13_000])

    def test_spread_vs_asking_is_none_without_an_asking_price(self):
        scenarios = fin.build_mao_scenarios(200_000, 20_000, 30_000, 40_000)
        self.assertTrue(all(s.spread_vs_asking is None for s in scenarios))

    def test_higher_repairs_never_raise_the_mao(self):
        scenarios = fin.build_mao_scenarios(200_000, 20_000, 30_000, 40_000)
        maos = [s.mao for s in scenarios]
        self.assertEqual(maos, sorted(maos, reverse=True))


class ReverseMathTests(unittest.TestCase):
    def test_implied_arv_inverts_the_mao_formula(self):
        arv = fin.implied_arv_for_offer(92_000, 30_000)
        self.assertAlmostEqual(arv, 200_000)
        self.assertAlmostEqual(fin.maximum_allowable_offer(arv, 30_000), 92_000)

    def test_max_repairs_inverts_the_mao_formula(self):
        repairs = fin.max_repairs_for_offer(200_000, 92_000)
        self.assertAlmostEqual(repairs, 30_000)
        self.assertAlmostEqual(fin.maximum_allowable_offer(200_000, repairs), 92_000)

    def test_implied_arv_exposes_an_unrealistic_asking_price(self):
        # Asking 150k on a 30k rehab requires a 282,857 ARV to work.
        required = fin.implied_arv_for_offer(150_000, 30_000)
        self.assertGreater(required, 280_000)


if __name__ == "__main__":
    unittest.main()
