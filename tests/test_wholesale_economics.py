"""Wholesale economics: the fee, and what it takes to qualify as a GO.

The distinction these tests exist to protect:

* MAO already reserves the target fee, so **MAO - offer is cushion, not fee**.
* The fee a deal supports is measured against the END BUYER's ceiling
  (ARV x 70% - repairs), at the price actually available.
* A deal that cannot produce the target fee at the price on the table is not
  a GO, however good the rest of the analysis looks.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.analysis import analyze_property
from wholesale_engine.analysis import financials as fin
from wholesale_engine.config import DEFAULT_CONFIG, EngineConfig
from wholesale_engine.models import (
    Comp,
    Condition,
    Decision,
    Occupancy,
    PropertyLead,
    PropertyType,
    SaleStatus,
    SellerMotivation,
    WholesaleFeeStatus,
)
from wholesale_engine.reports import CSV_COLUMNS, render_result, write_csv

TARGET = DEFAULT_CONFIG.target_wholesale_fee


def supporting_comps(price: float, sqft: int, count: int = 3):
    """Comps strong enough to make the ARV VERIFIED/SUPPORTED."""
    from datetime import date

    return [
        Comp(
            address=f"comp {i}",
            sale_price=price,
            sale_status=SaleStatus.CLOSED,
            sale_date=date(2026, 7, 1),
            beds=3,
            baths=2,
            sqft=sqft,
            year_built=1995,
            distance_miles=0.2,
            property_type=PropertyType.SINGLE_FAMILY,
        )
        for i in range(count)
    ]


def deal(asking: float, arv: float = 300_000, repairs: float = 30_000, **overrides):
    """A clean, well-documented lead whose only variable is the asking price."""
    defaults = dict(
        address="1 Economics Way",
        city="Tampa",
        state="FL",
        county="Hillsborough",
        asking_price=asking,
        user_arv=arv,
        user_repair_estimate=repairs,
        beds=3,
        baths=2,
        sqft=1_500,
        lot_size_sqft=7_000,
        year_built=1995,
        condition=Condition.COSMETIC,
        occupancy=Occupancy.VACANT,
        property_type=PropertyType.SINGLE_FAMILY,
        seller_motivation=SellerMotivation.HIGH,
        days_on_market=95,
        estimated_monthly_rent=2_200,
        comps=supporting_comps(arv, 1_500),
    )
    defaults.update(overrides)
    return PropertyLead(**defaults)


# ---------------------------------------------------------------------------
# 4. MAO calculation
# ---------------------------------------------------------------------------


class MAOTests(unittest.TestCase):
    def test_mao_reserves_the_target_fee(self):
        # (300,000 x 0.70) - 30,000 - 18,000
        self.assertEqual(fin.maximum_allowable_offer(300_000, 30_000), 162_000)

    def test_mao_is_the_end_buyer_ceiling_less_the_target_fee(self):
        ceiling = fin.end_buyer_max_price(300_000, 30_000)
        self.assertEqual(ceiling, 180_000)  # (300,000 x 0.70) - 30,000
        self.assertEqual(fin.maximum_allowable_offer(300_000, 30_000), ceiling - TARGET)

    def test_the_seventy_percent_rule_stays_configurable(self):
        config = EngineConfig(arv_percentage=0.65)
        self.assertEqual(fin.end_buyer_max_price(300_000, 30_000, config), 165_000)
        self.assertEqual(fin.maximum_allowable_offer(300_000, 30_000, config), 147_000)

    def test_the_target_fee_stays_configurable(self):
        config = EngineConfig(target_wholesale_fee=25_000)
        self.assertEqual(fin.maximum_allowable_offer(300_000, 30_000, config), 155_000)

    def test_analyzer_uses_the_same_formula(self):
        result = analyze_property(deal(asking=150_000))
        self.assertAlmostEqual(
            result.financials.mao,
            fin.maximum_allowable_offer(result.arv.arv, result.repairs.base),
            places=2,
        )


# ---------------------------------------------------------------------------
# 5. Recommended offer  /  6. Assignment price
# ---------------------------------------------------------------------------


class OfferAndAssignmentTests(unittest.TestCase):
    def test_recommended_offer_never_exceeds_mao(self):
        result = analyze_property(deal(asking=150_000))
        self.assertLessEqual(result.financials.recommended_offer, result.financials.mao)

    def test_recommended_offer_never_exceeds_asking(self):
        result = analyze_property(deal(asking=120_000))
        self.assertLessEqual(result.financials.recommended_offer, 120_000)

    def test_assignment_price_is_offer_plus_target_fee(self):
        result = analyze_property(deal(asking=150_000))
        financials = result.financials
        self.assertEqual(
            financials.assignment_price, financials.recommended_offer + TARGET
        )

    def test_assignment_price_uses_the_configured_fee(self):
        config = EngineConfig(target_wholesale_fee=25_000)
        result = analyze_property(deal(asking=150_000), config)
        self.assertEqual(
            result.financials.assignment_price,
            result.financials.recommended_offer + 25_000,
        )

    def test_cushion_is_reported_separately_from_the_fee(self):
        result = analyze_property(deal(asking=150_000))
        financials = result.financials
        cushion = financials.mao - financials.recommended_offer
        self.assertEqual(financials.potential_gross_spread, cushion)
        # The fee is a different, larger number: the cushion sits on top of it.
        self.assertEqual(financials.potential_wholesale_fee, cushion + TARGET)
        self.assertNotEqual(financials.potential_wholesale_fee, cushion)

    def test_buyer_margin_is_room_left_for_the_end_buyer(self):
        result = analyze_property(deal(asking=150_000))
        financials = result.financials
        self.assertEqual(
            financials.buyer_margin,
            financials.end_buyer_max_price - financials.assignment_price,
        )


# ---------------------------------------------------------------------------
# 1-3, 7. Fee status: meets / below / unknown
# ---------------------------------------------------------------------------


class WholesaleFeeStatusTests(unittest.TestCase):
    def test_classifier_at_and_around_the_target(self):
        self.assertIs(fin.classify_wholesale_fee(TARGET), WholesaleFeeStatus.MEETS_TARGET)
        self.assertIs(
            fin.classify_wholesale_fee(TARGET + 1), WholesaleFeeStatus.MEETS_TARGET
        )
        self.assertIs(
            fin.classify_wholesale_fee(TARGET - 1), WholesaleFeeStatus.BELOW_TARGET
        )
        self.assertIs(fin.classify_wholesale_fee(None), WholesaleFeeStatus.UNKNOWN)

    def test_deal_meeting_the_target(self):
        # Asking at MAO exactly: the fee lands precisely on target.
        mao = fin.maximum_allowable_offer(300_000, 30_000)  # 162,000
        result = analyze_property(deal(asking=mao))
        financials = result.financials
        self.assertEqual(financials.wholesale_fee_at_asking, TARGET)
        self.assertIs(financials.wholesale_fee_status, WholesaleFeeStatus.MEETS_TARGET)

    def test_deal_comfortably_above_the_target(self):
        result = analyze_property(deal(asking=140_000))
        financials = result.financials
        self.assertGreater(financials.wholesale_fee_at_asking, TARGET)
        self.assertIs(financials.wholesale_fee_status, WholesaleFeeStatus.MEETS_TARGET)

    def test_deal_below_the_target(self):
        # Asking above MAO: the achievable fee is squeezed under the target.
        result = analyze_property(deal(asking=175_000))
        financials = result.financials
        self.assertLess(financials.wholesale_fee_at_asking, TARGET)
        self.assertIs(financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET)

    def test_below_target_raises_the_named_risk_flag(self):
        result = analyze_property(deal(asking=175_000))
        flag = next(
            f for f in result.risk_flags if f.code == "below_target_wholesale_fee"
        )
        self.assertIn("BELOW TARGET WHOLESALE FEE", flag.message)

    def test_meeting_the_target_raises_no_fee_flag(self):
        result = analyze_property(deal(asking=140_000))
        self.assertFalse(
            any(f.code == "below_target_wholesale_fee" for f in result.risk_flags)
        )

    def test_unknown_when_the_economics_cannot_be_computed(self):
        # No ARV and no comps: there is nothing to measure a fee against.
        result = analyze_property(PropertyLead(address="1 Nowhere Rd", asking_price=50_000))
        self.assertIs(result.financials.wholesale_fee_status, WholesaleFeeStatus.UNKNOWN)
        self.assertIsNone(result.financials.potential_wholesale_fee)

    def test_unknown_when_repairs_cannot_be_estimated(self):
        result = analyze_property(
            PropertyLead(address="1 Nowhere Rd", asking_price=50_000, user_arv=200_000)
        )
        self.assertIs(result.financials.wholesale_fee_status, WholesaleFeeStatus.UNKNOWN)

    def test_the_fee_is_judged_at_the_asking_price_not_the_wished_for_offer(self):
        # The recommended offer would clear the target, but the seller is asking
        # far more — the deal must be judged at the price actually on the table.
        result = analyze_property(deal(asking=175_000))
        financials = result.financials
        self.assertGreater(financials.potential_wholesale_fee, TARGET)
        self.assertEqual(financials.binding_wholesale_fee, financials.wholesale_fee_at_asking)
        self.assertIs(financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET)

    def test_the_bar_is_configurable(self):
        config = EngineConfig(target_wholesale_fee=40_000)
        result = analyze_property(deal(asking=150_000), config)
        self.assertIs(result.financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET)

    def test_an_extra_cushion_requirement_can_be_demanded(self):
        # Opt-in stricter rule: clear the fee AND the same again in cushion.
        strict = EngineConfig(min_cushion_above_target=TARGET)
        self.assertEqual(strict.required_wholesale_fee, TARGET * 2)
        # Fee at this asking price is $30,000: clears $18,000, not $36,000.
        relaxed_result = analyze_property(deal(asking=150_000))
        strict_result = analyze_property(deal(asking=150_000), strict)
        self.assertIs(
            relaxed_result.financials.wholesale_fee_status, WholesaleFeeStatus.MEETS_TARGET
        )
        self.assertIs(
            strict_result.financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET
        )


# ---------------------------------------------------------------------------
# 8. GO requires sufficient wholesale economics
# ---------------------------------------------------------------------------


class GoRequiresEconomicsTests(unittest.TestCase):
    def test_a_deal_clearing_the_target_can_be_a_go(self):
        result = analyze_property(deal(asking=140_000))
        self.assertIs(result.financials.wholesale_fee_status, WholesaleFeeStatus.MEETS_TARGET)
        self.assertEqual(result.decision, Decision.GO)

    def test_a_deal_below_the_target_is_never_a_go(self):
        result = analyze_property(deal(asking=175_000))
        self.assertIs(result.financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET)
        self.assertNotEqual(result.decision, Decision.GO)
        self.assertIn(result.decision, (Decision.NEGOTIATE, Decision.PASS))

    def test_raising_the_target_can_turn_a_go_into_a_negotiation(self):
        lead_args = dict(asking=140_000)
        self.assertEqual(analyze_property(deal(**lead_args)).decision, Decision.GO)
        demanding = EngineConfig(target_wholesale_fee=45_000)
        result = analyze_property(deal(**lead_args), demanding)
        self.assertNotEqual(result.decision, Decision.GO)

    def test_no_go_is_ever_issued_below_target(self):
        # Sweep the asking price across the whole range: every GO must clear.
        for asking in range(100_000, 200_001, 5_000):
            result = analyze_property(deal(asking=asking))
            if result.decision is Decision.GO:
                self.assertIs(
                    result.financials.wholesale_fee_status,
                    WholesaleFeeStatus.MEETS_TARGET,
                    f"GO issued at asking {asking:,} without clearing the fee target",
                )
                self.assertGreaterEqual(result.financials.binding_wholesale_fee, TARGET)

    def test_the_go_explanation_states_the_achievable_fee(self):
        result = analyze_property(deal(asking=140_000))
        self.assertIn("assignment fee", result.decision_explanation)

    def test_the_negotiate_explanation_names_the_shortfall(self):
        result = analyze_property(deal(asking=175_000))
        self.assertIn("target", result.decision_explanation.lower())


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class FeeReportingTests(unittest.TestCase):
    def test_the_report_shows_the_three_fee_lines(self):
        text = render_result(analyze_property(deal(asking=175_000)))
        flat = " ".join(text.split())
        self.assertIn("Target Wholesale Fee: $18,000", flat)
        self.assertIn("Potential Wholesale Fee:", flat)
        self.assertIn("Wholesale Fee Status: BELOW TARGET", flat)

    def test_the_report_labels_cushion_as_cushion(self):
        flat = " ".join(render_result(analyze_property(deal(asking=140_000))).split())
        self.assertIn("Deal Cushion (MAO - Offer)", flat)
        self.assertIn("Wholesale Fee Status: MEETS TARGET", flat)

    def test_the_csv_carries_the_three_fee_columns(self):
        for column in (
            "target_wholesale_fee",
            "potential_wholesale_fee",
            "wholesale_fee_status",
        ):
            self.assertIn(column, CSV_COLUMNS)

    def test_csv_values_round_trip(self):
        results = [analyze_property(deal(asking=175_000)), analyze_property(deal(asking=140_000))]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(results, Path(tmp) / "out.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["target_wholesale_fee"], "18000.0")
        self.assertEqual(rows[0]["wholesale_fee_status"], "BELOW TARGET")
        self.assertEqual(rows[1]["wholesale_fee_status"], "MEETS TARGET")

    def test_unknown_economics_export_as_unknown(self):
        results = [analyze_property(PropertyLead(address="1 Nowhere Rd", asking_price=50_000))]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(results, Path(tmp) / "out.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                row = next(iter(csv.DictReader(handle)))
        self.assertEqual(row["wholesale_fee_status"], "UNKNOWN")
        self.assertEqual(row["potential_wholesale_fee"], "")


class LeadPipelineEconomicsTests(unittest.TestCase):
    """The Wave 2 pipeline must inherit the same discipline."""

    @classmethod
    def setUpClass(cls):
        from wholesale_engine.lead_hunter import run_from_csv
        from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS

        cls.report = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)
        cls.by_id = {r.lead.lead_id: r for r in cls.report.results}

    def test_no_lead_is_a_go_without_clearing_the_fee_target(self):
        for result in self.report.results:
            if result.analysis is not None and result.analysis.decision is Decision.GO:
                self.assertIs(
                    result.analysis.financials.wholesale_fee_status,
                    WholesaleFeeStatus.MEETS_TARGET,
                    result.lead.address,
                )

    def test_a_lead_short_of_the_fee_is_flagged_and_downgraded(self):
        result = self.by_id["LH-009"]  # asking $148k against a $132.8k MAO
        financials = result.analysis.financials
        self.assertIs(financials.wholesale_fee_status, WholesaleFeeStatus.BELOW_TARGET)
        self.assertLess(financials.wholesale_fee_at_asking, TARGET)
        self.assertNotEqual(result.analysis.decision, Decision.GO)
        self.assertTrue(
            any(f.code == "below_target_wholesale_fee" for f in result.analysis.risk_flags)
        )

    def test_the_pipeline_csv_carries_the_fee_columns(self):
        from wholesale_engine.reports import LEAD_PIPELINE_COLUMNS, write_lead_pipeline_csv

        for column in (
            "target_wholesale_fee",
            "potential_wholesale_fee",
            "wholesale_fee_status",
        ):
            self.assertIn(column, LEAD_PIPELINE_COLUMNS)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_lead_pipeline_csv(self.report, Path(tmp) / "p.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                rows = {r["lead_id"]: r for r in csv.DictReader(handle)}
        self.assertEqual(rows["LH-009"]["wholesale_fee_status"], "BELOW TARGET")
        self.assertEqual(rows["LH-011"]["wholesale_fee_status"], "MEETS TARGET")


if __name__ == "__main__":
    unittest.main()
