"""Decision-log wiring tests: what the funnel records, and what it does not.

The reason this file exists is one question a lead list cannot answer on its
own — *why isn't this property in my results?* The funnel already knew; it
just threw the answer away. These tests pin down that it now keeps it, that
keeping it is opt-in, and that the reasons it keeps are groupable.

The grouping is the part worth being careful about. ``rejection_summary``
groups by reason, so a reason with the property's own numbers baked into it —
"equity below $25,000", "property type duplex not requested" — produces one
group per property, which is a list, not a summary. Reason says which rule
fired; detail says what this property's figures were.

No network, no live provider: every hunt here runs off the bundled fictional
CSV lead list.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wholesale_engine.hunt import HuntBudget, Rejection, cheap_filter, run_hunt
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.models.enums import PropertyType
from wholesale_engine.providers import CsvProvider
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.service.paths import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.storage import (
    ACCEPTED,
    INCOMPLETE,
    REJECTED,
    STAGE_BUY_BOX,
    STAGE_DEAL_SCORE,
    STAGE_FINAL,
    STAGE_LEAD_SCORE,
    DecisionLog,
    LeadStore,
)


class PassthroughProvider:
    """Returns every lead in the sample file, unfiltered.

    CsvProvider applies the criteria itself, the way a real vendor's API
    would. That is correct, but it means cheap_filter never sees a lead it
    would reject — so exercising the funnel's own buy-box filters needs a
    source that hands everything over.
    """

    name = "passthrough"
    is_local = True

    def __init__(self):
        from wholesale_engine.providers import CsvProvider
        from wholesale_engine.providers.metrics import ProviderMetrics

        self.metrics = ProviderMetrics()
        self._inner = CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS)

    def search_properties(self, criteria):
        return self._inner.search_properties(HuntCriteria(states=(), property_types=()))

    def supports(self, capability):
        return self._inner.supports(capability)

    def get_property(self, lead):
        return self._inner.get_property(lead)

    def get_distress_data(self, lead):
        return self._inner.get_distress_data(lead)

    def get_comps(self, lead, **kwargs):
        return self._inner.get_comps(lead, **kwargs)


class HuntCase(unittest.TestCase):
    """A real funnel run over the bundled fictional lead list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = LeadStore(self.tmp / "leads.db")
        self.log = DecisionLog(self.store.connection)
        self.provider = CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def run_funnel(self, criteria=None, provider=None, **kwargs):
        return run_hunt(
            provider or self.provider,
            criteria or HuntCriteria(),
            store=self.store,
            budget=HuntBudget(),
            **kwargs,
        )

    def count_decisions(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0]

    def count_runs(self) -> int:
        return self.store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


# ---------------------------------------------------------------------------
# (a) the default is unchanged
# ---------------------------------------------------------------------------


class DefaultIsUnchanged(HuntCase):
    def test_a_plain_run_records_nothing(self):
        result = self.run_funnel()
        self.assertTrue(result.prioritized)
        self.assertEqual(self.count_decisions(), 0)
        self.assertEqual(self.count_runs(), 0)

    def test_the_new_parameters_all_default_to_off(self):
        import inspect

        signature = inspect.signature(run_hunt)
        self.assertIsNone(signature.parameters["decisions"].default)
        self.assertIsNone(signature.parameters["run_id"].default)

    def test_existing_positional_callers_still_work(self):
        # The old call shape, unchanged, with everything positional.
        result = run_hunt(self.provider, HuntCriteria())
        self.assertTrue(result.prioritized)

    def test_a_plain_run_builds_no_decision_objects_at_all(self):
        # Not merely "does not write them" — does not construct them either,
        # so recording costs nothing when it is off.
        from wholesale_engine import hunt as hunt_module

        built = []
        original = hunt_module._decision
        hunt_module._decision = lambda *a, **k: built.append(a) or original(*a, **k)
        try:
            self.run_funnel()
        finally:
            hunt_module._decision = original
        self.assertEqual(built, [])


# ---------------------------------------------------------------------------
# (b) opt-in recording
# ---------------------------------------------------------------------------


class OptInRecording(HuntCase):
    def test_passing_a_log_creates_a_run_and_decisions(self):
        self.run_funnel(decisions=self.log)
        self.assertEqual(self.count_runs(), 1)
        self.assertGreater(self.count_decisions(), 0)

    def test_every_property_the_run_saw_gets_a_decision(self):
        result = self.run_funnel(decisions=self.log)
        run = self.log.recent_runs(1)[0]
        recorded = self.log.for_run(run.run_id)
        self.assertEqual(len(recorded), len(result.report.results))

    def test_the_run_row_carries_the_trigger_and_provider(self):
        self.run_funnel(decisions=self.log, trigger="scheduled", mode="TEST")
        run = self.log.recent_runs(1)[0]
        self.assertEqual(run.trigger, "scheduled")
        self.assertEqual(run.provider, "csv")
        self.assertEqual(run.mode, "TEST")

    def test_a_caller_that_owns_the_run_does_not_get_a_second_one(self):
        # The service opens its own run and passes the id in.
        run = self.log.start_run(trigger="api", provider="csv")
        self.run_funnel(decisions=self.log, run_id=run.run_id)
        self.assertEqual(self.count_runs(), 1)
        self.assertGreater(len(self.log.for_run(run.run_id)), 0)

    def test_a_caller_owned_run_is_not_finished_by_the_funnel(self):
        run = self.log.start_run(trigger="api", provider="csv")
        self.run_funnel(decisions=self.log, run_id=run.run_id)
        # Still RUNNING: closing it is the caller's job, and closing it here
        # would stamp a finish time before the caller had finished.
        self.assertEqual(self.log.get_run(run.run_id).status, "RUNNING")


# ---------------------------------------------------------------------------
# (c) + (d) reasons group; details stay separate
# ---------------------------------------------------------------------------


class ReasonsAreGroupable(unittest.TestCase):
    """The two reasons the inspection flagged, plus the shape that fixes them."""

    def _drop(self, lead: Lead, criteria: HuntCriteria) -> Rejection:
        _, dropped = cheap_filter([lead], criteria)
        self.assertEqual(len(dropped), 1)
        return dropped[0][1]

    def test_property_type_reason_no_longer_carries_the_type(self):
        lead = Lead(address="1 Main St", state="FL", property_type=PropertyType.CONDO)
        rejection = self._drop(
            lead, HuntCriteria(states=("FL",), property_types=("single_family",))
        )
        self.assertEqual(rejection.reason, "property type not in the requested set")
        self.assertNotIn("CONDO", rejection.reason)
        self.assertIn("CONDO", rejection.detail)

    def test_two_different_property_types_group_into_one_reason(self):
        # The whole point. Before, these were two separate rejection groups.
        criteria = HuntCriteria(states=("FL",), property_types=("single_family",))
        first = self._drop(
            Lead(address="1 A St", state="FL", property_type=PropertyType.CONDO), criteria
        )
        second = self._drop(
            Lead(address="2 B St", state="FL", property_type=PropertyType.LAND), criteria
        )
        self.assertEqual(first.reason, second.reason)
        self.assertNotEqual(first.detail, second.detail)

    def test_equity_reason_no_longer_carries_the_threshold(self):
        lead = Lead(
            address="1 Main St", state="FL", property_type=PropertyType.SINGLE_FAMILY,
            estimated_value=100_000, estimated_equity=12_000,
        )
        rejection = self._drop(lead, HuntCriteria(states=("FL",), min_equity=25_000))
        self.assertEqual(rejection.reason, "estimated equity below the minimum")
        self.assertNotIn("25,000", rejection.reason)
        self.assertIn("25,000", rejection.detail)
        self.assertIn("12,000", rejection.detail)

    def test_two_different_thresholds_still_group_into_one_reason(self):
        lead = Lead(
            address="1 Main St", state="FL", property_type=PropertyType.SINGLE_FAMILY,
            estimated_value=100_000, estimated_equity=1_000,
        )
        low = self._drop(lead, HuntCriteria(states=("FL",), min_equity=25_000))
        high = self._drop(lead, HuntCriteria(states=("FL",), min_equity=90_000))
        self.assertEqual(low.reason, high.reason)
        self.assertNotEqual(low.detail, high.detail)

    def test_a_rejection_is_still_a_string_with_the_old_text(self):
        # Nothing a person reads changed. The rejected-leads export, the report
        # tables and FilterOutcome.reasons all keep their exact values.
        lead = Lead(address="1 Main St", state="FL", property_type=PropertyType.CONDO)
        rejection = self._drop(
            lead, HuntCriteria(states=("FL",), property_types=("single_family",))
        )
        self.assertIsInstance(rejection, str)
        self.assertEqual(str(rejection), "property type CONDO not requested")

    def test_equity_rendered_text_is_also_unchanged(self):
        lead = Lead(
            address="1 Main St", state="FL", property_type=PropertyType.SINGLE_FAMILY,
            estimated_value=100_000, estimated_equity=12_000,
        )
        rejection = self._drop(lead, HuntCriteria(states=("FL",), min_equity=25_000))
        self.assertEqual(str(rejection), "equity below $25,000")

    def test_a_rejection_without_detail_renders_as_the_reason_alone(self):
        self.assertEqual(str(Rejection("nope")), "nope")
        self.assertEqual(Rejection("nope").detail, "")

    def test_a_rejection_composes_reason_and_detail_when_no_text_is_given(self):
        rejection = Rejection("too small", "400 sqft")
        self.assertEqual(str(rejection), "too small: 400 sqft")


class GroupingEndToEnd(HuntCase):
    def test_the_summary_groups_rather_than_listing(self):
        # A buy box narrow enough that many properties fail the same rule.
        self.run_funnel(
            HuntCriteria(states=("FL",), property_types=("single_family",)),
            provider=PassthroughProvider(),
            decisions=self.log,
        )
        run = self.log.recent_runs(1)[0]
        summary = self.log.rejection_summary(run.run_id)
        reasons = [reason for _, reason, _ in summary]
        self.assertEqual(len(reasons), len(set(reasons)),
                         f"reasons should be distinct groups, got {reasons}")
        # More properties rejected than there are groups: it really grouped.
        total = sum(count for _, _, count in summary)
        self.assertGreater(total, len(summary))

    def test_details_are_retained_separately_per_property(self):
        self.run_funnel(
            HuntCriteria(states=("FL",), property_types=("single_family",)),
            provider=PassthroughProvider(),
            decisions=self.log,
        )
        run = self.log.recent_runs(1)[0]
        type_rejections = [
            d for d in self.log.for_run(run.run_id)
            if d.reason == "property type not in the requested set"
        ]
        self.assertTrue(type_rejections)
        for decision in type_rejections:
            self.assertTrue(decision.detail, decision.address)
            self.assertIn("requested", decision.detail)

    def test_the_rendered_summary_reads_as_a_summary(self):
        self.run_funnel(
            HuntCriteria(states=("FL",), property_types=("single_family",)),
            provider=PassthroughProvider(),
            decisions=self.log,
        )
        run = self.log.recent_runs(1)[0]
        text = self.log.render_summary(run.run_id)
        self.assertIn("PROPERTIES WERE REJECTED", text)
        self.assertIn("[buy_box]", text)


# ---------------------------------------------------------------------------
# (e) score rejections
# ---------------------------------------------------------------------------


class ScoreRejections(HuntCase):
    def _decisions(self, criteria, provider=None):
        self.run_funnel(criteria, provider=provider, decisions=self.log)
        run = self.log.recent_runs(1)[0]
        return self.log.for_run(run.run_id)

    def test_lead_score_rejections_are_recorded_at_their_own_stage(self):
        recorded = self._decisions(HuntCriteria(min_lead_score=60))
        at_stage = [d for d in recorded if d.stage == STAGE_LEAD_SCORE]
        self.assertTrue(at_stage)
        for decision in at_stage:
            self.assertEqual(decision.outcome, REJECTED)
            self.assertEqual(decision.reason, "below the minimum lead score")
            self.assertIn("minimum 60", decision.detail)
            self.assertIsNotNone(decision.lead_score)

    def test_deal_score_rejections_are_recorded_at_their_own_stage(self):
        recorded = self._decisions(HuntCriteria(min_deal_score=75))
        at_stage = [d for d in recorded if d.stage == STAGE_DEAL_SCORE]
        self.assertTrue(at_stage)
        for decision in at_stage:
            self.assertEqual(decision.outcome, REJECTED)
            self.assertEqual(decision.reason, "below the minimum deal score")
            self.assertIsNotNone(decision.deal_score)

    def test_a_lead_score_rejection_keeps_the_score_that_caused_it(self):
        recorded = self._decisions(HuntCriteria(min_lead_score=60))
        at_stage = [d for d in recorded if d.stage == STAGE_LEAD_SCORE]
        for decision in at_stage:
            self.assertLess(decision.lead_score, 60)

    def test_buy_box_rejections_are_not_blamed_on_the_lead_score(self):
        # A lead outside the geography failed the buy box, whatever it scored.
        recorded = self._decisions(
            HuntCriteria(states=("FL",), min_lead_score=60),
            provider=PassthroughProvider(),
        )
        geography = [
            d for d in recorded if d.reason == "outside the requested geography"
        ]
        self.assertTrue(geography)
        for decision in geography:
            self.assertEqual(decision.stage, STAGE_BUY_BOX)


# ---------------------------------------------------------------------------
# (f) accepted outcomes and run completion
# ---------------------------------------------------------------------------


class AcceptedAndCompletion(HuntCase):
    def test_leads_that_clear_everything_are_recorded_as_accepted(self):
        self.run_funnel(decisions=self.log)
        run = self.log.recent_runs(1)[0]
        final = [d for d in self.log.for_run(run.run_id) if d.stage == STAGE_FINAL]
        self.assertTrue(final)
        self.assertTrue(any(d.outcome == ACCEPTED for d in final))

    def test_an_analyzed_lead_missing_data_is_incomplete_not_accepted(self):
        # Calling a gap an acceptance turns a list of leads you cannot yet
        # offer on into something that reads like a list of deals.
        self.run_funnel(decisions=self.log)
        run = self.log.recent_runs(1)[0]
        final = [d for d in self.log.for_run(run.run_id) if d.stage == STAGE_FINAL]
        incomplete = [d for d in final if d.outcome == INCOMPLETE]
        self.assertTrue(incomplete, "the sample list contains NEED MORE DATA leads")
        for decision in incomplete:
            self.assertIn("NEED MORE DATA", decision.detail)
            self.assertFalse(decision.was_rejected)

    def test_a_completed_run_is_finalized_with_counts(self):
        self.run_funnel(decisions=self.log)
        run = self.log.recent_runs(1)[0]
        self.assertEqual(run.status, "OK")
        self.assertTrue(run.succeeded)
        self.assertTrue(run.finished_at)
        self.assertGreater(run.leads_seen, 0)
        # Every property is in exactly one bucket: accepted, rejected, or
        # incomplete. A property missing from all three is one the run saw and
        # cannot account for.
        self.assertEqual(
            run.leads_seen,
            run.leads_accepted + run.leads_rejected + self._incomplete_count(run.run_id),
        )

    def _incomplete_count(self, run_id: int) -> int:
        return sum(
            1 for d in self.log.for_run(run_id) if d.outcome == INCOMPLETE
        )

    def test_every_decision_carries_the_address_and_dedupe_key(self):
        self.run_funnel(decisions=self.log)
        run = self.log.recent_runs(1)[0]
        for decision in self.log.for_run(run.run_id):
            self.assertTrue(decision.dedupe_key, decision.address)

    def test_one_property_can_be_traced_across_runs(self):
        # "Why isn't this in my list?" — the question this whole thing exists
        # to answer. A property rejected twice for the same reason is a buy
        # box problem, not a property problem.
        criteria = HuntCriteria(states=("FL",), property_types=("single_family",))
        self.run_funnel(criteria, provider=PassthroughProvider(), decisions=self.log)
        self.run_funnel(criteria, provider=PassthroughProvider(), decisions=self.log)
        run = self.log.recent_runs(1)[0]
        rejected = [d for d in self.log.for_run(run.run_id) if d.was_rejected]
        self.assertTrue(rejected)
        history = self.log.for_property(rejected[0].dedupe_key)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].reason, history[1].reason)


# ---------------------------------------------------------------------------
# (g) failures still finalize
# ---------------------------------------------------------------------------


class Exploding:
    """A provider that fails mid-search."""

    name = "boom"
    is_local = True

    def __init__(self):
        from wholesale_engine.providers.metrics import ProviderMetrics

        self.metrics = ProviderMetrics()

    def search_properties(self, criteria):
        raise RuntimeError("provider exploded")


class Failures(HuntCase):
    def test_a_crash_finalizes_the_run_as_failed_and_re_raises(self):
        with self.assertRaises(RuntimeError):
            run_hunt(Exploding(), HuntCriteria(), store=self.store, decisions=self.log)
        run = self.log.recent_runs(1)[0]
        self.assertEqual(run.status, "FAILED")
        self.assertFalse(run.succeeded)
        self.assertIn("exploded", run.error)
        self.assertTrue(run.finished_at)

    def test_a_crash_still_propagates_to_the_caller(self):
        # Recording a failure must not swallow it: existing callers rely on
        # the exception reaching them.
        with self.assertRaises(RuntimeError):
            run_hunt(Exploding(), HuntCriteria(), decisions=self.log)

    def test_a_crash_writes_no_partial_decisions(self):
        with self.assertRaises(RuntimeError):
            run_hunt(Exploding(), HuntCriteria(), store=self.store, decisions=self.log)
        self.assertEqual(self.count_decisions(), 0)

    def test_a_crash_without_a_log_behaves_exactly_as_before(self):
        with self.assertRaises(RuntimeError):
            run_hunt(Exploding(), HuntCriteria(), store=self.store)
        self.assertEqual(self.count_runs(), 0)

    def test_a_refused_search_is_partial_not_failed(self):
        class Refusing:
            name = "refusing"
            is_local = True

            def __init__(self):
                from wholesale_engine.providers.metrics import ProviderMetrics

                self.metrics = ProviderMetrics()

            def search_properties(self, criteria):
                from wholesale_engine.providers.base import ProviderResponse

                return ProviderResponse(
                    data=[], supported=False,
                    reason="this provider does not support search",
                    source="refusing",
                )

        run_hunt(Refusing(), HuntCriteria(), store=self.store, decisions=self.log)
        run = self.log.recent_runs(1)[0]
        # Nothing broke, but nothing was screened either.
        self.assertEqual(run.status, "PARTIAL")
        self.assertIn("does not support search", run.error)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


class Exports(unittest.TestCase):
    def test_the_decision_log_is_reachable_from_the_storage_package(self):
        from wholesale_engine import storage

        for name in ("DecisionLog", "Decision", "RunRecord", "ACCEPTED",
                     "REJECTED", "INCOMPLETE", "STAGE_BUY_BOX", "STAGE_FINAL"):
            self.assertTrue(hasattr(storage, name), name)
            self.assertIn(name, storage.__all__, name)

    def test_the_existing_storage_exports_still_resolve(self):
        from wholesale_engine import storage

        for name in storage.__all__:
            self.assertTrue(hasattr(storage, name), name)


if __name__ == "__main__":
    unittest.main()
