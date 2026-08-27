"""End-to-end tests: CSV in, analysis out, CSV back out.

These lock in the behaviours that protect the user from a bad deal: unverified
data cannot produce a GO, an overpriced lead cannot produce a GO, and the
engine never fabricates a value it was not given.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from wholesale_engine.analysis import analyze_properties, analyze_property
from wholesale_engine.data.csv_loader import load_properties_csv, to_date, to_float, to_list
from wholesale_engine.main import SAMPLE_COMPS, SAMPLE_PROPERTIES, run
from wholesale_engine.models import (
    ARVConfidence,
    Comp,
    Condition,
    Decision,
    Occupancy,
    PropertyLead,
    PropertyType,
    SaleStatus,
    SellerMotivation,
    Severity,
)
from wholesale_engine.reports import CSV_COLUMNS, render_result, write_csv

TODAY = date(2026, 8, 22)


class ParsingTests(unittest.TestCase):
    def test_money_strings(self):
        self.assertEqual(to_float("$185,000"), 185_000.0)
        self.assertEqual(to_float(" 42000 "), 42_000.0)
        self.assertIsNone(to_float(""))
        self.assertIsNone(to_float("unknown"))
        self.assertIsNone(to_float(None))

    def test_dates(self):
        self.assertEqual(to_date("2026-05-11"), date(2026, 5, 11))
        self.assertEqual(to_date("5/11/2026"), date(2026, 5, 11))
        self.assertIsNone(to_date("sometime last spring"))

    def test_multi_value_cells(self):
        self.assertEqual(to_list("probate; vacant"), ["probate", "vacant"])
        self.assertEqual(to_list(""), [])

    def test_enum_parsing_is_forgiving(self):
        self.assertEqual(Condition.parse("Needs Work"), Condition.MODERATE)
        self.assertEqual(Occupancy.parse("Tenant Occupied"), Occupancy.TENANT_OCCUPIED)
        self.assertEqual(PropertyType.parse("SFR"), PropertyType.SINGLE_FAMILY)
        self.assertEqual(SaleStatus.parse("Sold"), SaleStatus.CLOSED)
        self.assertEqual(SellerMotivation.parse("must sell"), SellerMotivation.HIGH)

    def test_unrecognised_values_become_unknown_not_a_guess(self):
        self.assertEqual(Condition.parse("purple"), Condition.UNKNOWN)
        self.assertEqual(PropertyType.parse(""), PropertyType.UNKNOWN)


class SampleDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report = load_properties_csv(SAMPLE_PROPERTIES, SAMPLE_COMPS)
        cls.report = report
        cls.results = {
            result.lead.property_id: result
            for result in analyze_properties(report.leads)
        }

    def test_all_sample_rows_load_without_warnings(self):
        self.assertEqual(self.report.warnings, [])
        self.assertGreaterEqual(len(self.report.leads), 5)

    def test_comps_are_joined_to_their_properties(self):
        lead = next(l for l in self.report.leads if l.property_id == "WS-001")
        self.assertEqual(len(lead.comps), 4)

    def test_well_documented_discounted_deal_is_a_go(self):
        result = self.results["WS-001"]
        self.assertEqual(result.decision, Decision.GO)
        self.assertEqual(result.arv.confidence, ARVConfidence.VERIFIED_SUPPORTED)
        self.assertGreater(result.financials.mao, result.lead.asking_price)

    def test_retail_priced_listing_is_a_pass(self):
        result = self.results["WS-002"]
        self.assertEqual(result.decision, Decision.PASS)
        self.assertTrue(any(f.code in ("overpriced", "asking_above_arv") for f in result.risk_flags))

    def test_empty_lead_cannot_be_underwritten(self):
        result = self.results["WS-003"]
        self.assertEqual(result.decision, Decision.NEED_MORE_DATA)
        self.assertEqual(result.arv.confidence, ARVConfidence.INSUFFICIENT_DATA)
        self.assertIsNone(result.financials.mao)
        self.assertIsNone(result.arv.arv)

    def test_inflated_arv_is_caught_and_the_deal_dies(self):
        result = self.results["WS-004"]
        self.assertTrue(any(f.code == "arv_conflict" for f in result.risk_flags))
        self.assertLess(result.arv.arv, result.lead.user_arv)
        self.assertEqual(result.decision, Decision.PASS)

    def test_unverified_arv_blocks_a_go_even_with_a_decent_score(self):
        result = self.results["WS-005"]
        self.assertEqual(result.arv.confidence, ARVConfidence.USER_PROVIDED)
        self.assertTrue(result.score.needs_more_data)
        self.assertEqual(result.decision, Decision.NEED_MORE_DATA)

    def test_mobile_home_risk_is_surfaced(self):
        result = self.results["WS-006"]
        self.assertTrue(any(f.code == "mobile_home" for f in result.risk_flags))

    def test_gap_above_mao_leads_to_negotiate(self):
        result = self.results["WS-007"]
        self.assertEqual(result.decision, Decision.NEGOTIATE)
        self.assertLess(result.financials.mao, result.lead.asking_price)


class SafetyRailTests(unittest.TestCase):
    def test_engine_never_invents_an_arv(self):
        result = analyze_property(PropertyLead(address="1 Nowhere Rd", asking_price=50_000))
        self.assertIsNone(result.arv.arv)
        self.assertIsNone(result.financials.mao)
        self.assertEqual(result.decision, Decision.NEED_MORE_DATA)

    def test_every_report_carries_the_no_guarantee_language(self):
        result = analyze_property(PropertyLead(address="1 Nowhere Rd", asking_price=50_000))
        text = " ".join(render_result(result).split())
        self.assertIn("No deal is guaranteed profitable", text)
        self.assertIn("has no access to public records", text)

    def test_missing_data_always_names_the_public_record_gap(self):
        result = analyze_property(PropertyLead(address="1 Nowhere Rd"))
        self.assertTrue(any("Title, lien" in gap for gap in result.missing_data))

    def test_user_distress_claims_are_labelled_as_unverified(self):
        lead = PropertyLead(
            address="1 Nowhere Rd",
            asking_price=50_000,
            distress_indicators=["pre-foreclosure"],
        )
        result = analyze_property(lead)
        flag = next(f for f in result.risk_flags if f.code == "distress_indicator")
        self.assertIn("has not verified it", flag.message)

    def test_negative_mao_produces_no_offer_and_a_critical_flag(self):
        lead = PropertyLead(
            address="1 Nowhere Rd",
            asking_price=90_000,
            user_arv=100_000,
            user_repair_estimate=80_000,
            condition=Condition.HEAVY,
            sqft=1_200,
        )
        result = analyze_property(lead)
        self.assertLess(result.financials.mao, 0)
        self.assertIsNone(result.financials.recommended_offer)
        self.assertTrue(any(f.severity is Severity.CRITICAL for f in result.risk_flags))
        self.assertEqual(result.decision, Decision.PASS)

    def test_recommended_offer_is_always_below_mao(self):
        lead = PropertyLead(
            address="1 Nowhere Rd",
            asking_price=200_000,
            sqft=1_500,
            beds=3,
            baths=2,
            year_built=1995,
            condition=Condition.COSMETIC,
            occupancy=Occupancy.VACANT,
            property_type=PropertyType.SINGLE_FAMILY,
            user_repair_estimate=25_000,
            comps=[
                Comp(
                    address=f"comp {i}",
                    sale_price=300_000,
                    sale_status=SaleStatus.CLOSED,
                    sale_date=date(2026, 7, 1),
                    beds=3,
                    baths=2,
                    sqft=1_500,
                    year_built=1995,
                    distance_miles=0.2,
                    property_type=PropertyType.SINGLE_FAMILY,
                )
                for i in range(3)
            ],
        )
        result = analyze_property(lead, as_of=TODAY)
        self.assertLess(result.financials.recommended_offer, result.financials.mao)
        self.assertGreaterEqual(result.financials.potential_gross_spread, 0)

    def test_assignment_price_equals_offer_plus_fee(self):
        result = analyze_property(
            PropertyLead(
                address="1 Nowhere Rd",
                asking_price=60_000,
                user_arv=200_000,
                user_repair_estimate=30_000,
                condition=Condition.COSMETIC,
                sqft=1_400,
            )
        )
        self.assertEqual(
            result.financials.assignment_price,
            result.financials.recommended_offer + 18_000,
        )


class CsvExportTests(unittest.TestCase):
    def test_export_columns_match_the_specification(self):
        results = analyze_properties(load_properties_csv(SAMPLE_PROPERTIES, SAMPLE_COMPS).leads)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(results, Path(tmp) / "out.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, CSV_COLUMNS)
                rows = list(reader)
        self.assertEqual(len(rows), len(results))
        self.assertTrue(all(row["address"] for row in rows))

    def test_unknown_values_export_as_blank_not_zero(self):
        results = [analyze_property(PropertyLead(address="1 Nowhere Rd"))]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(results, Path(tmp) / "out.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                row = next(iter(csv.DictReader(handle)))
        self.assertEqual(row["arv"], "")
        self.assertEqual(row["mao"], "")
        self.assertEqual(row["recommended_offer"], "")

    def test_detail_mode_adds_columns_without_dropping_the_core_set(self):
        results = [analyze_property(PropertyLead(address="1 Nowhere Rd", asking_price=1_000))]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(results, Path(tmp) / "out.csv", include_detail=True)
            with open(path, newline="", encoding="utf-8") as handle:
                fieldnames = csv.DictReader(handle).fieldnames
        self.assertEqual(fieldnames[: len(CSV_COLUMNS)], CSV_COLUMNS)
        self.assertIn("decision_explanation", fieldnames)


class CliTests(unittest.TestCase):
    def test_sample_run_exits_clean_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deals.csv"
            report = Path(tmp) / "deals.txt"
            code = run(["--sample", "--quiet", "--out", str(out), "--report", str(report)])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            self.assertGreater(report.stat().st_size, 1_000)

    def test_missing_input_file_is_reported_not_crashed(self):
        self.assertEqual(run(["--csv", "does_not_exist.csv", "--quiet"]), 2)

    def test_no_input_prints_usage(self):
        self.assertEqual(run(["--quiet"]), 2)

    def test_assumptions_are_overridable_from_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deals.csv"
            code = run(
                ["--sample", "--quiet", "--out", str(out), "--arv-pct", "65", "--fee", "25000"]
            )
            self.assertEqual(code, 0)
            with open(out, newline="", encoding="utf-8") as handle:
                rows = {r["address"]: r for r in csv.DictReader(handle)}
            # 412 Magnolia Ln: (215,000 x 0.65) - 42,000 - 25,000
            self.assertAlmostEqual(float(rows["412 Magnolia Ln"]["mao"]), 72_750.0, places=2)


class BadInputTests(unittest.TestCase):
    def test_a_row_without_an_address_is_skipped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text(
                "address,asking_price\n,50000\n123 Real St,60000\n", encoding="utf-8"
            )
            report = load_properties_csv(path)
        self.assertEqual(len(report.leads), 1)
        self.assertEqual(len(report.warnings), 1)

    def test_blank_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaps.csv"
            path.write_text("address,asking_price\n123 Real St,60000\n,\n", encoding="utf-8")
            report = load_properties_csv(path)
        self.assertEqual(len(report.leads), 1)
        self.assertEqual(report.warnings, [])

    def test_inline_comps_json_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inline.csv"
            path.write_text(
                'address,asking_price,comps_json\n'
                '"123 Real St",60000,'
                '"[{""address"": ""1 Comp St"", ""sale_price"": 200000, ""sale_status"": ""closed""}]"\n',
                encoding="utf-8",
            )
            report = load_properties_csv(path)
        self.assertEqual(len(report.leads), 1)
        self.assertEqual(len(report.leads[0].comps), 1)
        self.assertEqual(report.leads[0].comps[0].sale_price, 200_000)


if __name__ == "__main__":
    unittest.main()
