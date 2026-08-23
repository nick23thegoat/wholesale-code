"""Wave 4 — the funnel end to end, and the CLI that drives it.

The load-bearing guarantee: Wave 4 adds a data layer in front of the existing
engine and changes none of its arithmetic. A lead run through the hunt must
produce the identical analysis to the same lead run through Wave 2.
"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.config import DEFAULT_CONFIG, DEFAULT_LEAD_CONFIG
from wholesale_engine.hunt import HuntBudget, run_hunt
from wholesale_engine.lead_hunter import run_from_csv
from wholesale_engine.lead_hunter.models import STATUS_ANALYZED
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS, run
from wholesale_engine.models.enums import WholesaleFeeStatus
from wholesale_engine.providers import CsvProvider, HuntCriteria
from wholesale_engine.reports.hunt_report import render_hunt_summary
from wholesale_engine.storage import LeadStore


def hunt(criteria=None, **kwargs):
    store = LeadStore(":memory:")
    try:
        return run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            criteria or HuntCriteria(),
            store=store,
            **kwargs,
        )
    finally:
        store.close()


class FunnelTests(unittest.TestCase):
    def test_the_hunt_produces_results(self):
        result = hunt()
        self.assertTrue(result.report.results)
        self.assertTrue(result.prioritized)

    def test_it_runs_with_no_credentials_at_all(self):
        from wholesale_engine.settings import ProviderSettings

        self.assertFalse(ProviderSettings().has_property_data)
        self.assertTrue(hunt().report.results)

    def test_score_gates_are_applied(self):
        result = hunt(HuntCriteria(min_lead_score=80, min_deal_score=70))
        for entry in result.report.results:
            if entry.status == STATUS_ANALYZED:
                self.assertGreaterEqual(entry.score.total, 80)
                self.assertGreaterEqual(entry.deal_score, 70)

    def test_geography_narrows_the_funnel(self):
        florida = hunt(HuntCriteria(states=("FL",)))
        for entry in florida.report.results:
            if entry.status == STATUS_ANALYZED:
                self.assertEqual(entry.lead.state.upper(), "FL")

    def test_prioritization_puts_analyzed_leads_first(self):
        result = hunt(HuntCriteria(min_lead_score=60))
        statuses = [r.status == STATUS_ANALYZED for r in result.prioritized]
        self.assertEqual(statuses, sorted(statuses, reverse=True))

    def test_priority_is_its_own_score_not_the_deal_score(self):
        store = LeadStore(":memory:")
        run_hunt(CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store)
        second = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store
        )
        for entry in second.report.results:
            priority = second.priority_for(entry.lead)
            self.assertIsNotNone(priority, entry.lead.address)
            self.assertEqual(second.priority_of(entry), priority.total)
            self.assertGreaterEqual(priority.total, 0.0)
            self.assertLessEqual(priority.total, 100.0)
        store.close()

    def test_priority_never_writes_back_to_the_other_two_scores(self):
        store = LeadStore(":memory:")
        with_priority = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store
        )
        without = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)
        baseline = {r.lead.address: r for r in without.results}
        for entry in with_priority.report.results:
            other = baseline.get(entry.lead.address)
            if other is None:
                continue
            self.assertEqual(entry.score.total, other.score.total, entry.lead.address)
            if entry.analysis and other.analysis:
                self.assertEqual(entry.analysis.score.total, other.analysis.score.total)
        store.close()

    def test_the_summary_renders(self):
        text = render_hunt_summary(hunt(HuntCriteria(min_lead_score=60)))
        self.assertIn("HUNT", text)
        self.assertIn("PROVIDER CALLS", text)

    def test_every_fee_column_names_its_price(self):
        text = render_hunt_summary(hunt())
        self.assertIn("FEE@ASK", text)
        self.assertIn("ASKING", text)
        self.assertIn("OFFER", text)


class NoSecondAnalyzerTests(unittest.TestCase):
    """Wave 4 must not have grown its own deal math."""

    def test_the_hunt_matches_wave_2_exactly(self):
        wave2 = run_from_csv(SAMPLE_LEADS, comps_path=SAMPLE_LEAD_COMPS)
        wave2_by_address = {
            r.lead.address: r.analysis for r in wave2.results if r.analysis
        }
        for entry in hunt().report.results:
            if entry.analysis is None:
                continue
            other = wave2_by_address.get(entry.lead.address)
            if other is None:
                continue
            self.assertEqual(entry.analysis.score.total, other.score.total, entry.lead.address)
            self.assertEqual(entry.analysis.financials.mao, other.financials.mao)
            self.assertEqual(
                entry.analysis.financials.potential_wholesale_fee,
                other.financials.potential_wholesale_fee,
            )
            self.assertEqual(entry.analysis.decision, other.decision)

    def test_the_hunt_module_defines_no_mao_of_its_own(self):
        source = (Path(__file__).resolve().parent.parent
                  / "wholesale_engine" / "hunt.py").read_text()
        for forbidden in ("arv_percentage", "0.70", "* 0.7", "wholesale_fee ="):
            self.assertNotIn(forbidden, source, f"hunt.py must not recompute {forbidden}")

    def test_the_fee_stays_a_target_through_the_hunt(self):
        result = hunt(HuntCriteria(min_lead_score=60))
        below_target_go = [
            entry for entry in result.report.results
            if entry.analysis
            and entry.analysis.financials.wholesale_fee_status is WholesaleFeeStatus.BELOW_TARGET
            and "GO" in str(entry.analysis.decision)
        ]
        self.assertTrue(
            below_target_go,
            "a below-target deal must still be able to reach GO after Wave 4",
        )


class BudgetTests(unittest.TestCase):
    def test_a_zero_research_budget_stops_billable_enrichment(self):
        result = hunt(budget=HuntBudget(research_limit=0, comps_limit=0))
        stages = dict(result.metrics.stages)
        self.assertEqual(stages["after property research"], 0)

    def test_analysis_still_runs_when_enrichment_is_capped_out(self):
        result = hunt(
            HuntCriteria(min_lead_score=60),
            budget=HuntBudget(research_limit=0, comps_limit=0),
        )
        self.assertTrue(any(r.analysis for r in result.report.results))

    def test_defaults_match_the_documented_funnel_shape(self):
        budget = HuntBudget()
        self.assertGreater(budget.research_limit, budget.comps_limit)
        self.assertGreater(budget.comps_min_lead_score, budget.research_min_lead_score)


class CliTests(unittest.TestCase):
    def _run(self, argv) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = run(argv)
        self.assertEqual(code, 0, buffer.getvalue())
        return buffer.getvalue()

    def test_list_sources_says_what_is_configured(self):
        output = self._run(["--list-sources"])
        self.assertIn("csv", output)
        self.assertIn("No live property-data provider configured", output)

    def test_hunt_runs_from_the_csv_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._run([
                "--hunt", "--source", "csv",
                "--states", "FL,TX,MO",
                "--min-lead-score", "60", "--min-deal-score", "60",
                "--db", ":memory:", "--out-dir", tmp,
            ])
            for name in (
                "daily_leads.csv", "hot_leads.csv",
                "deals_to_review.csv", "rejected_leads.csv", "daily_leads.json",
            ):
                self.assertTrue((Path(tmp) / name).exists(), name)
        self.assertIn("PROVIDER CALLS", output)

    def test_an_unconfigured_live_source_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._run([
                "--hunt", "--source", "http-template",
                "--db", ":memory:", "--out-dir", tmp,
            ])
        self.assertIn("No live property-data provider configured", output)

    def test_geography_flags_reach_the_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._run([
                "--hunt", "--states", "FL", "--cities", "Tampa",
                "--db", ":memory:", "--out-dir", tmp,
            ])
        self.assertIn("cities: tampa", output)

    def test_signal_flags_reach_the_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._run([
                "--hunt", "--vacant", "--probate",
                "--db", ":memory:", "--out-dir", tmp,
            ])
        self.assertIn("signals:", output)
        self.assertIn("vacant", output)

    def test_the_wave_1_and_wave_2_commands_still_work(self):
        self._run(["--sample", "--quiet"])
        self._run(["--sample-leads", "--quiet"])

    def test_the_viability_floor_is_settable_from_the_cli(self):
        from wholesale_engine.main import build_parser

        args = build_parser().parse_args(["--sample", "--viable-fee", "0"])
        self.assertEqual(args.viable_fee, 0.0)
        self.assertEqual(DEFAULT_CONFIG.min_viable_wholesale_fee, 10_000.0)


class SkipTraceTests(unittest.TestCase):
    def test_skip_tracing_is_still_only_an_interface(self):
        from wholesale_engine.lead_hunter.skip_trace import UnconfiguredSkipTraceProvider

        with self.assertRaises(Exception):
            UnconfiguredSkipTraceProvider().trace(None)

    def test_no_provider_exposes_a_contact_capability(self):
        from wholesale_engine.providers import Capability

        self.assertNotIn("contact", [c.value for c in Capability])

    def test_the_hunt_never_calls_a_skip_trace(self):
        self.assertEqual(hunt().metrics.skip_trace_calls, 0)


if __name__ == "__main__":
    unittest.main()
