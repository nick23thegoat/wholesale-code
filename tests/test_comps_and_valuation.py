"""Tests for comp grading, comp-derived ARV, and ARV reconciliation."""

from __future__ import annotations

import unittest
from datetime import date

from wholesale_engine.analysis.comps import analyze_comps, evaluate_comp
from wholesale_engine.analysis.valuation import assess_arv
from wholesale_engine.models import (
    ARVConfidence,
    Comp,
    CompConfidence,
    PropertyLead,
    PropertyType,
    SaleStatus,
)

TODAY = date(2026, 8, 22)


def subject(**overrides) -> PropertyLead:
    defaults = dict(
        address="100 Test St",
        beds=3,
        baths=2,
        sqft=1_500,
        year_built=1990,
        property_type=PropertyType.SINGLE_FAMILY,
    )
    defaults.update(overrides)
    return PropertyLead(**defaults)


def good_comp(price: float = 300_000, **overrides) -> Comp:
    defaults = dict(
        address="102 Test St",
        sale_price=price,
        sale_status=SaleStatus.CLOSED,
        sale_date=date(2026, 7, 1),
        beds=3,
        baths=2,
        sqft=1_500,
        year_built=1990,
        distance_miles=0.2,
        property_type=PropertyType.SINGLE_FAMILY,
    )
    defaults.update(overrides)
    return Comp(**defaults)


class CompGradingTests(unittest.TestCase):
    def test_a_near_identical_closed_sale_scores_high(self):
        evaluation = evaluate_comp(subject(), good_comp(), as_of=TODAY)
        self.assertGreater(evaluation.quality_score, 0.90)
        self.assertTrue(evaluation.reliable)
        self.assertEqual(evaluation.grade, "A")

    def test_active_listings_score_below_closed_sales(self):
        closed = evaluate_comp(subject(), good_comp(), as_of=TODAY)
        active = evaluate_comp(
            subject(), good_comp(sale_status=SaleStatus.ACTIVE, sale_date=None), as_of=TODAY
        )
        self.assertLess(active.quality_score, closed.quality_score)

    def test_a_comp_without_a_price_is_never_used(self):
        evaluation = evaluate_comp(subject(), good_comp(sale_price=None), as_of=TODAY)
        self.assertFalse(evaluation.reliable)
        self.assertEqual(evaluation.quality_score, 0.0)

    def test_distance_reduces_quality(self):
        near = evaluate_comp(subject(), good_comp(distance_miles=0.2), as_of=TODAY)
        far = evaluate_comp(subject(), good_comp(distance_miles=1.9), as_of=TODAY)
        self.assertLess(far.quality_score, near.quality_score)

    def test_stale_sales_lose_recency_points(self):
        fresh = evaluate_comp(subject(), good_comp(sale_date=date(2026, 8, 1)), as_of=TODAY)
        stale = evaluate_comp(subject(), good_comp(sale_date=date(2025, 6, 1)), as_of=TODAY)
        self.assertLess(stale.quality_score, fresh.quality_score)

    def test_different_property_type_is_penalised(self):
        mismatch = evaluate_comp(
            subject(), good_comp(property_type=PropertyType.CONDO), as_of=TODAY
        )
        self.assertLess(mismatch.quality_score, 0.90)
        self.assertTrue(any("different property type" in r for r in mismatch.reasons))

    def test_size_gap_is_penalised_and_explained(self):
        mismatch = evaluate_comp(subject(), good_comp(sqft=900), as_of=TODAY)
        self.assertTrue(any("size gap" in r for r in mismatch.reasons))


class CompDerivedARVTests(unittest.TestCase):
    def test_no_comps_yields_no_arv(self):
        analysis = analyze_comps(subject(), as_of=TODAY)
        self.assertIsNone(analysis.comp_derived_arv)
        self.assertEqual(analysis.confidence, CompConfidence.NONE)

    def test_three_strong_comps_give_high_confidence(self):
        lead = subject(
            comps=[
                good_comp(300_000, address="A"),
                good_comp(306_000, address="B", sqft=1_530),
                good_comp(294_000, address="C", sqft=1_470),
            ]
        )
        analysis = analyze_comps(lead, as_of=TODAY)
        self.assertEqual(analysis.confidence, CompConfidence.HIGH)
        self.assertEqual(analysis.reliable_count, 3)
        self.assertAlmostEqual(analysis.comp_derived_arv, 300_000, delta=2_000)

    def test_weak_comps_are_excluded_from_the_valuation(self):
        lead = subject(
            comps=[
                good_comp(300_000, address="A"),
                Comp(
                    address="Junk",
                    sale_price=520_000,
                    sale_status=SaleStatus.ACTIVE,
                    beds=6,
                    baths=4,
                    sqft=4_000,
                    year_built=1930,
                    distance_miles=5.0,
                    property_type=PropertyType.MULTI_FAMILY,
                ),
            ]
        )
        analysis = analyze_comps(lead, as_of=TODAY)
        self.assertEqual(analysis.reliable_count, 1)
        self.assertAlmostEqual(analysis.comp_derived_arv, 300_000, delta=1_000)

    def test_dispersed_comps_are_flagged(self):
        lead = subject(
            comps=[
                good_comp(240_000, address="A"),
                good_comp(360_000, address="B"),
                good_comp(300_000, address="C"),
            ]
        )
        analysis = analyze_comps(lead, as_of=TODAY)
        self.assertTrue(any("does not agree on value" in note for note in analysis.notes))


class ARVReconciliationTests(unittest.TestCase):
    def _analysis(self, lead: PropertyLead):
        return analyze_comps(lead, as_of=TODAY)

    def test_no_arv_and_no_comps_is_insufficient_data(self):
        lead = subject()
        assessment, flags = assess_arv(lead, self._analysis(lead))
        self.assertEqual(assessment.confidence, ARVConfidence.INSUFFICIENT_DATA)
        self.assertIsNone(assessment.arv)
        self.assertTrue(any(f.code == "no_arv" for f in flags))

    def test_user_arv_without_comps_stays_unverified(self):
        lead = subject(user_arv=300_000)
        assessment, flags = assess_arv(lead, self._analysis(lead))
        self.assertEqual(assessment.confidence, ARVConfidence.USER_PROVIDED)
        self.assertEqual(assessment.arv, 300_000)
        self.assertTrue(any(f.code == "unverified_arv" for f in flags))

    def test_supporting_comps_verify_the_user_arv(self):
        lead = subject(
            user_arv=300_000,
            comps=[
                good_comp(300_000, address="A"),
                good_comp(303_000, address="B", sqft=1_515),
                good_comp(297_000, address="C", sqft=1_485),
            ],
        )
        assessment, _ = assess_arv(lead, self._analysis(lead))
        self.assertEqual(assessment.confidence, ARVConfidence.VERIFIED_SUPPORTED)
        self.assertAlmostEqual(assessment.arv, 300_000, delta=2_000)

    def test_inflated_user_arv_is_flagged_and_overridden(self):
        lead = subject(
            user_arv=420_000,
            comps=[
                good_comp(300_000, address="A"),
                good_comp(306_000, address="B", sqft=1_530),
                good_comp(294_000, address="C", sqft=1_470),
            ],
        )
        assessment, flags = assess_arv(lead, self._analysis(lead))
        self.assertTrue(any(f.code == "arv_conflict" for f in flags))
        self.assertLess(assessment.arv, 420_000)
        self.assertNotEqual(assessment.confidence, ARVConfidence.VERIFIED_SUPPORTED)

    def test_the_engine_underwrites_the_conservative_number(self):
        lead = subject(
            user_arv=280_000,
            comps=[
                good_comp(300_000, address="A"),
                good_comp(306_000, address="B", sqft=1_530),
                good_comp(294_000, address="C", sqft=1_470),
            ],
        )
        assessment, _ = assess_arv(lead, self._analysis(lead))
        self.assertEqual(assessment.arv, 280_000)


if __name__ == "__main__":
    unittest.main()
