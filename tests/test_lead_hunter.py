"""Wave 2 tests: normalization, dedupe, lead scoring, filtering, CSV loading,
the pipeline, and its integration with the Wave 1 analyzer.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from wholesale_engine.config import DEFAULT_LEAD_CONFIG, LeadHunterConfig
from wholesale_engine.data.sources import NotConfiguredError
from wholesale_engine.lead_hunter import (
    Lead,
    apply_filters,
    classify_lead,
    deduplicate,
    hot_leads,
    merge_leads,
    normalize_address,
    normalize_city,
    normalize_lead,
    normalize_state,
    normalize_zip,
    prioritize,
    run_from_csv,
    run_lead_pipeline,
    score_lead,
    skip_trace_candidates,
    with_overrides,
)
from wholesale_engine.lead_hunter.models import (
    ARV_COMP_SUPPORTED,
    ARV_SOURCE_PROVIDED,
    STATUS_ANALYZED,
    STATUS_BELOW_DEAL_SCORE,
    STATUS_FILTERED,
)
from wholesale_engine.lead_hunter.sources import (
    ApiLeadSourceTemplate,
    CsvLeadSource,
    lead_from_row,
    to_tri_bool,
)
from wholesale_engine.lead_hunter.skip_trace import UnconfiguredSkipTraceProvider, build_request
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS, run
from wholesale_engine.models import (
    Classification,
    Condition,
    Decision,
    Occupancy,
    PropertyType,
    SellerMotivation,
)
from wholesale_engine.reports import (
    LEAD_PIPELINE_COLUMNS,
    render_lead_summary,
    write_hot_leads_csv,
    write_lead_pipeline_csv,
)


def lead(**overrides) -> Lead:
    """A minimal in-target lead; override whatever the test is about."""
    defaults = dict(
        lead_id="T-1",
        address="123 Main Street",
        city="Tampa",
        state="FL",
        property_type=PropertyType.SINGLE_FAMILY,
    )
    defaults.update(overrides)
    return Lead(**defaults)


# ---------------------------------------------------------------------------
# Address normalization
# ---------------------------------------------------------------------------


class AddressNormalizationTests(unittest.TestCase):
    def test_the_same_street_in_three_formats_normalizes_identically(self):
        forms = ["123 Main Street", "123 Main St.", "123 MAIN ST", "  123   main   street "]
        normalized = {normalize_address(form) for form in forms}
        self.assertEqual(normalized, {"123 MAIN ST"})

    def test_every_required_suffix_is_abbreviated(self):
        cases = {
            "1 Oak Street": "1 OAK ST",
            "1 Oak Avenue": "1 OAK AVE",
            "1 Oak Road": "1 OAK RD",
            "1 Oak Drive": "1 OAK DR",
            "1 Oak Lane": "1 OAK LN",
            "1 Oak Court": "1 OAK CT",
            "1 Oak Circle": "1 OAK CIR",
            "1 Oak Highway": "1 OAK HWY",
            "1 Oak Boulevard": "1 OAK BLVD",
            "1 Oak Parkway": "1 OAK PKWY",
            "1 Oak Place": "1 OAK PL",
            "1 Oak Terrace": "1 OAK TER",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_address(raw), expected, raw)

    def test_directionals_are_abbreviated(self):
        self.assertEqual(normalize_address("412 North Magnolia Avenue"), "412 N MAGNOLIA AVE")
        self.assertEqual(normalize_address("7 Elm St West"), "7 ELM ST W")

    def test_unit_numbers_are_preserved_so_units_never_merge(self):
        first = normalize_address("3005 Palmetto Street #1")
        second = normalize_address("3005 Palmetto Street #2")
        self.assertNotEqual(first, second)
        self.assertIn("UNIT 1", first)

    def test_blank_input_normalizes_to_blank(self):
        self.assertEqual(normalize_address(""), "")
        self.assertEqual(normalize_address(None), "")

    def test_city_state_and_zip_normalization(self):
        self.assertEqual(normalize_city(" st. petersburg "), "ST PETERSBURG")
        self.assertEqual(normalize_state("florida"), "FL")
        self.assertEqual(normalize_state("fl"), "FL")
        self.assertEqual(normalize_state(""), "")
        self.assertEqual(normalize_zip("33606-1234"), "33606")
        self.assertEqual(normalize_zip("abc"), "")

    def test_normalize_lead_populates_the_comparison_fields(self):
        item = normalize_lead(lead(address="123 Main Street", zip_code="33606-9999"))
        self.assertEqual(item.normalized_address, "123 MAIN ST")
        self.assertEqual(item.normalized_state, "FL")
        self.assertEqual(item.zip_code, "33606")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class DeduplicationTests(unittest.TestCase):
    def test_formatting_variants_of_one_property_collapse(self):
        unique, removed = deduplicate([
            lead(lead_id="A", address="123 Main Street"),
            lead(lead_id="B", address="123 Main St."),
        ])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(removed), 1)
        self.assertIn("B", unique[0].merged_from)

    def test_different_properties_are_never_merged(self):
        unique, _ = deduplicate([
            lead(lead_id="A", address="123 Main Street"),
            lead(lead_id="B", address="125 Main Street"),
            lead(lead_id="C", address="123 Main Street", city="Orlando"),
        ])
        self.assertEqual(len(unique), 3)

    def test_separate_units_are_never_merged(self):
        unique, _ = deduplicate([
            lead(lead_id="A", address="3005 Palmetto St #1"),
            lead(lead_id="B", address="3005 Palmetto St #2"),
        ])
        self.assertEqual(len(unique), 2)

    def test_conflicting_zips_keep_the_rows_apart(self):
        unique, _ = deduplicate([
            lead(lead_id="A", address="123 Main St", zip_code="33606"),
            lead(lead_id="B", address="123 Main St", zip_code="33607"),
        ])
        self.assertEqual(len(unique), 2)

    def test_a_blank_zip_does_not_block_a_merge(self):
        unique, _ = deduplicate([
            lead(lead_id="A", address="123 Main St", zip_code="33606"),
            lead(lead_id="B", address="123 Main St", zip_code=""),
        ])
        self.assertEqual(len(unique), 1)

    def test_leads_without_an_address_stay_separate(self):
        unique, removed = deduplicate([lead(lead_id="A", address=""), lead(lead_id="B", address="")])
        self.assertEqual(len(unique), 2)
        self.assertEqual(removed, [])

    def test_merging_fills_blanks_without_overwriting_known_values(self):
        primary = lead(lead_id="A", asking_price=100_000, county="", vacant=None)
        duplicate = lead(lead_id="B", asking_price=999_000, county="Hillsborough", vacant=True)
        merged = merge_leads(primary, duplicate)
        self.assertEqual(merged.asking_price, 100_000)  # kept
        self.assertEqual(merged.county, "Hillsborough")  # filled
        self.assertTrue(merged.vacant)  # filled

    def test_conflicting_signals_are_flagged_for_verification(self):
        merged = merge_leads(
            lead(lead_id="A", vacant=False), lead(lead_id="B", vacant=True)
        )
        self.assertTrue(merged.vacant)
        self.assertTrue(any("disagree" in note for note in merged.needs_verification))


# ---------------------------------------------------------------------------
# Lead scoring and classification
# ---------------------------------------------------------------------------


class LeadScoringTests(unittest.TestCase):
    def test_no_signals_scores_zero_and_passes(self):
        score = score_lead(lead())
        self.assertEqual(score.total, 0.0)
        self.assertEqual(score.classification, Classification.PASS)

    def test_individual_signal_values(self):
        cases = {
            "absentee_owner": 10.0,
            "vacant": 10.0,
            "tax_delinquent": 10.0,
            "pre_foreclosure": 15.0,
            "foreclosure": 15.0,
            "probate": 10.0,
            "inherited": 10.0,
            "code_violation": 10.0,
            "tired_landlord": 10.0,
        }
        for signal, points in cases.items():
            score = score_lead(lead(**{signal: True}))
            self.assertEqual(score.total, points, signal)

    def test_signals_accumulate(self):
        score = score_lead(lead(absentee_owner=True, vacant=True, tax_delinquent=True))
        self.assertEqual(score.total, 30.0)

    def test_score_is_capped_at_one_hundred(self):
        score = score_lead(
            lead(
                absentee_owner=True, vacant=True, tax_delinquent=True, foreclosure=True,
                probate=True, code_violation=True, tired_landlord=True, high_equity=True,
                estimated_repairs=60_000, seller_motivation=SellerMotivation.HIGH,
            )
        )
        self.assertEqual(score.total, 100.0)

    def test_related_signals_are_not_double_counted(self):
        both = score_lead(lead(pre_foreclosure=True, foreclosure=True))
        one = score_lead(lead(foreclosure=True))
        self.assertEqual(both.total, one.total)
        self.assertIn("pre_foreclosure", both.suppressed)

    def test_probate_and_inherited_count_once(self):
        both = score_lead(lead(probate=True, inherited=True))
        self.assertEqual(both.total, 10.0)

    def test_unknown_signals_score_nothing_and_are_listed(self):
        score = score_lead(lead(vacant=None, probate=None))
        self.assertEqual(score.total, 0.0)
        self.assertIn("vacant", score.unknown_signals)

    def test_explicit_no_scores_nothing(self):
        self.assertEqual(score_lead(lead(vacant=False, probate=False)).total, 0.0)

    def test_motivation_scores_only_when_reported(self):
        self.assertEqual(score_lead(lead(seller_motivation=SellerMotivation.HIGH)).total, 15.0)
        self.assertEqual(score_lead(lead(seller_motivation=SellerMotivation.MODERATE)).total, 7.0)
        self.assertEqual(score_lead(lead(seller_motivation=SellerMotivation.LOW)).total, 0.0)
        self.assertEqual(score_lead(lead(seller_motivation=SellerMotivation.UNKNOWN)).total, 0.0)

    def test_high_equity_is_taken_from_the_source_when_reported(self):
        self.assertEqual(score_lead(lead(high_equity=True)).total, 15.0)

    def test_high_equity_can_be_derived_from_supplied_figures(self):
        score = score_lead(lead(estimated_value=200_000, asking_price=100_000))
        self.assertEqual(score.total, 15.0)
        hit = next(h for h in score.hits if h.name == "high_equity")
        self.assertIn("derived", hit.basis)

    def test_thin_equity_is_not_called_high_equity(self):
        self.assertEqual(score_lead(lead(estimated_value=200_000, asking_price=190_000)).total, 0.0)

    def test_significant_repairs_from_a_dollar_figure_or_condition(self):
        self.assertEqual(score_lead(lead(estimated_repairs=30_000)).total, 10.0)
        self.assertEqual(score_lead(lead(condition=Condition.HEAVY)).total, 10.0)
        self.assertEqual(score_lead(lead(estimated_repairs=5_000)).total, 0.0)

    def test_scoring_weights_are_configurable(self):
        config = replace(
            DEFAULT_LEAD_CONFIG, signal_points={**DEFAULT_LEAD_CONFIG.signal_points, "vacant": 40.0}
        )
        self.assertEqual(score_lead(lead(vacant=True), config).total, 40.0)


class LeadClassificationTests(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(classify_lead(100), Classification.HOT)
        self.assertEqual(classify_lead(90), Classification.HOT)
        self.assertEqual(classify_lead(89.9), Classification.STRONG)
        self.assertEqual(classify_lead(75), Classification.STRONG)
        self.assertEqual(classify_lead(74.9), Classification.POSSIBLE)
        self.assertEqual(classify_lead(60), Classification.POSSIBLE)
        self.assertEqual(classify_lead(59.9), Classification.WEAK)
        self.assertEqual(classify_lead(40), Classification.WEAK)
        self.assertEqual(classify_lead(39.9), Classification.PASS)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def outcome_for(item: Lead, config: LeadHunterConfig = DEFAULT_LEAD_CONFIG):
    return apply_filters(item, score_lead(item, config), config)


class StateFilterTests(unittest.TestCase):
    def test_target_states_pass(self):
        for state in ("FL", "TX", "MO"):
            self.assertTrue(outcome_for(lead(state=state)).passed, state)

    def test_non_target_state_is_rejected(self):
        result = outcome_for(lead(state="OH"))
        self.assertFalse(result.passed)
        self.assertTrue(any("outside the target markets" in r for r in result.reasons))

    def test_unknown_state_is_a_warning_not_a_rejection(self):
        result = outcome_for(lead(state=""))
        self.assertTrue(result.passed)
        self.assertTrue(any("state unknown" in w for w in result.warnings))

    def test_states_are_configurable_not_hard_coded(self):
        config = replace(DEFAULT_LEAD_CONFIG, target_states=("GA", "SC"))
        self.assertTrue(outcome_for(lead(state="GA"), config).passed)
        self.assertFalse(outcome_for(lead(state="FL"), config).passed)

    def test_an_expansion_state_is_one_config_change_away(self):
        config = replace(
            DEFAULT_LEAD_CONFIG, target_states=DEFAULT_LEAD_CONFIG.target_states + ("TN",)
        )
        self.assertTrue(outcome_for(lead(state="TN"), config).passed)


class PropertyTypeFilterTests(unittest.TestCase):
    def test_default_types_pass(self):
        for property_type in (
            PropertyType.SINGLE_FAMILY,
            PropertyType.DUPLEX,
            PropertyType.TRIPLEX,
            PropertyType.FOURPLEX,
        ):
            self.assertTrue(outcome_for(lead(property_type=property_type)).passed, property_type)

    def test_commercial_and_land_are_not_included_by_default(self):
        for property_type in (PropertyType.COMMERCIAL, PropertyType.LAND):
            result = outcome_for(lead(property_type=property_type))
            self.assertFalse(result.passed, property_type)

    def test_unknown_type_is_a_warning_not_a_rejection(self):
        result = outcome_for(lead(property_type=PropertyType.UNKNOWN))
        self.assertTrue(result.passed)
        self.assertTrue(any("property type unknown" in w for w in result.warnings))

    def test_property_types_are_configurable(self):
        config = replace(DEFAULT_LEAD_CONFIG, preferred_property_types=("condo",))
        self.assertTrue(outcome_for(lead(property_type=PropertyType.CONDO), config).passed)
        self.assertFalse(outcome_for(lead(property_type=PropertyType.SINGLE_FAMILY), config).passed)


class ScoreAndValueFilterTests(unittest.TestCase):
    def test_minimum_lead_score_filter(self):
        config = replace(DEFAULT_LEAD_CONFIG, min_lead_score=60)
        self.assertFalse(outcome_for(lead(vacant=True), config).passed)
        self.assertTrue(
            outcome_for(
                lead(
                    vacant=True, absentee_owner=True, probate=True, tax_delinquent=True,
                    code_violation=True, tired_landlord=True,
                ),
                config,
            ).passed
        )

    def test_maximum_asking_price_filter(self):
        config = replace(DEFAULT_LEAD_CONFIG, max_asking_price=150_000)
        self.assertFalse(outcome_for(lead(asking_price=200_000), config).passed)
        self.assertTrue(outcome_for(lead(asking_price=100_000), config).passed)

    def test_unknown_asking_price_warns_instead_of_rejecting(self):
        config = replace(DEFAULT_LEAD_CONFIG, max_asking_price=150_000)
        result = outcome_for(lead(asking_price=None), config)
        self.assertTrue(result.passed)
        self.assertTrue(any("asking price unknown" in w for w in result.warnings))

    def test_minimum_equity_filter_uses_derived_equity(self):
        config = replace(DEFAULT_LEAD_CONFIG, min_equity=50_000)
        self.assertTrue(
            outcome_for(lead(estimated_value=200_000, asking_price=100_000), config).passed
        )
        self.assertFalse(
            outcome_for(lead(estimated_value=200_000, asking_price=180_000), config).passed
        )

    def test_unknown_equity_warns_instead_of_rejecting(self):
        config = replace(DEFAULT_LEAD_CONFIG, min_equity=50_000)
        result = outcome_for(lead(), config)
        self.assertTrue(result.passed)
        self.assertTrue(any("equity unknown" in w for w in result.warnings))

    def test_occupancy_filter(self):
        config = replace(DEFAULT_LEAD_CONFIG, allowed_occupancy=("vacant",))
        self.assertTrue(outcome_for(lead(occupancy=Occupancy.VACANT), config).passed)
        self.assertFalse(outcome_for(lead(occupancy=Occupancy.OWNER_OCCUPIED), config).passed)
        self.assertTrue(outcome_for(lead(occupancy=Occupancy.UNKNOWN), config).passed)

    def test_required_distress_signal_filter(self):
        config = replace(DEFAULT_LEAD_CONFIG, required_signals=("probate",))
        self.assertTrue(outcome_for(lead(probate=True), config).passed)
        self.assertFalse(outcome_for(lead(vacant=True), config).passed)

    def test_minimum_signal_count_filter(self):
        config = replace(DEFAULT_LEAD_CONFIG, min_signal_count=2)
        self.assertFalse(outcome_for(lead(vacant=True), config).passed)
        self.assertTrue(outcome_for(lead(vacant=True, probate=True), config).passed)


class MissingDataTests(unittest.TestCase):
    def test_gaps_are_recorded_rather_than_causing_rejection(self):
        bare = lead(state="", property_type=PropertyType.UNKNOWN)
        result = outcome_for(bare)
        self.assertTrue(result.passed)
        for expected in ("asking price", "estimated value / ARV", "beds", "square footage"):
            self.assertIn(expected, bare.missing_data)

    def test_unconfirmed_signals_are_flagged_for_verification(self):
        bare = lead()
        outcome_for(bare)
        self.assertTrue(any("unconfirmed signals" in n for n in bare.needs_verification))

    def test_derived_equity_is_flagged_as_derived(self):
        item = lead(estimated_value=200_000, asking_price=100_000)
        outcome_for(item)
        self.assertTrue(item.equity_is_derived)
        self.assertTrue(any("derived" in n for n in item.needs_verification))

    def test_reported_equity_is_not_flagged_as_derived(self):
        item = lead(estimated_equity=90_000)
        self.assertFalse(item.equity_is_derived)
        self.assertEqual(item.equity_estimate, 90_000)

    def test_equity_is_none_when_it_cannot_be_computed(self):
        self.assertIsNone(lead().equity_estimate)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


class CsvLoadingTests(unittest.TestCase):
    def test_column_aliases_are_accepted(self):
        row = {
            "property_address": "9 Alias Way",
            "list_price": "$120,000",
            "market_value": "250,000",
            "repair_estimate": "30000",
            "owner": "FICTIONAL OWNER",
            "absentee": "yes",
            "vacant_property": "Y",
            "br": "3",
            "ba": "2",
            "building_sqft": "1500",
            "dom": "45",
        }
        item = lead_from_row(row)
        self.assertEqual(item.address, "9 Alias Way")
        self.assertEqual(item.asking_price, 120_000)
        self.assertEqual(item.estimated_value, 250_000)
        self.assertEqual(item.estimated_repairs, 30_000)
        self.assertEqual(item.owner_name, "FICTIONAL OWNER")
        self.assertTrue(item.absentee_owner)
        self.assertTrue(item.vacant)
        self.assertEqual(item.beds, 3)
        self.assertEqual(item.days_on_market, 45)

    def test_canonical_column_names_also_work(self):
        item = lead_from_row(
            {"address": "9 Canonical St", "asking_price": "99000", "estimated_value": "200000"}
        )
        self.assertEqual(item.address, "9 Canonical St")
        self.assertEqual(item.asking_price, 99_000)

    def test_blank_and_unknown_never_become_false(self):
        self.assertIsNone(to_tri_bool(""))
        self.assertIsNone(to_tri_bool("unknown"))
        self.assertIsNone(to_tri_bool(None))
        self.assertIsNone(to_tri_bool("n/a"))
        self.assertFalse(to_tri_bool("no"))
        self.assertTrue(to_tri_bool("YES"))

    def test_missing_fields_stay_blank(self):
        item = lead_from_row({"address": "9 Sparse St"})
        self.assertIsNone(item.asking_price)
        self.assertIsNone(item.estimated_value)
        self.assertIsNone(item.vacant)
        self.assertEqual(item.owner_name, "")
        self.assertIs(item.property_type, PropertyType.UNKNOWN)

    def test_a_lead_id_is_derived_when_absent(self):
        self.assertTrue(lead_from_row({"address": "9 Sparse St"}).lead_id)

    def test_the_bundled_sample_list_loads(self):
        source = CsvLeadSource(SAMPLE_LEADS)
        leads = source.search_leads()
        self.assertGreaterEqual(len(leads), 20)
        self.assertTrue(all(item.source for item in leads))

    def test_a_row_without_an_address_is_kept_with_a_warning(self):
        source = CsvLeadSource(SAMPLE_LEADS)
        source.search_leads()
        self.assertTrue(any("no address" in w for w in source.warnings))


# ---------------------------------------------------------------------------
# Pipeline and Wave 1 integration
# ---------------------------------------------------------------------------


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)
        cls.by_id = {r.lead.lead_id: r for r in cls.report.results}

    def test_duplicates_are_merged_before_analysis(self):
        self.assertGreaterEqual(len(self.report.duplicates), 2)
        self.assertLess(len(self.report.results), self.report.rows_read)
        self.assertNotIn("LH-002", self.by_id)  # merged into LH-001

    def test_out_of_market_leads_are_filtered_not_analyzed(self):
        result = self.by_id["LH-005"]  # Ohio
        self.assertEqual(result.status, STATUS_FILTERED)
        self.assertIsNone(result.analysis)

    def test_a_hot_lead_can_still_be_filtered_out_on_market(self):
        result = self.by_id["LH-005"]
        self.assertEqual(result.score.classification, Classification.HOT)
        self.assertFalse(result.filter_outcome.passed)

    def test_commercial_and_land_are_excluded(self):
        for lead_id in ("LH-018", "LH-019"):
            self.assertEqual(self.by_id[lead_id].status, STATUS_FILTERED)

    def test_surviving_leads_reach_the_wave_one_analyzer(self):
        result = self.by_id["LH-011"]
        self.assertEqual(result.status, STATUS_ANALYZED)
        self.assertIsNotNone(result.analysis)
        self.assertIsNotNone(result.analysis.financials.mao)

    def test_the_wave_one_formula_is_the_only_mao_calculator(self):
        from wholesale_engine.analysis.financials import maximum_allowable_offer

        result = self.by_id["LH-011"]
        expected = maximum_allowable_offer(
            result.analysis.arv.arv, result.analysis.repairs.base
        )
        self.assertAlmostEqual(result.analysis.financials.mao, expected, places=2)

    def test_comps_lift_a_source_arv_to_verified(self):
        result = self.by_id["LH-011"]
        self.assertEqual(result.arv_status, ARV_COMP_SUPPORTED)

    def test_without_comps_an_arv_stays_source_provided(self):
        result = self.by_id["LH-010"]
        self.assertEqual(result.arv_status, ARV_SOURCE_PROVIDED)
        self.assertEqual(result.analysis.decision, Decision.NEED_MORE_DATA)

    def test_lead_signals_reach_the_analyzer_as_unverified_claims(self):
        result = self.by_id["LH-009"]
        flags = [f for f in result.analysis.risk_flags if f.code == "distress_indicator"]
        self.assertTrue(flags)
        self.assertTrue(all("has not verified it" in f.message for f in flags))

    def test_lead_score_and_deal_score_are_independent(self):
        pairs = {
            (r.score.total, r.analysis.score.total)
            for r in self.report.results
            if r.analysis is not None
        }
        self.assertTrue(any(lead_s != deal_s for lead_s, deal_s in pairs))

    def test_min_deal_score_filters_after_analysis(self):
        config = with_overrides(DEFAULT_LEAD_CONFIG, min_deal_score=95)
        report = run_from_csv(SAMPLE_LEADS, lead_config=config, comps_path=SAMPLE_LEAD_COMPS)
        self.assertTrue(all(r.status != STATUS_ANALYZED for r in report.results))
        self.assertTrue(any(r.status == STATUS_BELOW_DEAL_SCORE for r in report.results))

    def test_prioritization_puts_the_best_deal_first(self):
        ranked = prioritize(self.report.analyzed)
        scores = [r.deal_score for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_hot_leads_are_hot_or_strong_only(self):
        for result in hot_leads(self.report):
            self.assertIn(
                result.score.classification, (Classification.HOT, Classification.STRONG)
            )

    def test_analysis_can_be_skipped_for_a_lead_only_pass(self):
        report = run_lead_pipeline([lead(vacant=True)], analyze=False)
        self.assertIsNone(report.results[0].analysis)
        self.assertEqual(report.results[0].score.total, 10.0)

    def test_the_summary_states_the_lead_deal_distinction(self):
        text = render_lead_summary(self.report)
        self.assertIn("LEAD score", text)
        self.assertIn("DEAL score", text)


class Wave1RegressionTests(unittest.TestCase):
    """Wave 2 must not have changed how Wave 1 underwrites."""

    def test_wave_one_sample_still_produces_its_original_decisions(self):
        from wholesale_engine.analysis import analyze_properties
        from wholesale_engine.data.csv_loader import load_properties_csv
        from wholesale_engine.main import SAMPLE_COMPS, SAMPLE_PROPERTIES

        loaded = load_properties_csv(SAMPLE_PROPERTIES, SAMPLE_COMPS)
        results = {r.lead.property_id: r for r in analyze_properties(loaded.leads)}
        self.assertEqual(results["WS-001"].decision, Decision.GO)
        self.assertEqual(results["WS-002"].decision, Decision.PASS)
        self.assertEqual(results["WS-003"].decision, Decision.NEED_MORE_DATA)
        self.assertEqual(results["WS-007"].decision, Decision.NEGOTIATE)

    def test_lead_conversion_preserves_the_property_fields(self):
        item = lead(
            address="1 Convert St", city="Tampa", state="FL", county="Hillsborough",
            asking_price=100_000, estimated_value=200_000, estimated_repairs=30_000,
            beds=3, baths=2, sqft=1_400, year_built=1990, vacant=True, probate=True,
        )
        converted = item.to_property_lead()
        self.assertEqual(converted.asking_price, 100_000)
        self.assertEqual(converted.user_arv, 200_000)
        self.assertEqual(converted.user_repair_estimate, 30_000)
        self.assertIn("vacant", converted.distress_indicators)
        self.assertIn("probate", converted.distress_indicators)


# ---------------------------------------------------------------------------
# Output files and CLI
# ---------------------------------------------------------------------------


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)

    def test_pipeline_csv_starts_with_the_specified_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_lead_pipeline_csv(self.report, Path(tmp) / "lead_pipeline.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames[: len(LEAD_PIPELINE_COLUMNS)], LEAD_PIPELINE_COLUMNS)
                rows = list(reader)
        self.assertEqual(len(rows), len(self.report.results))

    def test_filtered_leads_export_with_a_blank_deal_side_and_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_lead_pipeline_csv(self.report, Path(tmp) / "p.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                rows = {row["lead_id"]: row for row in csv.DictReader(handle)}
        filtered = rows["LH-005"]
        self.assertEqual(filtered["mao"], "")
        self.assertEqual(filtered["deal_score"], "")
        self.assertTrue(filtered["filter_reasons"])

    def test_hot_leads_csv_contains_only_hot_and_strong(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_hot_leads_csv(self.report, Path(tmp) / "hot_leads.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["lead_classification"], ("🔥 HOT", "🟠 STRONG"))

    def test_hot_leads_are_sorted_by_deal_then_lead_then_spread(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_hot_leads_csv(self.report, Path(tmp) / "hot_leads.csv")
            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        keys = [
            (-float(r["deal_score"]), -float(r["lead_score"]), -float(r["potential_spread"] or 0))
            for r in rows
        ]
        self.assertEqual(keys, sorted(keys))


class CliTests(unittest.TestCase):
    def test_lead_hunting_run_writes_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Path(tmp) / "lead_pipeline.csv"
            hot = Path(tmp) / "hot_leads.csv"
            code = run([
                "--leads", str(SAMPLE_LEADS),
                "--lead-comps", str(SAMPLE_LEAD_COMPS),
                "--quiet", "--lead-out", str(pipeline), "--hot-out", str(hot),
            ])
            self.assertEqual(code, 0)
            self.assertTrue(pipeline.exists())
            self.assertTrue(hot.exists())

    def test_sample_leads_flag_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = run([
                "--sample-leads", "--quiet",
                "--lead-out", str(Path(tmp) / "p.csv"),
                "--hot-out", str(Path(tmp) / "h.csv"),
            ])
            self.assertEqual(code, 0)

    def test_state_and_score_flags_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Path(tmp) / "p.csv"
            run([
                "--leads", str(SAMPLE_LEADS), "--states", "FL", "--min-lead-score", "60",
                "--quiet", "--lead-out", str(pipeline), "--hot-out", str(Path(tmp) / "h.csv"),
            ])
            with open(pipeline, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        analyzed = [r for r in rows if r["pipeline_status"] == "analyzed"]
        self.assertTrue(analyzed)
        for row in analyzed:
            self.assertEqual(row["state"], "FL")
            self.assertGreaterEqual(float(row["lead_score"]), 60)

    def test_hot_only_restricts_the_pipeline_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Path(tmp) / "p.csv"
            run([
                "--sample-leads", "--hot-only", "--quiet",
                "--lead-out", str(pipeline), "--hot-out", str(Path(tmp) / "h.csv"),
            ])
            with open(pipeline, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        for row in rows:
            self.assertIn(row["lead_classification"], ("🔥 HOT", "🟠 STRONG"))

    def test_missing_lead_file_is_reported(self):
        self.assertEqual(run(["--leads", "nope.csv", "--quiet"]), 2)

    def test_wave_one_commands_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(["--sample", "--quiet", "--out", str(Path(tmp) / "o.csv")]), 0)


# ---------------------------------------------------------------------------
# Future-integration seams
# ---------------------------------------------------------------------------


class SeamTests(unittest.TestCase):
    def test_the_api_template_is_not_connected(self):
        template = ApiLeadSourceTemplate()
        for call in (
            lambda: template.search_leads(),
            lambda: template.get_property("1"),
            lambda: template.get_owner("1"),
            lambda: template.get_comps(lead()),
        ):
            with self.assertRaises(NotConfiguredError):
                call()

    def test_the_csv_source_admits_what_it_cannot_do(self):
        source = CsvLeadSource(SAMPLE_LEADS)
        with self.assertRaises(NotConfiguredError):
            source.get_owner("1")

    def test_skip_tracing_is_a_seam_not_an_implementation(self):
        with self.assertRaises(NotConfiguredError):
            UnconfiguredSkipTraceProvider().trace(build_request(
                run_lead_pipeline([lead(vacant=True)], analyze=False).results[0]
            ))

    def test_skip_trace_request_carries_no_invented_contact_data(self):
        result = run_lead_pipeline([lead(vacant=True)], analyze=False).results[0]
        request = build_request(result)
        self.assertFalse(hasattr(request, "phone"))
        self.assertEqual(request.owner_name, "")

    def test_skip_trace_candidates_exclude_passes(self):
        report = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)
        for result in skip_trace_candidates(report):
            self.assertNotEqual(result.analysis.decision, Decision.PASS)


if __name__ == "__main__":
    unittest.main()
