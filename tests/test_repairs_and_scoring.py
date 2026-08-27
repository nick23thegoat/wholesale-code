"""Tests for the rehab estimator, the deal score, and the classification bands."""

from __future__ import annotations

import unittest

from wholesale_engine.analysis.repairs import estimate_repairs
from wholesale_engine.analysis.scoring import apply_score_cap, classify, score_deal
from wholesale_engine.config import DEFAULT_CONFIG
from wholesale_engine.models import (
    ARVAssessment,
    ARVConfidence,
    Classification,
    CompAnalysis,
    CompConfidence,
    Condition,
    FinancialSummary,
    PropertyLead,
    PropertyType,
    RepairConfidence,
    RepairEstimate,
    SellerMotivation,
)


class RepairEstimateTests(unittest.TestCase):
    def test_user_figure_is_used_and_labelled(self):
        lead = PropertyLead(address="x", user_repair_estimate=40_000, condition=Condition.MODERATE, sqft=1_500)
        estimate, _ = estimate_repairs(lead)
        self.assertEqual(estimate.base, 40_000)
        self.assertEqual(estimate.confidence, RepairConfidence.USER_PROVIDED)
        self.assertIn("not a contractor quote", estimate.basis_note)

    def test_user_figure_is_banded_upward_for_overruns(self):
        lead = PropertyLead(address="x", user_repair_estimate=40_000, condition=Condition.MODERATE, sqft=1_500)
        estimate, _ = estimate_repairs(lead)
        self.assertEqual(estimate.low, 40_000)
        self.assertGreater(estimate.mid, estimate.low)
        self.assertGreater(estimate.high, estimate.mid)

    def test_suspiciously_low_user_figure_is_flagged(self):
        lead = PropertyLead(address="x", user_repair_estimate=5_000, condition=Condition.HEAVY, sqft=1_600)
        _, flags = estimate_repairs(lead)
        self.assertTrue(any(f.code == "repairs_understated" for f in flags))

    def test_condition_drives_the_estimate_when_repairs_are_unknown(self):
        lead = PropertyLead(address="x", condition=Condition.COSMETIC, sqft=1_500)
        estimate, flags = estimate_repairs(lead)
        self.assertEqual(estimate.confidence, RepairConfidence.CONDITION_BASED)
        self.assertEqual(estimate.base, estimate.mid)
        self.assertTrue(any(f.code == "repairs_estimated" for f in flags))

    def test_heavier_condition_costs_more(self):
        light, _ = estimate_repairs(PropertyLead(address="x", condition=Condition.COSMETIC, sqft=1_500))
        heavy, _ = estimate_repairs(PropertyLead(address="x", condition=Condition.HEAVY, sqft=1_500))
        self.assertGreater(heavy.base, light.base)

    def test_old_houses_carry_an_age_multiplier(self):
        modern, _ = estimate_repairs(
            PropertyLead(address="x", condition=Condition.MODERATE, sqft=1_500, year_built=2005)
        )
        ancient, _ = estimate_repairs(
            PropertyLead(address="x", condition=Condition.MODERATE, sqft=1_500, year_built=1920)
        )
        self.assertGreater(ancient.base, modern.base)

    def test_missing_sqft_falls_back_to_a_flat_range(self):
        estimate, _ = estimate_repairs(PropertyLead(address="x", condition=Condition.MODERATE))
        self.assertIsNotNone(estimate.base)
        self.assertIn("square footage unknown", estimate.basis_note)

    def test_no_condition_and_no_figure_is_insufficient_data(self):
        estimate, flags = estimate_repairs(PropertyLead(address="x"))
        self.assertEqual(estimate.confidence, RepairConfidence.INSUFFICIENT_DATA)
        self.assertIsNone(estimate.base)
        self.assertFalse(estimate.is_usable)
        self.assertTrue(any(f.code == "no_repair_basis" for f in flags))


class ClassificationTests(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(classify(100), Classification.HOT)
        self.assertEqual(classify(90), Classification.HOT)
        self.assertEqual(classify(89.9), Classification.STRONG)
        self.assertEqual(classify(75), Classification.STRONG)
        self.assertEqual(classify(74.9), Classification.POSSIBLE)
        self.assertEqual(classify(60), Classification.POSSIBLE)
        self.assertEqual(classify(59.9), Classification.WEAK)
        self.assertEqual(classify(40), Classification.WEAK)
        self.assertEqual(classify(39.9), Classification.PASS)
        self.assertEqual(classify(0), Classification.PASS)


class ScoreTests(unittest.TestCase):
    def _score(self, lead, arv_value, repairs_base, mao, offer, comp_conf, arv_conf):
        comps = CompAnalysis(confidence=comp_conf, mean_quality=0.8)
        comps.reliable_evaluations = []
        arv = ARVAssessment(arv=arv_value, confidence=arv_conf, source_note="test")
        repairs = RepairEstimate(
            low=repairs_base * 0.9,
            mid=repairs_base,
            high=repairs_base * 1.2,
            base=repairs_base,
            confidence=RepairConfidence.USER_PROVIDED,
            basis_note="test",
        )
        financials = FinancialSummary(
            arv=arv_value,
            repairs_used=repairs_base,
            mao=mao,
            recommended_offer=offer,
            assignment_price=offer + DEFAULT_CONFIG.wholesale_fee,
            potential_gross_spread=mao - offer,
            spread_vs_asking=None if lead.asking_price is None else mao - lead.asking_price,
            target_wholesale_fee=DEFAULT_CONFIG.target_wholesale_fee,
        )
        return score_deal(lead, comps, arv, repairs, financials)

    def test_a_clean_deal_scores_well(self):
        lead = PropertyLead(
            address="x",
            city="Y",
            county="Z",
            asking_price=90_000,
            beds=3,
            baths=2,
            sqft=1_500,
            lot_size_sqft=7_000,
            year_built=1995,
            property_type=PropertyType.SINGLE_FAMILY,
            condition=Condition.COSMETIC,
            seller_motivation=SellerMotivation.HIGH,
            days_on_market=95,
            estimated_monthly_rent=1_800,
        )
        score = self._score(
            lead, 250_000, 30_000, 127_000, 118_000, CompConfidence.HIGH,
            ARVConfidence.VERIFIED_SUPPORTED,
        )
        self.assertGreaterEqual(score.total, 75)
        self.assertIn(score.classification, (Classification.STRONG, Classification.HOT))

    def test_an_overpriced_deal_scores_poorly(self):
        lead = PropertyLead(
            address="x",
            asking_price=240_000,
            sqft=1_500,
            condition=Condition.HEAVY,
            property_type=PropertyType.SINGLE_FAMILY,
            seller_motivation=SellerMotivation.LOW,
        )
        score = self._score(
            lead, 250_000, 60_000, 97_000, 88_000, CompConfidence.NONE,
            ARVConfidence.USER_PROVIDED,
        )
        self.assertLess(score.total, 50)

    def test_component_weights_sum_to_one_hundred(self):
        lead = PropertyLead(address="x", asking_price=90_000)
        score = self._score(
            lead, 250_000, 30_000, 127_000, 118_000, CompConfidence.HIGH,
            ARVConfidence.VERIFIED_SUPPORTED,
        )
        self.assertAlmostEqual(sum(c.weight for c in score.components), 100.0)
        self.assertEqual(len(score.components), 9)

    def test_score_is_bounded(self):
        lead = PropertyLead(address="x", asking_price=1)
        score = self._score(
            lead, 250_000, 1, 200_000, 1, CompConfidence.HIGH, ARVConfidence.VERIFIED_SUPPORTED
        )
        self.assertLessEqual(score.total, 100.0)
        self.assertGreaterEqual(score.total, 0.0)

    def test_needs_more_data_flag_is_carried_through(self):
        lead = PropertyLead(address="x", asking_price=90_000)
        comps = CompAnalysis(confidence=CompConfidence.NONE)
        arv = ARVAssessment(arv=None, confidence=ARVConfidence.INSUFFICIENT_DATA, source_note="")
        repairs = RepairEstimate(
            None, None, None, None, RepairConfidence.INSUFFICIENT_DATA, ""
        )
        score = score_deal(lead, comps, arv, repairs, FinancialSummary(), needs_more_data=True)
        self.assertTrue(score.needs_more_data)


class ScoreCapTests(unittest.TestCase):
    def test_cap_lowers_and_reclassifies(self):
        lead = PropertyLead(address="x")
        score = score_deal(
            lead,
            CompAnalysis(),
            ARVAssessment(200_000, ARVConfidence.VERIFIED_SUPPORTED, ""),
            RepairEstimate(10_000, 10_000, 10_000, 10_000, RepairConfidence.USER_PROVIDED, ""),
            FinancialSummary(),
        )
        score.total = 88.0
        score.classification = Classification.STRONG
        capped = apply_score_cap(score, 39.0)
        self.assertEqual(capped.total, 39.0)
        self.assertEqual(capped.classification, Classification.PASS)

    def test_cap_does_not_raise_a_lower_score(self):
        lead = PropertyLead(address="x")
        score = score_deal(
            lead,
            CompAnalysis(),
            ARVAssessment(None, ARVConfidence.INSUFFICIENT_DATA, ""),
            RepairEstimate(None, None, None, None, RepairConfidence.INSUFFICIENT_DATA, ""),
            FinancialSummary(),
        )
        original = score.total
        self.assertEqual(apply_score_cap(score, 39.0).total, original)


if __name__ == "__main__":
    unittest.main()
