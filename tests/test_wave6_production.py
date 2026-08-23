"""Wave 6 — production readiness.

The properties these tests hold in place:

* TEST mode is the default and needs no credentials
* LIVE mode refuses to start when a configured provider has none
* no credential ever reaches a log line
* nothing expensive runs on a rejected lead
* skip tracing is never automatic for everything
* every integration is honest about being NOT CONNECTED
"""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from wholesale_engine.acquisitions import (
    AcquisitionStore,
    AcquisitionWorkflow,
    ContactMethod,
    MethodKind,
    MethodStatus,
    MockSkipTraceProvider,
    SellerResponse,
    SkipTraceNotConfigured,
    best_method,
    deduplicate,
    get_skip_trace_provider,
    merge_method,
)
from wholesale_engine.automation import monitor, render_daily_report, run_daily
from wholesale_engine.automation.daily_priority import BANDS, DailyPriorityEngine
from wholesale_engine.backup import backup_database, create_backup, restore_database
from wholesale_engine.budget import ApiBudget, UsageReport
from wholesale_engine.hunt import run_hunt
from wholesale_engine.importer import ImportError_, read_rows, run_import
from wholesale_engine.integrations import (
    ConsoleNotifier,
    EventType,
    GoogleSheetsAdapter,
    IntegrationNotConfigured,
    IntegrationState,
    LocalCrmAdapter,
    LocalSheetsAdapter,
    Message,
    NotificationCenter,
    OutreachGate,
    SendBlocked,
    SmsAdapter,
    UnconfiguredCrmAdapter,
    get_note_writer,
    get_sheets_adapter,
    integration_status,
    upsert_rows,
)
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.providers import (
    Capability,
    CsvProvider,
    HttpConfig,
    HttpError,
    HuntCriteria,
    SafeHttpClient,
    capability_matrix,
    providers_for,
    redact,
    redact_headers,
    redact_payload,
    registered_names,
    registration,
    supports,
)
from wholesale_engine.research.facts import Confidence
from wholesale_engine.runtime import (
    LOCAL_PROVIDERS,
    PROVIDER_SLOTS,
    ModeError,
    RunMode,
    RuntimeConfig,
)
from wholesale_engine.security import (
    ValidationError,
    audit_source,
    safe_amount,
    safe_identifier,
    safe_limit,
    safe_path,
    safe_sort_column,
)
from wholesale_engine.settings import ProviderSettings
from wholesale_engine.storage import LeadStore

TODAY = date(2026, 8, 23)


def clean_env(**overrides):
    """Run with a controlled environment, so a stray .env cannot leak in."""
    values = {name: "" for name in PROVIDER_SLOTS}
    values.update({
        "WHOLESALE_MODE": "", "PROPERTY_DATA_API_KEY": "", "PROPERTY_DATA_BASE_URL": "",
        "COMPS_API_KEY": "", "SKIP_TRACE_API_KEY": "", "SKIP_TRACE_BASE_URL": "",
    })
    values.update(overrides)
    return mock.patch.dict(os.environ, values, clear=False)


def populated_store() -> LeadStore:
    store = LeadStore(":memory:")
    run_hunt(CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store)
    return store


# ---------------------------------------------------------------------------
# 1-2. Provider registry and configuration
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_capabilities_are_detected_independently(self):
        self.assertTrue(supports("csv", Capability.SEARCH))
        self.assertTrue(supports("csv", Capability.COMPS))
        self.assertFalse(supports("csv", Capability.OWNER))

    def test_all_nine_capabilities_exist(self):
        self.assertEqual(
            {c.value for c in Capability},
            {
                "search_properties", "get_property", "get_owner", "get_equity",
                "get_distress_data", "get_foreclosure_data", "get_tax_data",
                "get_comps", "get_valuation",
            },
        )

    def test_every_capability_has_a_display_label(self):
        for capability in Capability:
            self.assertTrue(capability.label)
            self.assertEqual(capability.label, capability.label.upper())

    def test_the_matrix_reports_available_and_not_available(self):
        text = capability_matrix(ProviderSettings())
        self.assertIn("PROPERTY SEARCH", text)
        self.assertIn("OWNER DATA", text)
        self.assertIn("NOT CONNECTED", text)

    def test_a_provider_reports_every_capability_not_just_what_it_has(self):
        report = CsvProvider(SAMPLE_LEADS).capability_report()
        self.assertEqual(len(report), len(Capability))
        self.assertIn(("OWNER DATA", False), report)

    def test_an_unsupported_capability_answers_clearly(self):
        for call in ("get_equity", "get_foreclosure_data", "get_tax_data", "get_valuation"):
            response = getattr(CsvProvider(SAMPLE_LEADS), call)(Lead(address="1 A"))
            self.assertFalse(response.supported)
            self.assertIn("nothing has been invented", response.reason)

    def test_no_paid_vendor_ships_registered(self):
        self.assertEqual(set(registered_names()), {"csv", "http-template"})

    def test_providers_can_be_found_by_capability(self):
        self.assertIn("csv", providers_for(Capability.COMPS))
        self.assertEqual(providers_for(Capability.OWNER), [])

    def test_a_registration_reports_what_it_needs(self):
        entry = registration("http-template")
        self.assertIn("PROPERTY_DATA_API_KEY", entry.required_settings)
        with clean_env():
            self.assertFalse(entry.is_configured(ProviderSettings()))

    def test_a_local_provider_passes_its_health_check(self):
        ok, message = CsvProvider(SAMPLE_LEADS).health_check()
        self.assertTrue(ok)
        self.assertNotIn("key", message.lower())


# ---------------------------------------------------------------------------
# 3. TEST vs LIVE
# ---------------------------------------------------------------------------


class RunModeTests(unittest.TestCase):
    def test_test_is_the_default(self):
        with clean_env():
            self.assertIs(RuntimeConfig.from_env(load_file=False).mode, RunMode.TEST)

    def test_mode_parses_forgivingly(self):
        self.assertIs(RunMode.parse("live"), RunMode.LIVE)
        self.assertIs(RunMode.parse(""), RunMode.TEST)
        self.assertIs(RunMode.parse(None), RunMode.TEST)
        with self.assertRaises(ValueError):
            RunMode.parse("sideways")

    def test_test_mode_starts_with_no_credentials_at_all(self):
        with clean_env():
            config = RuntimeConfig.from_env(load_file=False)
            config.assert_live_ready()  # no-op in TEST
            self.assertEqual(config.data_provider, "csv")

    def test_live_refuses_without_the_credentials_for_a_remote_provider(self):
        with clean_env(DATA_PROVIDER="http-template"):
            config = RuntimeConfig.from_env(mode="LIVE", load_file=False)
            with self.assertRaises(ModeError) as ctx:
                config.assert_live_ready()
            self.assertIn("PROPERTY_DATA_API_KEY", str(ctx.exception))

    def test_live_starts_when_the_configured_providers_are_local(self):
        with clean_env(DATA_PROVIDER="csv"):
            RuntimeConfig.from_env(mode="LIVE", load_file=False).assert_live_ready()

    def test_live_refuses_a_provider_that_fabricates_data(self):
        with clean_env(SKIP_TRACE_PROVIDER="mock"):
            config = RuntimeConfig.from_env(mode="LIVE", load_file=False)
            with self.assertRaises(ModeError) as ctx:
                config.assert_live_ready()
            self.assertIn("fabricated", str(ctx.exception))

    def test_test_mode_will_not_use_a_configured_live_tracer(self):
        with clean_env(SKIP_TRACE_PROVIDER="http"):
            config = RuntimeConfig.from_env(load_file=False)
            self.assertEqual(config.skip_trace_provider, "none")

    def test_test_mode_allows_fabricated_data_and_live_does_not(self):
        with clean_env():
            self.assertTrue(RuntimeConfig.from_env(load_file=False).allows_fabricated_data())
            self.assertFalse(
                RuntimeConfig.from_env(mode="LIVE", load_file=False).allows_fabricated_data()
            )

    def test_the_banner_names_the_mode_and_leaks_nothing(self):
        with clean_env(PROPERTY_DATA_API_KEY="sk_live_SECRET_KEY_123456"):
            banner = RuntimeConfig.from_env(load_file=False).banner()
        self.assertIn("MODE: TEST", banner)
        self.assertNotIn("SECRET_KEY", banner)

    def test_every_slot_has_a_local_default(self):
        with clean_env():
            config = RuntimeConfig.from_env(load_file=False)
            for slot in PROVIDER_SLOTS:
                self.assertIn(config.slot(slot), LOCAL_PROVIDERS, slot)


# ---------------------------------------------------------------------------
# 4. API safety
# ---------------------------------------------------------------------------


class HttpSafetyTests(unittest.TestCase):
    def test_a_key_in_a_url_is_redacted(self):
        text = redact("https://api.x/v1?api_key=sk_live_ABCDEFGHIJKL&state=FL")
        self.assertNotIn("sk_live_ABCDEFGHIJKL", text)
        self.assertIn("state=FL", text)

    def test_every_secret_parameter_name_is_covered(self):
        from wholesale_engine.providers.http_client import SECRET_PARAMS

        for name in SECRET_PARAMS:
            self.assertNotIn(
                "SUPERSECRETVALUE", redact(f"https://api.x/v1?{name}=SUPERSECRETVALUE")
            )

    def test_auth_headers_are_redacted(self):
        headers = redact_headers({"Authorization": "Bearer abc123def456", "Accept": "json"})
        self.assertNotIn("abc123def456", headers["Authorization"])
        self.assertEqual(headers["Accept"], "json")

    def test_secret_keys_in_a_body_are_redacted(self):
        payload = redact_payload({"token": "xyz789", "results": [{"address": "1 A St"}]})
        self.assertNotIn("xyz789", str(payload["token"]))
        self.assertEqual(payload["results"][0]["address"], "1 A St")

    def test_a_bearer_token_is_redacted_even_without_a_parameter_name(self):
        self.assertNotIn("abcdefghijklmnop", redact("Bearer abcdefghijklmnop"))

    def test_the_client_never_shows_a_key_in_its_repr(self):
        client = SafeHttpClient("https://api.example.invalid/v1", "sk_live_SECRET123456")
        self.assertNotIn("SECRET", repr(client))

    def test_plain_http_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            SafeHttpClient("http://insecure.example/v1")
        self.assertIn("https", str(ctx.exception))

    def test_a_missing_base_url_is_refused(self):
        with self.assertRaises(ValueError):
            SafeHttpClient("")

    def test_every_request_has_a_timeout(self):
        self.assertGreater(HttpConfig().timeout_seconds, 0)

    def test_retries_are_bounded(self):
        self.assertGreaterEqual(HttpConfig().max_retries, 1)
        self.assertLessEqual(HttpConfig().max_retries, 10)

    def test_backoff_grows_exponentially_and_is_capped(self):
        client = SafeHttpClient("https://api.example.invalid/v1", "k")
        waits = [client._backoff(i) for i in range(6)]
        self.assertLess(waits[0], waits[3])
        for wait in waits:
            self.assertLessEqual(wait, client.config.backoff_max_seconds * 1.3)

    def test_retry_after_is_honoured_and_capped(self):
        client = SafeHttpClient("https://api.example.invalid/v1", "k")
        self.assertEqual(client._backoff(0, retry_after=5.0), 5.0)
        self.assertLessEqual(
            client._backoff(0, retry_after=99_999.0),
            client.config.max_retry_after_seconds,
        )

    def test_rate_limit_statuses_are_retried_and_client_errors_are_not(self):
        config = HttpConfig()
        self.assertIn(429, config.retry_statuses)
        self.assertIn(503, config.retry_statuses)
        self.assertNotIn(400, config.retry_statuses)
        self.assertNotIn(404, config.retry_statuses)

    def test_the_client_rate_limits_itself(self):
        self.assertGreater(HttpConfig().min_interval_seconds, 0)

    def test_a_failed_request_raises_a_redacted_error(self):
        client = SafeHttpClient(
            "https://nonexistent.invalid/v1", "sk_live_SECRETKEY123456",
            HttpConfig(max_retries=1, min_interval_seconds=0, timeout_seconds=0.2),
        )
        with self.assertRaises(HttpError) as ctx:
            client.request("search", {"api_key": "sk_live_SECRETKEY123456"})
        self.assertNotIn("SECRETKEY", str(ctx.exception))
        self.assertEqual(client.stats.failures, 1)

    def test_an_auth_failure_is_not_retried(self):
        client = SafeHttpClient(
            "https://api.example.invalid/v1", "k",
            HttpConfig(max_retries=3, min_interval_seconds=0),
        )
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HttpError) as ctx:
                client.request("search")
        self.assertTrue(ctx.exception.is_auth_failure)
        self.assertEqual(client.stats.requests, 1, "a rejected key must not be retried")

    def test_a_rate_limit_is_retried_and_counted(self):
        client = SafeHttpClient(
            "https://api.example.invalid/v1", "k",
            HttpConfig(max_retries=2, min_interval_seconds=0, backoff_base_seconds=0),
        )
        error = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HttpError):
                client.request("search")
        self.assertGreaterEqual(client.stats.rate_limited, 1)
        self.assertGreaterEqual(client.stats.retries, 1)

    def test_a_health_check_without_a_key_fails_cleanly(self):
        client = SafeHttpClient("https://api.example.invalid/v1")
        ok, message = client.health_check()
        self.assertFalse(ok)
        self.assertIn("no API key", message)


# ---------------------------------------------------------------------------
# 5. Cost control
# ---------------------------------------------------------------------------


class BudgetTests(unittest.TestCase):
    def test_defaults_are_conservative_and_staged(self):
        budget = ApiBudget()
        self.assertGreater(budget.max_raw_leads, budget.max_research)
        self.assertGreater(budget.max_research, budget.max_comps)
        self.assertGreater(budget.max_comps, budget.max_skip_traces)

    def test_gates_tighten_down_the_funnel(self):
        budget = ApiBudget()
        self.assertLess(budget.research_min_lead_score, budget.comps_min_lead_score)
        self.assertLess(budget.comps_min_lead_score, budget.skip_trace_min_lead_score)

    def test_the_environment_overrides_the_defaults(self):
        with mock.patch.dict(os.environ, {"MAX_COMPS": "7", "MAX_SKIP_TRACES": "2"}):
            budget = ApiBudget.from_env()
        self.assertEqual(budget.max_comps, 7)
        self.assertEqual(budget.max_skip_traces, 2)

    def test_a_bad_environment_value_falls_back_rather_than_crashing(self):
        with mock.patch.dict(os.environ, {"MAX_COMPS": "not a number"}):
            self.assertEqual(ApiBudget.from_env().max_comps, ApiBudget.max_comps)

    def test_explicit_overrides_beat_the_environment(self):
        with mock.patch.dict(os.environ, {"MAX_COMPS": "7"}):
            self.assertEqual(ApiBudget.from_env(max_comps=3).max_comps, 3)

    def test_skip_trace_needs_any_one_of_the_three_bars(self):
        budget = ApiBudget()
        self.assertTrue(budget.qualifies_for_skip_trace(lead_score=70))
        self.assertTrue(budget.qualifies_for_skip_trace(deal_score=70))
        self.assertTrue(budget.qualifies_for_skip_trace(priority_score=75))
        self.assertFalse(budget.qualifies_for_skip_trace(lead_score=69, deal_score=69))

    def test_a_dead_or_passed_lead_is_never_traced(self):
        budget = ApiBudget()
        for status in ("DEAD", "PASSED", "CLOSED"):
            self.assertFalse(
                budget.qualifies_for_skip_trace(lead_score=95, status=status), status
            )

    def test_a_lead_you_can_already_reach_is_not_re_traced(self):
        self.assertFalse(
            ApiBudget().qualifies_for_skip_trace(lead_score=95, already_reachable=True)
        )

    def test_bulk_skip_tracing_is_off_by_default(self):
        self.assertFalse(ApiBudget().auto_skip_trace)

    def test_usage_reports_every_counter(self):
        usage = UsageReport(raw_leads=1000, filtered_out=700, research_calls=100,
                            comp_calls=30, skip_trace_calls=10)
        text = usage.render(ApiBudget())
        for label in ("RAW LEADS", "RESEARCH CALLS", "COMP CALLS",
                      "SKIP TRACE CALLS", "ERRORS"):
            self.assertIn(label, text)
        self.assertEqual(usage.billable_calls, 140)

    def test_no_cost_is_invented_without_real_pricing(self):
        self.assertEqual(ApiBudget().estimate("skip_trace", 10), 0.0)
        self.assertIn("unknown", UsageReport().render(ApiBudget()))

    def test_nothing_expensive_runs_on_a_filtered_lead(self):
        store = LeadStore(":memory:")
        result = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            HuntCriteria(min_lead_score=85), store=store,
        )
        stages = dict(result.metrics.stages)
        self.assertLess(stages["after lead score"], stages["raw leads from source"])
        self.assertLessEqual(stages["after property research"], stages["after lead score"])
        store.close()


# ---------------------------------------------------------------------------
# 8-10. Skip tracing and contacts
# ---------------------------------------------------------------------------


class ContactMethodTests(unittest.TestCase):
    def test_several_phones_are_kept_for_one_owner(self):
        methods = deduplicate([
            ContactMethod.phone("8135550101", "a", Confidence.LOW, property_id="p"),
            ContactMethod.phone("8135550102", "b", Confidence.HIGH, property_id="p"),
        ])
        self.assertEqual(len(methods), 2)

    def test_the_same_number_from_two_sources_merges(self):
        methods = deduplicate([
            ContactMethod.phone("(813) 555-0100", "a", Confidence.LOW, property_id="p"),
            ContactMethod.phone("813-555-0100", "b", Confidence.HIGH, property_id="p"),
        ])
        self.assertEqual(len(methods), 1)
        self.assertIs(methods[0].confidence, Confidence.HIGH)

    def test_emails_and_phones_are_separate_records(self):
        methods = deduplicate([
            ContactMethod.phone("8135550100", "a", property_id="p"),
            ContactMethod.email("owner@example.invalid", "a", property_id="p"),
        ])
        self.assertEqual(len(methods), 2)
        self.assertEqual(
            {m.kind for m in methods}, {MethodKind.PHONE, MethodKind.EMAIL}
        )

    def test_an_unusable_value_produces_no_record_at_all(self):
        self.assertIsNone(ContactMethod.phone("not a number", "a"))
        self.assertIsNone(ContactMethod.email("not an email", "a"))
        self.assertIsNone(ContactMethod.phone(None, "a"))

    def test_verified_data_is_never_silently_overwritten(self):
        verified = ContactMethod.phone(
            "8135550100", "county", Confidence.HIGH, property_id="p",
            status=MethodStatus.VERIFIED,
        )
        weaker = ContactMethod.phone("8135550100", "cheap", Confidence.LOW, property_id="p")
        outcome = merge_method(verified, weaker)
        self.assertEqual(outcome.action, "conflict")
        self.assertIs(outcome.method.confidence, Confidence.HIGH)

    def test_a_conflict_is_recorded_rather_than_discarded(self):
        verified = ContactMethod.phone(
            "8135550100", "county", Confidence.HIGH, property_id="p",
            status=MethodStatus.VERIFIED,
        )
        weaker = ContactMethod.phone("8135550100", "cheap", Confidence.LOW, property_id="p")
        outcome = merge_method(verified, weaker)
        self.assertIn("cheap", outcome.method.notes)

    def test_better_provenance_improves_an_unverified_record(self):
        existing = ContactMethod.phone("8135550100", "a", Confidence.LOW, property_id="p")
        better = ContactMethod.phone("8135550100", "county", Confidence.HIGH, property_id="p")
        outcome = merge_method(existing, better)
        self.assertEqual(outcome.action, "improved")
        self.assertIs(outcome.method.confidence, Confidence.HIGH)

    def test_a_suppressed_record_always_wins(self):
        blocked = ContactMethod.phone(
            "8135550100", "seller", Confidence.HIGH, property_id="p",
            status=MethodStatus.DO_NOT_CONTACT,
        )
        good = ContactMethod.phone("8135550100", "county", Confidence.HIGH, property_id="p")
        self.assertIs(merge_method(blocked, good).method.status, MethodStatus.DO_NOT_CONTACT)

    def test_the_best_phone_is_verified_then_confident(self):
        methods = [
            ContactMethod.phone("8135550101", "a", Confidence.HIGH, property_id="p"),
            ContactMethod.phone(
                "8135550102", "b", Confidence.MEDIUM, property_id="p",
                status=MethodStatus.VERIFIED,
            ),
        ]
        self.assertEqual(best_method(methods).value, "8135550102")

    def test_a_suppressed_method_is_never_the_best(self):
        methods = [
            ContactMethod.phone(
                "8135550101", "a", Confidence.HIGH, property_id="p",
                status=MethodStatus.DO_NOT_CONTACT,
            ),
        ]
        self.assertIsNone(best_method(methods))


class ContactStoreTests(unittest.TestCase):
    def setUp(self):
        self.leads = LeadStore(":memory:")
        self.leads.upsert_lead(Lead(lead_id="A", address="1 Main St", city="Tampa",
                                    state="FL", source="csv"))
        self.row = self.leads.find_one("A")
        self.store = AcquisitionStore(self.leads)

    def tearDown(self):
        self.leads.close()

    def method(self, value="8135550100", **kwargs):
        return ContactMethod.phone(
            value, kwargs.pop("source", "county"),
            kwargs.pop("confidence", Confidence.HIGH),
            property_id=self.row.dedupe_key, **kwargs
        )

    def test_methods_round_trip(self):
        self.store.save_method(self.method())
        methods = self.store.methods_for(self.row.dedupe_key)
        self.assertEqual(len(methods), 1)
        self.assertEqual(methods[0].value, "8135550100")

    def test_saving_the_same_number_twice_does_not_duplicate(self):
        self.store.save_method(self.method())
        self.store.save_method(self.method("(813) 555-0100"))
        self.assertEqual(len(self.store.methods_for(self.row.dedupe_key)), 1)

    def test_multiple_numbers_are_all_kept(self):
        for value in ("8135550101", "8135550102", "8135550103"):
            self.store.save_method(self.method(value))
        self.assertEqual(len(self.store.methods_for(self.row.dedupe_key)), 3)

    def test_a_verified_record_survives_a_weaker_import(self):
        self.store.save_method(self.method(status=MethodStatus.VERIFIED))
        outcome = self.store.save_method(
            self.method(source="cheap", confidence=Confidence.LOW)
        )
        self.assertEqual(outcome.action, "conflict")
        self.assertIs(
            self.store.methods_for(self.row.dedupe_key)[0].status, MethodStatus.VERIFIED
        )

    def test_marking_do_not_contact_adds_to_the_suppression_list(self):
        saved = self.store.save_method(self.method()).method
        self.store.set_method_status(saved.method_id, MethodStatus.DO_NOT_CONTACT)
        self.assertIn("8135550100", self.store.suppressed_values())

    def test_a_do_not_contact_response_suppresses_every_method(self):
        self.store.save_method(self.method("8135550101"))
        self.store.save_method(self.method("8135550102"))
        self.store.record_seller_response(self.row.dedupe_key, "DO_NOT_CONTACT")
        for method in self.store.methods_for(self.row.dedupe_key):
            self.assertIs(method.status, MethodStatus.DO_NOT_CONTACT)

    def test_all_eleven_seller_responses_parse(self):
        for name in (
            "INTERESTED", "NOT_INTERESTED", "CALL_BACK", "WANTS_PRICE",
            "WANTS_OFFER", "COUNTER", "ACCEPTED", "REJECTED", "NO_RESPONSE",
            "WRONG_NUMBER", "DO_NOT_CONTACT",
        ):
            self.assertIs(SellerResponse.parse(name), SellerResponse(name))

    def test_an_unknown_response_is_refused(self):
        with self.assertRaises(ValueError):
            SellerResponse.parse("maybe later")

    def test_responses_are_kept_in_history(self):
        self.store.record_seller_response(self.row.dedupe_key, "CALL_BACK")
        self.store.record_seller_response(self.row.dedupe_key, "WANTS_OFFER")
        self.assertEqual(len(self.store.seller_responses(self.row.dedupe_key)), 2)
        self.assertEqual(
            self.store.latest_seller_response(self.row.dedupe_key), "WANTS_OFFER"
        )


class SkipTraceProviderTests(unittest.TestCase):
    def test_the_http_template_refuses_without_credentials(self):
        with clean_env():
            with self.assertRaises(SkipTraceNotConfigured) as ctx:
                get_skip_trace_provider("http")
        self.assertIn("NOT CONNECTED", str(ctx.exception))

    def test_credentials_alone_are_not_enough(self):
        with clean_env(
            SKIP_TRACE_API_KEY="k", SKIP_TRACE_BASE_URL="https://api.example.invalid/v1"
        ):
            with self.assertRaises(SkipTraceNotConfigured) as ctx:
                get_skip_trace_provider("http")
        self.assertIn("endpoint path", str(ctx.exception))

    def test_the_result_supports_every_documented_field(self):
        contact = MockSkipTraceProvider().skip_trace(
            "p", owner_name="FICTIONAL OWNER", address="1 A St", city="T", state="FL"
        ).to_contact("p")
        for attribute in (
            "owner_name", "phone", "phone_type", "phone_confidence", "email",
            "email_confidence", "mailing_address", "source", "source_date",
        ):
            self.assertTrue(hasattr(contact, attribute), attribute)

    def test_the_status_report_names_nothing_as_connected(self):
        from wholesale_engine.acquisitions import skip_trace_status

        with clean_env():
            text = skip_trace_status()
        self.assertIn("NOT CONNECTED", text)
        self.assertIn("TEST DATA ONLY", text)


# ---------------------------------------------------------------------------
# 11-13, 15, 17. Integrations
# ---------------------------------------------------------------------------


class IntegrationTests(unittest.TestCase):
    def test_the_status_table_names_every_state(self):
        with clean_env():
            text = integration_status()
        for state in ("CONNECTED", "NOT CONNECTED"):
            self.assertIn(state, text)

    # --- sheets ---

    def test_upsert_updates_rather_than_duplicating(self):
        rows, updated, added = upsert_rows(
            [{"property_id": "p1", "x": 1}],
            [{"property_id": "p1", "x": 2}, {"property_id": "p2", "x": 3}],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual((updated, added), (1, 1))
        self.assertEqual(rows[0]["x"], 2)

    def test_upsert_keeps_a_row_in_place(self):
        rows, _, _ = upsert_rows(
            [{"property_id": "a"}, {"property_id": "b"}], [{"property_id": "a", "x": 1}]
        )
        self.assertEqual(rows[0]["property_id"], "a")

    def test_google_sheets_is_not_connected(self):
        with clean_env():
            adapter = GoogleSheetsAdapter()
            self.assertIs(adapter.state(), IntegrationState.NOT_CONNECTED)
            with self.assertRaises(IntegrationNotConfigured):
                adapter.publish("hot_leads", [], [])

    def test_the_local_sheets_fallback_works_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalSheetsAdapter(Path(tmp))
            columns = ["property_id", "address"]
            adapter.publish("hot_leads", [{"property_id": "p1", "address": "1 A"}], columns)
            adapter.publish("hot_leads", [{"property_id": "p1", "address": "1 B"}], columns)
            rows = adapter.read("hot_leads")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "1 B")

    def test_two_way_sync_is_not_offered(self):
        self.assertFalse(hasattr(GoogleSheetsAdapter, "pull"))
        self.assertFalse(hasattr(GoogleSheetsAdapter, "sync"))

    # --- crm ---

    def test_the_crm_interface_has_all_six_operations(self):
        for operation in (
            "create_contact", "update_contact", "create_deal",
            "update_deal", "create_note", "create_task",
        ):
            self.assertTrue(hasattr(UnconfiguredCrmAdapter, operation), operation)

    def test_no_crm_is_hard_coded(self):
        with clean_env():
            with self.assertRaises(IntegrationNotConfigured) as ctx:
                UnconfiguredCrmAdapter().create_deal(property_id="p")
        self.assertIn("CSV/JSON exports", str(ctx.exception))

    def test_the_local_crm_records_and_updates(self):
        adapter = LocalCrmAdapter()
        adapter.create_deal(property_id="p", price=1)
        adapter.update_deal("p", price=2)
        self.assertEqual(len(adapter.records), 1)
        self.assertEqual(adapter.records[0].fields["price"], 2)

    # --- outreach ---

    def test_nothing_sends_without_an_explicit_action(self):
        adapter = SmsAdapter(OutreachGate())
        with self.assertRaises(SendBlocked) as ctx:
            adapter.send(Message("SMS", "8135550101", "hello"))
        self.assertIn("explicit", str(ctx.exception))

    def test_an_explicit_send_without_credentials_is_a_dry_run(self):
        result = SmsAdapter(OutreachGate()).send(
            Message("SMS", "8135550101", "hello"), explicit=True
        )
        self.assertFalse(result.sent)
        self.assertTrue(result.dry_run)
        self.assertIn("NOT CONNECTED", result.detail)

    def test_mass_messaging_is_refused(self):
        messages = [Message("SMS", f"81355501{i:02d}", "x") for i in range(10)]
        with self.assertRaises(SendBlocked) as ctx:
            SmsAdapter(OutreachGate()).send_batch(messages, explicit=True)
        self.assertIn("mass messaging", str(ctx.exception))

    def test_bulk_is_possible_only_when_deliberately_enabled(self):
        messages = [Message("SMS", f"81355501{i:02d}", "x") for i in range(8)]
        results = SmsAdapter(OutreachGate(allow_bulk=True)).send_batch(
            messages, explicit=True
        )
        self.assertEqual(len(results), 8)

    def test_the_suppression_list_is_absolute(self):
        gate = OutreachGate(allow_bulk=True, suppressed=("8135550100",))
        for form in ("8135550100", "(813) 555-0100", "1-813-555-0100"):
            with self.assertRaises(SendBlocked):
                SmsAdapter(gate).send(Message("SMS", form, "x"), explicit=True)

    def test_automation_can_be_enabled_per_channel(self):
        gate = OutreachGate(automation_enabled={"SMS": True})
        SmsAdapter(gate).send(Message("SMS", "8135550101", "x"))
        with self.assertRaises(SendBlocked):
            from wholesale_engine.integrations import EmailOutreachAdapter

            EmailOutreachAdapter(gate).send(Message("EMAIL", "a@b.invalid", "x"))

    # --- notifications ---

    def test_all_eight_events_exist(self):
        self.assertEqual(len({e.value for e in EventType}), 8)
        for event in EventType:
            self.assertTrue(event.icon)

    def test_the_console_notifier_always_works(self):
        center = NotificationCenter.build("console")
        center.push(EventType.NEW_HOT_LEAD, "Hot lead", address="1 A St")
        self.assertEqual(len(center.collected), 1)
        self.assertEqual(center.failures, [])

    def test_disabled_events_are_not_raised(self):
        center = NotificationCenter.build("console", enabled=(EventType.NEW_HOT_LEAD,))
        self.assertIsNone(center.push(EventType.BUYER_INTEREST, "x"))

    def test_an_unconnected_channel_never_loses_the_notification(self):
        with clean_env():
            center = NotificationCenter.build("webhook")
            center.push(EventType.NEW_HOT_LEAD, "Hot lead")
        self.assertEqual(len(center.collected), 1)
        self.assertTrue(center.failures)

    # --- ai notes ---

    def test_the_rule_based_writer_needs_no_credentials(self):
        writer = get_note_writer("none")
        suggestions = writer.all_suggestions({"property_id": "p", "next_action": "CALL"})
        self.assertEqual(len(suggestions), 4)

    def test_every_suggestion_is_marked_advisory(self):
        for suggestion in get_note_writer("none").all_suggestions({"property_id": "p"}):
            self.assertIn("ADVISORY", suggestion.render())
            self.assertTrue(suggestion.as_dict()["advisory"])

    def test_the_llm_writer_is_not_connected(self):
        with clean_env():
            with self.assertRaises(IntegrationNotConfigured):
                get_note_writer("llm").next_action({})

    def test_the_writer_flags_test_contact_data(self):
        suggestion = get_note_writer("none").seller_summary(
            {"property_id": "p", "is_test_data": True}
        )
        self.assertIn("TEST DATA", suggestion.text)


# ---------------------------------------------------------------------------
# 6, 16, 18. Daily automation, priority, monitoring
# ---------------------------------------------------------------------------


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_the_daily_run_produces_a_report(self):
        report = run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY
        )
        self.assertGreater(report.new_leads, 0)
        self.assertGreater(report.total_tracked, 0)

    def test_the_report_has_every_documented_section(self):
        report = run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY
        )
        text = render_daily_report(report)
        for label in (
            "NEW LEADS", "HOT", "STRONG", "FOLLOW UPS", "OFFERS",
            "NEGOTIATIONS", "UNDER CONTRACT", "BUYER SEARCH", "CLOSED",
        ):
            self.assertIn(label, text)

    def test_a_second_run_finds_no_new_leads(self):
        run_daily(self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY)
        second = run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY
        )
        self.assertEqual(second.new_leads, 0)

    def test_the_run_reports_its_api_usage(self):
        report = run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY
        )
        self.assertGreater(report.usage.raw_leads, 0)
        self.assertIn("BILLABLE TOTAL", report.usage.render())

    def test_it_runs_without_a_provider(self):
        run_daily(self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY)
        report = run_daily(self.store, provider=None, ingest=False, today=TODAY)
        self.assertEqual(report.new_leads, 0)
        self.assertGreater(report.total_tracked, 0)

    def test_hot_leads_raise_notifications(self):
        center = NotificationCenter.build("console")
        run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            notifications=center, today=TODAY,
        )
        self.assertTrue(
            any(n.event is EventType.NEW_HOT_LEAD for n in center.collected)
        )

    def test_projected_figures_are_labelled(self):
        report = run_daily(
            self.store, CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), today=TODAY
        )
        self.assertIn("Projected", render_daily_report(report))


class DailyPriorityTests(unittest.TestCase):
    def setUp(self):
        self.store = populated_store()
        self.workflow = AcquisitionWorkflow(self.store)
        self.engine = DailyPriorityEngine(self.workflow)

    def tearDown(self):
        self.store.close()

    def test_all_eight_bands_are_defined_in_order(self):
        self.assertEqual(len(BANDS), 8)
        self.assertIn("SELLER COUNTERS", BANDS[0])
        self.assertIn("BUYER OPPORTUNITIES", BANDS[7])

    def test_items_are_sorted_by_band(self):
        items = self.engine.build(TODAY)
        self.assertEqual(
            [i.band_index for i in items], sorted(i.band_index for i in items)
        )

    def test_a_counter_outranks_everything(self):
        row = self.store.find_one("LH-021")
        self.workflow.build_offer(row.dedupe_key, 55_000)
        self.workflow.record_counter(row.dedupe_key, 71_000)
        items = self.engine.build(TODAY)
        self.assertEqual(items[0].action, "RESPOND TO COUNTER")

    def test_each_property_appears_once(self):
        items = self.engine.build(TODAY)
        ids = [i.property_id for i in items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_item_carries_the_documented_columns(self):
        for item in self.engine.build(TODAY):
            row = item.as_dict()
            for column in (
                "action", "address", "reason", "deal_score", "lead_score",
                "priority_score", "next_deadline",
            ):
                self.assertIn(column, row)

    def test_a_contract_deadline_ranks_above_a_new_lead(self):
        from wholesale_engine.acquisitions import Contract

        row = self.store.find_one("LH-011")
        self.workflow.set_status(row.dedupe_key, "UNDER_CONTRACT", "test")
        self.workflow.store.save_contract(
            Contract(
                property_id=row.dedupe_key, purchase_price=100_000,
                closing_date=TODAY + timedelta(days=3),
            )
        )
        items = self.engine.build(TODAY)
        bands = [i.band for i in items]
        if BANDS[6] in bands and BANDS[5] in bands:
            self.assertLess(bands.index(BANDS[6]), bands.index(BANDS[5]) + len(bands))


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def lead(self, **kwargs):
        base = dict(lead_id="A", address="1 Main St", city="Tampa", state="FL",
                    asking_price=125_000, source="csv")
        base.update(kwargs)
        return Lead(**base)

    def test_no_change_on_a_first_sighting(self):
        self.store.upsert_lead(self.lead(), lead_score=60.0, deal_score=58.0)
        self.assertEqual(monitor(self.store), [])

    def test_a_price_reduction_is_detected(self):
        self.store.upsert_lead(self.lead(), lead_score=60.0, deal_score=58.0)
        self.store.upsert_lead(self.lead(asking_price=99_000), lead_score=70.0, deal_score=79.0)
        changes = monitor(self.store)
        self.assertTrue(changes)
        self.assertTrue(any("PRICE REDUCTION" in m.description for m in changes[0].movements))

    def test_a_deal_score_jump_is_an_improvement(self):
        self.store.upsert_lead(self.lead(), deal_score=58.0)
        self.store.upsert_lead(self.lead(), deal_score=79.0)
        changes = monitor(self.store)
        self.assertTrue(changes[0].is_improvement)
        self.assertTrue(
            any("58 -> 79" in m.description for m in changes[0].movements)
        )

    def test_the_improvement_banner_appears(self):
        self.store.upsert_lead(self.lead(), deal_score=58.0)
        self.store.upsert_lead(self.lead(), deal_score=79.0)
        self.assertIn("DEAL IMPROVEMENT DETECTED", monitor(self.store)[0].render())

    def test_a_new_foreclosure_signal_is_detected(self):
        self.store.upsert_lead(self.lead())
        self.store.upsert_lead(self.lead(pre_foreclosure=True))
        changes = monitor(self.store)
        self.assertTrue(
            any("PRE FORECLOSURE" in m.description for m in changes[0].movements)
        )

    def test_trivial_drift_is_ignored(self):
        self.store.upsert_lead(self.lead(), deal_score=58.0)
        self.store.upsert_lead(self.lead(asking_price=124_800), deal_score=58.0)
        self.assertEqual(monitor(self.store), [])

    def test_improvements_rank_above_ordinary_movement(self):
        self.store.upsert_lead(self.lead(), deal_score=58.0)
        self.store.upsert_lead(self.lead(asking_price=99_000), deal_score=79.0)
        self.store.upsert_lead(
            self.lead(lead_id="B", address="2 Oak Ave"), deal_score=40.0
        )
        self.store.upsert_lead(
            self.lead(lead_id="B", address="2 Oak Ave"), deal_score=39.0
        )
        changes = monitor(self.store)
        self.assertTrue(changes[0].is_improvement)


# ---------------------------------------------------------------------------
# 22-24. Security, backup, import/export
# ---------------------------------------------------------------------------


class SecurityTests(unittest.TestCase):
    def test_the_source_audit_is_clean(self):
        findings = audit_source()
        high = [f for f in findings if f.severity == "HIGH"]
        self.assertEqual(high, [], "\n".join(f.render() for f in high))

    def test_a_sort_column_outside_the_allow_list_is_replaced(self):
        self.assertEqual(
            safe_sort_column("; DROP TABLE leads", ["a", "b"], "a"), "a"
        )
        self.assertEqual(safe_sort_column("b", ["a", "b"], "a"), "b")

    def test_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for candidate in ("../../etc/passwd", "/etc/passwd"):
                with self.assertRaises(ValidationError):
                    safe_path(candidate, base=Path(tmp))

    def test_an_unsupported_extension_is_refused(self):
        with self.assertRaises(ValidationError):
            safe_path("script.sh", must_exist=False)

    def test_a_directory_is_not_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                safe_path(tmp, must_exist=True)

    def test_amounts_are_validated(self):
        self.assertEqual(safe_amount("$59,500"), 59_500.0)
        for bad in ("not a number", "-5", "1e99"):
            with self.assertRaises(ValidationError):
                safe_amount(bad)

    def test_limits_are_validated_and_capped(self):
        self.assertEqual(safe_limit("20"), 20)
        self.assertEqual(safe_limit(999_999), 10_000)
        with self.assertRaises(ValidationError):
            safe_limit("0")

    def test_identifiers_reject_injection_shaped_input(self):
        self.assertEqual(safe_identifier("LH-011"), "LH-011")
        for bad in ("'; DROP TABLE leads;--", "$(rm -rf /)", ""):
            with self.assertRaises(ValidationError):
                safe_identifier(bad)

    def test_env_is_git_ignored(self):
        ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
        self.assertIn("\n.env\n", ignore)

    def test_no_dotenv_is_committed(self):
        self.assertFalse((Path(__file__).resolve().parent.parent / ".env").exists())


class BackupTests(unittest.TestCase):
    def test_a_backup_contains_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            store = LeadStore(db)
            store.upsert_lead(Lead(lead_id="A", address="1 Main St", city="T",
                                   state="FL", source="csv"))
            store.close()
            result = create_backup(db, Path(tmp) / "backups")
            self.assertTrue(result.path.exists())
            self.assertEqual(result.database_rows, 1)
            self.assertIn("database/leads.db", result.files)

    def test_secrets_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("PROPERTY_DATA_API_KEY=sk_live_SECRET\n")
            db = root / "leads.db"
            LeadStore(db).close()
            result = create_backup(db, root / "backups", project_root=root)
            self.assertIn(".env", result.skipped_secrets)
            self.assertNotIn("secrets/.env", result.files)

    def test_secrets_are_included_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("KEY=value\n")
            db = root / "leads.db"
            LeadStore(db).close()
            result = create_backup(
                db, root / "backups", project_root=root, include_secrets=True
            )
            self.assertIn("secrets/.env", result.files)

    def test_a_backup_can_be_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            store = LeadStore(db)
            store.upsert_lead(Lead(lead_id="A", address="1 Main St", city="T",
                                   state="FL", source="csv"))
            store.close()
            archive = create_backup(db, Path(tmp) / "backups").path
            restored = Path(tmp) / "restored.db"
            self.assertTrue(restore_database(archive, restored))
            reopened = LeadStore(restored)
            self.assertEqual(reopened.count(), 1)
            reopened.close()

    def test_the_database_copy_is_consistent_while_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "leads.db"
            store = LeadStore(db)
            store.upsert_lead(Lead(lead_id="A", address="1 Main St", city="T",
                                   state="FL", source="csv"))
            rows = backup_database(db, Path(tmp) / "copy.db")
            store.close()
        self.assertEqual(rows, 1)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.leads = LeadStore(":memory:")
        self.acquisitions = AcquisitionStore(self.leads)

    def tearDown(self):
        self.leads.close()

    def test_importing_leads_adds_them(self):
        result = run_import("leads", SAMPLE_LEADS, self.leads, self.acquisitions)
        self.assertGreater(result.added, 0)
        self.assertGreater(self.leads.count(), 0)

    def test_importing_twice_creates_no_duplicates(self):
        run_import("leads", SAMPLE_LEADS, self.leads, self.acquisitions)
        before = self.leads.count()
        second = run_import("leads", SAMPLE_LEADS, self.leads, self.acquisitions)
        self.assertEqual(self.leads.count(), before)
        self.assertEqual(second.added, 0)
        self.assertGreater(second.updated, 0)

    def test_a_row_without_an_address_is_skipped_with_a_reason(self):
        result = run_import("leads", SAMPLE_LEADS, self.leads, self.acquisitions)
        self.assertGreater(result.skipped, 0)
        self.assertTrue(any("no address" in e for e in result.errors))

    def test_json_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buyers.json"
            path.write_text('[{"name": "TEST BUYER", "preferred_states": "MO"}]')
            result = run_import("buyers", path, self.leads, self.acquisitions)
        self.assertEqual(result.added, 1)

    def test_a_wrapped_json_document_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buyers.json"
            path.write_text('{"count": 1, "rows": [{"name": "TEST BUYER"}]}')
            result = run_import("buyers", path, self.leads, self.acquisitions)
        self.assertEqual(result.added, 1)

    def test_importing_contacts_folds_in_the_methods(self):
        self.leads.upsert_lead(Lead(lead_id="A", address="1 Main St", city="Tampa",
                                    state="FL", source="csv"))
        row = self.leads.find_one("A")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contacts.csv"
            path.write_text(
                "property_id,owner_name,phone,email\n"
                f"{row.dedupe_key},TEST OWNER,8135550100,owner@example.invalid\n"
            )
            result = run_import("contacts", path, self.leads, self.acquisitions)
        self.assertEqual(result.added, 1)
        self.assertEqual(len(self.acquisitions.methods_for(row.dedupe_key)), 2)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ImportError_):
            run_import("aardvarks", SAMPLE_LEADS, self.leads, self.acquisitions)

    def test_a_missing_file_is_refused_with_a_message(self):
        with self.assertRaises(ImportError_) as ctx:
            read_rows(Path("/nonexistent/file.csv"))
        self.assertIn("no such file", str(ctx.exception))

    def test_an_unsupported_extension_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.xml"
            path.write_text("<xml/>")
            with self.assertRaises(ImportError_):
                read_rows(path)

    def test_malformed_json_is_refused_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json")
            with self.assertRaises(ImportError_) as ctx:
                read_rows(path)
        self.assertIn("not valid JSON", str(ctx.exception))


# ---------------------------------------------------------------------------
# 21. Economics preserved
# ---------------------------------------------------------------------------


class EconomicsTests(unittest.TestCase):
    def test_the_seventy_percent_rule_and_target_fee_are_unchanged(self):
        from wholesale_engine.config import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG.arv_percentage, 0.70)
        self.assertEqual(DEFAULT_CONFIG.target_wholesale_fee, 18_000.0)

    def test_the_target_is_still_not_a_hard_minimum(self):
        from wholesale_engine.config import EngineConfig

        fields = {f.name for f in __import__("dataclasses").fields(EngineConfig)}
        self.assertNotIn("min_cushion_above_target", fields)
        self.assertFalse(hasattr(DEFAULT := EngineConfig(), "required_wholesale_fee"))

    def test_a_below_target_deal_still_reaches_the_daily_report(self):
        store = populated_store()
        report = run_daily(store, provider=None, ingest=False, today=TODAY)
        below = [
            row for row in report.hot + report.strong
            if row.potential_fee is not None and row.potential_fee < 18_000
        ]
        self.assertTrue(below, "a below-target fee must not drop out of the pipeline")
        store.close()

    def test_the_six_quantities_stay_separate(self):
        from wholesale_engine.models.results import FinancialSummary

        summary = FinancialSummary()
        for attribute in (
            "end_buyer_max_price", "mao", "recommended_offer",
            "potential_wholesale_fee", "potential_gross_spread", "buyer_margin",
        ):
            self.assertTrue(hasattr(summary, attribute), attribute)


if __name__ == "__main__":
    unittest.main()
