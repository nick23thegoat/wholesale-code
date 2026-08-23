"""Wave 4 — provider architecture, settings and cost control.

The rules these tests exist to hold in place:

* no credentials means CSV/test mode, stated plainly — never fabricated results
* an unsupported capability is a clear answer, not an exception or a guess
* comps are never requested for a raw lead
* skip tracing never happens
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.hunt import HuntBudget, cheap_filter, run_hunt
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.models.enums import PropertyType
from wholesale_engine.providers import (
    Capability,
    CsvProvider,
    HttpPropertyDataProvider,
    HuntCriteria,
    ProviderMetrics,
    ProviderNotConfigured,
    ProviderResponse,
    describe_sources,
    get_provider,
    provider_info,
    registered_names,
)
from wholesale_engine.settings import NO_PROVIDER_MESSAGE, ProviderSettings
from wholesale_engine.storage import LeadStore


def csv_provider(comps: bool = True) -> CsvProvider:
    return CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS if comps else None)


# ---------------------------------------------------------------------------
# Settings and credentials
# ---------------------------------------------------------------------------


class SettingsTests(unittest.TestCase):
    def test_blank_environment_means_nothing_is_configured(self):
        settings = ProviderSettings()
        self.assertFalse(settings.has_property_data)
        self.assertFalse(settings.has_comps)
        self.assertFalse(settings.has_skip_trace)
        self.assertEqual(settings.describe(), "no live data credentials configured")

    def test_a_key_alone_is_not_enough_for_a_live_search(self):
        # An endpoint cannot be guessed, so a key on its own configures nothing.
        settings = ProviderSettings(property_data_api_key="k")
        self.assertFalse(settings.has_property_data)
        self.assertEqual(settings.missing_for_property_data(), ["PROPERTY_DATA_BASE_URL"])

    def test_key_and_endpoint_together_are_enough(self):
        settings = ProviderSettings(
            property_data_api_key="k", property_data_base_url="https://example.invalid/v1"
        )
        self.assertTrue(settings.has_property_data)
        self.assertEqual(settings.missing_for_property_data(), [])

    def test_describe_never_leaks_a_credential(self):
        settings = ProviderSettings(
            property_data_api_key="super-secret", property_data_base_url="https://x.invalid"
        )
        self.assertNotIn("super-secret", settings.describe())

    def test_dotenv_does_not_override_a_real_exported_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("WHOLESALE_TEST_VAR=from-file\n", encoding="utf-8")
            os.environ["WHOLESALE_TEST_VAR"] = "from-environment"
            try:
                from wholesale_engine.settings import load_dotenv

                load_dotenv(path)
                self.assertEqual(os.environ["WHOLESALE_TEST_VAR"], "from-environment")
            finally:
                os.environ.pop("WHOLESALE_TEST_VAR", None)

    def test_a_missing_dotenv_is_not_an_error(self):
        from wholesale_engine.settings import load_dotenv

        self.assertEqual(load_dotenv(Path("/nonexistent/.env")), {})

    def test_env_example_lists_every_variable_the_code_reads(self):
        from wholesale_engine.settings import ENV_VARS

        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for name in ENV_VARS:
            self.assertIn(name, text, f"{name} missing from .env.example")

    def test_env_example_ships_no_credential_values(self):
        # Non-secret defaults (mode, provider names, budget caps) carry values
        # on purpose. Anything that looks like a credential must be blank.
        secret_markers = ("KEY", "SECRET", "PASSWORD", "TOKEN", "JSON", "URL", "TO=")
        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name = stripped.split("=", 1)[0]
            if any(marker in name.upper() for marker in secret_markers):
                self.assertTrue(
                    stripped.endswith("="), f"credential carries a value: {line}"
                )

    def test_env_example_documents_the_mode_and_provider_slots(self):
        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for name in (
            "WHOLESALE_MODE", "DATA_PROVIDER", "COMPS_PROVIDER",
            "SKIP_TRACE_PROVIDER", "NOTIFICATION_PROVIDER",
            "MAX_RAW_LEADS", "MAX_RESEARCH", "MAX_COMPS", "MAX_SKIP_TRACES",
        ):
            self.assertIn(name, text)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class ProviderInterfaceTests(unittest.TestCase):
    def test_csv_provider_declares_only_what_it_can_do(self):
        provider = csv_provider(comps=False)
        self.assertTrue(provider.supports(Capability.SEARCH))
        self.assertFalse(provider.supports(Capability.COMPS))
        self.assertFalse(provider.supports(Capability.OWNER))
        self.assertFalse(provider.supports(Capability.DISTRESS))

    def test_comps_capability_appears_only_with_a_comps_file(self):
        self.assertTrue(csv_provider(comps=True).supports(Capability.COMPS))

    def test_an_unsupported_capability_answers_clearly(self):
        response = csv_provider().get_owner(Lead(address="1 Main St"))
        self.assertFalse(response.supported)
        self.assertIsNone(response.data)
        self.assertIn("does not support", response.reason)

    def test_unsupported_never_invents_data(self):
        for call in ("get_owner", "get_distress_data"):
            response = getattr(csv_provider(), call)(Lead(address="1 Main St"))
            self.assertIsNone(response.data)
            self.assertIn("nothing has been invented", response.reason)

    def test_unsupported_is_recorded_not_raised(self):
        provider = csv_provider()
        provider.get_distress_data(Lead(address="1 Main St"))
        self.assertIn("get_distress_data", provider.metrics.unsupported)

    def test_response_states_are_distinguishable(self):
        unsupported = ProviderResponse.unsupported("p", Capability.OWNER)
        empty = ProviderResponse.empty("p")
        found = ProviderResponse(data=["x"], source="p")
        self.assertFalse(unsupported.supported)
        self.assertTrue(empty.supported)
        self.assertFalse(empty.ok)
        self.assertTrue(found.ok)

    def test_search_returns_normalized_leads(self):
        response = csv_provider().search_properties(HuntCriteria())
        self.assertTrue(response.ok)
        self.assertTrue(all(isinstance(lead, Lead) for lead in response.data))


class HttpTemplateTests(unittest.TestCase):
    """The template must stay inert until a real vendor is wired in."""

    def test_it_refuses_to_construct_without_credentials(self):
        with self.assertRaises(ProviderNotConfigured) as ctx:
            HttpPropertyDataProvider(ProviderSettings())
        self.assertIn("PROPERTY_DATA_API_KEY", str(ctx.exception))

    def test_it_refuses_to_construct_without_a_documented_endpoint(self):
        settings = ProviderSettings(
            property_data_api_key="k", property_data_base_url="https://example.invalid/v1"
        )
        with self.assertRaises(ProviderNotConfigured) as ctx:
            HttpPropertyDataProvider(settings)
        self.assertIn("documentation", str(ctx.exception))

    def test_no_endpoint_path_is_shipped(self):
        self.assertEqual(HttpPropertyDataProvider.search_path, "")

    def test_no_base_url_is_shipped(self):
        self.assertIsNone(ProviderSettings().property_data_base_url)

    def test_it_rate_limits_itself_by_default(self):
        self.assertGreater(HttpPropertyDataProvider.min_seconds_between_calls, 0)


class RegistryTests(unittest.TestCase):
    def test_csv_is_always_available(self):
        self.assertIn("csv", registered_names())

    def test_no_paid_vendor_is_pre_selected(self):
        for name in registered_names():
            self.assertIn(name, ("csv", "http-template"))

    def test_an_unknown_source_is_refused_not_guessed(self):
        with self.assertRaises(ProviderNotConfigured) as ctx:
            get_provider("some-vendor")
        self.assertIn("unknown provider", str(ctx.exception))

    def test_describe_sources_states_the_fallback(self):
        text = describe_sources(ProviderSettings())
        self.assertIn(NO_PROVIDER_MESSAGE, text)

    def test_provider_info_reports_what_is_missing(self):
        info = provider_info("http-template")
        self.assertTrue(info.requires_credentials)
        self.assertFalse(info.configured)


# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------


class CostControlTests(unittest.TestCase):
    def setUp(self):
        self.provider = csv_provider()
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_cheap_filters_run_before_anything_billable(self):
        result = run_hunt(self.provider, HuntCriteria(min_lead_score=60), store=self.store)
        stages = [name for name, _ in result.metrics.stages]
        self.assertLess(
            stages.index("after cheap filters"), stages.index("after property research")
        )
        self.assertLess(stages.index("after lead score"), stages.index("after comps"))

    def test_the_funnel_narrows_at_every_stage(self):
        result = run_hunt(self.provider, HuntCriteria(min_lead_score=60), store=self.store)
        counts = [count for _, count in result.metrics.stages]
        for earlier, later in zip(counts, counts[1:]):
            self.assertLessEqual(later, earlier)

    def test_comps_are_not_requested_for_every_raw_lead(self):
        result = run_hunt(self.provider, HuntCriteria(min_lead_score=60), store=self.store)
        stages = dict(result.metrics.stages)
        self.assertLess(stages["after comps"], stages["raw leads from source"])

    def test_research_respects_its_budget(self):
        budget = HuntBudget(research_limit=2, comps_limit=1, research_min_lead_score=0.0)
        result = run_hunt(self.provider, HuntCriteria(), budget=budget, store=self.store)
        stages = dict(result.metrics.stages)
        self.assertLessEqual(stages["after property research"], 2)

    def test_nothing_is_ever_skip_traced(self):
        result = run_hunt(self.provider, HuntCriteria(), store=self.store)
        self.assertEqual(result.metrics.skip_trace_calls, 0)
        self.assertEqual(HuntBudget().skip_trace_limit, 0)

    def test_metrics_report_every_counter(self):
        result = run_hunt(self.provider, HuntCriteria(), store=self.store)
        data = result.metrics.as_dict()
        for key in (
            "properties_searched", "properties_returned", "properties_filtered",
            "property_detail_calls", "comp_calls", "api_errors", "estimated_api_calls",
        ):
            self.assertIn(key, data)

    def test_estimated_calls_are_the_sum_of_billable_calls(self):
        metrics = ProviderMetrics(search_calls=1, detail_calls=30, comp_calls=10)
        self.assertEqual(metrics.estimated_api_calls, 41)

    def test_a_tighter_lead_score_gate_cuts_billable_work(self):
        loose = run_hunt(csv_provider(), HuntCriteria(), store=LeadStore(":memory:"))
        tight = run_hunt(
            csv_provider(), HuntCriteria(min_lead_score=85), store=LeadStore(":memory:")
        )
        self.assertLessEqual(
            dict(tight.metrics.stages)["after lead score"],
            dict(loose.metrics.stages)["after lead score"],
        )


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


class CriteriaTests(unittest.TestCase):
    def test_default_states_are_the_target_markets(self):
        self.assertEqual(HuntCriteria().states, ("FL", "TX", "MO"))

    def test_states_are_normalized(self):
        self.assertEqual(HuntCriteria(states=(" fl ", "tx")).states, ("FL", "TX"))

    def test_an_unknown_signal_is_refused(self):
        with self.assertRaises(ValueError):
            HuntCriteria(required_signals=("haunted",))

    def test_geography_matching_across_all_four_levels(self):
        criteria = HuntCriteria(
            states=("FL",), counties=("Hillsborough",), cities=("Tampa",), zip_codes=("33601",)
        )
        self.assertTrue(criteria.matches_geography("FL", "Hillsborough", "Tampa", "33601"))
        self.assertFalse(criteria.matches_geography("TX", "Hillsborough", "Tampa", "33601"))
        self.assertFalse(criteria.matches_geography("FL", "Pinellas", "Tampa", "33601"))
        self.assertFalse(criteria.matches_geography("FL", "Hillsborough", "Ocala", "33601"))
        self.assertFalse(criteria.matches_geography("FL", "Hillsborough", "Tampa", "33602"))

    def test_an_unknown_price_never_rejects(self):
        self.assertTrue(HuntCriteria(min_price=50_000).matches_price(None))

    def test_an_unknown_property_type_never_rejects(self):
        self.assertTrue(HuntCriteria().matches_property_type("unknown"))

    def test_price_band_is_inclusive(self):
        criteria = HuntCriteria(min_price=50_000, max_price=100_000)
        self.assertTrue(criteria.matches_price(50_000))
        self.assertTrue(criteria.matches_price(100_000))
        self.assertFalse(criteria.matches_price(49_999))
        self.assertFalse(criteria.matches_price(100_001))

    def test_an_unknown_signal_value_does_not_reject_the_lead(self):
        lead = Lead(address="1 Main St", state="FL", property_type=PropertyType.SINGLE_FAMILY)
        kept, dropped = cheap_filter([lead], HuntCriteria(required_signals=("vacant",)))
        self.assertEqual(len(kept), 1, "unknown must be a gap to fill, not a rejection")

    def test_an_explicitly_false_signal_does_reject(self):
        lead = Lead(
            address="1 Main St", state="FL",
            property_type=PropertyType.SINGLE_FAMILY, vacant=False,
        )
        kept, dropped = cheap_filter([lead], HuntCriteria(required_signals=("vacant",)))
        self.assertEqual(kept, [])
        self.assertIn("required signals", dropped[0][1])

    def test_signals_are_any_of_not_all_of(self):
        lead = Lead(
            address="1 Main St", state="FL", property_type=PropertyType.SINGLE_FAMILY,
            vacant=True, probate=False,
        )
        kept, _ = cheap_filter([lead], HuntCriteria(required_signals=("vacant", "probate")))
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
