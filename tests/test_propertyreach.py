"""PropertyReach adapter — every test mocked, no request ever leaves the machine.

Three properties these tests exist to hold:

* **no live call, ever, from a test run** — the transport is either injected or
  monkeypatched, and one test asserts the default provider makes no call at all
* **an unconfirmed endpoint is refused, not guessed** — the vendor's REST paths
  for search/detail/comps are not confirmed, and the adapter must say so rather
  than fire a request at a made-up path
* **a field the vendor did not return stays unknown** — never zero, never a
  blank string standing in for an answer
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
from datetime import date
from unittest import mock

from wholesale_engine.budget import ApiBudget, UsageReport
from wholesale_engine.config import (
    MAX_PROPERTY_PRICE,
    MIN_PROPERTY_PRICE,
)
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.models.enums import Occupancy, PropertyType
from wholesale_engine.providers import registry
from wholesale_engine.providers.base import Capability, ProviderNotConfigured
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.providers.http_client import HttpConfig, HttpError, SafeHttpClient
from wholesale_engine.providers.propertyreach import (
    PropertyReachProvider,
    ReachUsage,
    dig,
    extract,
    first_list,
    to_comp,
    to_lead,
)
from wholesale_engine.providers import propertyreach_schema as schema
from wholesale_engine.settings import ProviderSettings


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClient:
    """Stands in for SafeHttpClient. Records calls; never touches a socket."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, path, params=None, method="GET", body=None):
        self.calls.append({"path": path, "method": method, "body": body})
        if self.error is not None:
            raise self.error
        if not self.responses:
            return {}
        return self.responses.pop(0)


#: A PropertyReach-shaped record using the candidate key names in the schema.
#: FICTIONAL. Every value here is test data, not a real property.
SAMPLE_RECORD = {
    "propertyId": "PR-TEST-001",
    "address": {"line1": "18 Testcase Avenue", "city": "Springfield",
                "state": "MO", "zip": "65801", "county": "Greene"},
    "propertyType": "Single Family",
    "bedrooms": 3,
    "bathrooms": 2,
    "buildingSqft": "1,480",
    "yearBuilt": 1968,
    "estimatedValue": 245000,
    "listPrice": 155000,
    "lastSalePrice": 98000,
    "lastSaleDate": "2011-06-14",
    "owner": {"name": "TEST OWNER", "mailingAddress": "PO Box 1 (TEST)"},
    "ownerOccupied": False,
    "mortgageBalance": 42000,
    "estimatedEquity": 203000,
    "taxAmount": 2100,
    "assessedValue": 190000,
    "taxDelinquent": True,
    "vacant": True,
    "absenteeOwner": True,
    "preForeclosure": False,
    "daysOnMarket": 91,
}

SAMPLE_COMP = {
    "address": {"line1": "22 Testcase Avenue"},
    "salePrice": 239000,
    "saleDate": "2025-11-02",
    "bedrooms": 3,
    "bathrooms": 2,
    "buildingSqft": 1500,
    "distance": 0.3,
}


def make_provider(client=None, allow_unverified=True, max_reach=100):
    """A provider with an injected transport. Reads no credentials."""
    return PropertyReachProvider(
        settings=ProviderSettings(),
        max_reach=max_reach,
        allow_unverified=allow_unverified,
        client=client if client is not None else FakeClient(),
    )


# ---------------------------------------------------------------------------


class SchemaHonestyTests(unittest.TestCase):
    """What the schema claims to know must match what is actually confirmed."""

    def test_only_skip_trace_has_a_confirmed_path(self):
        self.assertTrue(schema.ENDPOINTS["skip_trace"].verified)
        for key in ("search", "detail", "comps"):
            self.assertFalse(
                schema.ENDPOINTS[key].verified,
                f"{key} must not be marked confirmed until the docs are read",
            )

    def test_unverified_endpoints_are_listed(self):
        self.assertEqual(
            sorted(schema.unverified_endpoints()), ["comps", "detail", "search"]
        )

    def test_confirmed_facts(self):
        self.assertEqual(schema.DEFAULT_BASE_URL, "https://api.propertyreach.com/v1")
        self.assertEqual(schema.AUTH_HEADER, "x-api-key")
        self.assertEqual(schema.AUTH_SCHEME, "")
        self.assertEqual(schema.ENDPOINTS["skip_trace"].path, "skip-trace")

    def test_status_says_what_is_unverified(self):
        text = schema.schema_status()
        self.assertIn("UNVERIFIED", text)
        self.assertIn("api.propertyreach.com", text)

    def test_no_credential_is_hard_coded(self):
        source = ""
        for path in (schema.__file__, schema.__file__.replace("_schema", "")):
            with open(path, encoding="utf-8") as handle:
                source += handle.read()
        self.assertNotIn("x-api-key: ", source)
        for marker in ("sk_live", "sk_test", "Bearer ey"):
            self.assertNotIn(marker, source)


class ValueExtractionTests(unittest.TestCase):

    def test_dig_walks_nested_objects(self):
        self.assertEqual(dig({"a": {"b": {"c": 1}}}, "a.b.c"), 1)
        self.assertIsNone(dig({"a": {"b": {}}}, "a.b.c"))
        self.assertIsNone(dig({"a": 3}, "a.b"))

    def test_a_field_the_vendor_did_not_return_stays_absent(self):
        found = extract({}, schema.PROPERTY_FIELDS)
        self.assertEqual(found, {}, "an unmapped field must not get a default")

    def test_a_malformed_number_is_treated_as_missing_not_forced(self):
        found = extract({"listPrice": "not a number"}, schema.PROPERTY_FIELDS)
        self.assertNotIn("asking_price", found)

    def test_currency_and_thousands_separators_are_parsed(self):
        found = extract({"listPrice": "$155,000", "buildingSqft": "1,480"},
                        schema.PROPERTY_FIELDS)
        self.assertEqual(found["asking_price"], 155000.0)
        self.assertEqual(found["sqft"], 1480)

    def test_booleans_accept_the_usual_spellings_and_reject_the_rest(self):
        self.assertIs(extract({"vacant": "yes"}, schema.PROPERTY_FIELDS)["vacant"], True)
        self.assertIs(extract({"vacant": "false"}, schema.PROPERTY_FIELDS)["vacant"], False)
        self.assertNotIn("vacant", extract({"vacant": "maybe"}, schema.PROPERTY_FIELDS))

    def test_first_list_finds_the_result_array(self):
        self.assertEqual(first_list({"results": [{"a": 1}]}, schema.RESULT_LIST_KEYS),
                         [{"a": 1}])
        self.assertEqual(first_list({"nothing": 1}, schema.RESULT_LIST_KEYS), [])
        self.assertEqual(first_list([{"a": 1}], schema.RESULT_LIST_KEYS), [{"a": 1}])


class ModelMappingTests(unittest.TestCase):

    def test_a_full_record_maps_onto_the_lead_model(self):
        lead = to_lead(SAMPLE_RECORD)
        self.assertEqual(lead.address, "18 Testcase Avenue")
        self.assertEqual(lead.city, "Springfield")
        self.assertEqual(lead.state, "MO")
        self.assertEqual(lead.zip_code, "65801")
        self.assertEqual(lead.county, "Greene")
        self.assertEqual(lead.property_id, "PR-TEST-001")
        self.assertEqual(lead.owner_name, "TEST OWNER")
        self.assertEqual(lead.asking_price, 155000.0)
        self.assertEqual(lead.estimated_value, 245000.0)
        self.assertEqual(lead.beds, 3.0)
        self.assertEqual(lead.sqft, 1480)
        self.assertEqual(lead.year_built, 1968)
        self.assertEqual(lead.property_type, PropertyType.SINGLE_FAMILY)
        self.assertEqual(lead.days_on_market, 91)
        self.assertIs(lead.vacant, True)
        self.assertIs(lead.tax_delinquent, True)
        self.assertIs(lead.pre_foreclosure, False)
        self.assertEqual(lead.source, "propertyreach")

    def test_a_sparse_record_leaves_everything_else_unknown(self):
        lead = to_lead({"address": "1 Sparse Street", "state": "TX"})
        self.assertEqual(lead.address, "1 Sparse Street")
        self.assertIsNone(lead.asking_price)
        self.assertIsNone(lead.estimated_value)
        self.assertIsNone(lead.beds)
        self.assertIsNone(lead.vacant, "unknown must stay None, never False")
        self.assertIsNone(lead.tax_delinquent)
        self.assertEqual(lead.property_type, PropertyType.UNKNOWN)
        self.assertEqual(lead.occupancy, Occupancy.UNKNOWN)

    def test_vacancy_wins_over_owner_occupancy(self):
        lead = to_lead({"address": "x", "vacant": True, "ownerOccupied": True})
        self.assertEqual(lead.occupancy, Occupancy.VACANT)

    def test_absentee_owner_reads_as_tenant_occupied(self):
        lead = to_lead({"address": "x", "absenteeOwner": True})
        self.assertEqual(lead.occupancy, Occupancy.TENANT_OCCUPIED)

    def test_equity_without_a_mortgage_balance_is_flagged_unverified(self):
        lead = to_lead({"address": "x", "estimatedEquity": 120000})
        self.assertTrue(
            any("mortgage balance" in note for note in lead.needs_verification)
        )

    def test_equity_with_a_mortgage_balance_is_not_flagged(self):
        lead = to_lead(SAMPLE_RECORD)
        self.assertFalse(
            any("mortgage balance" in note for note in lead.needs_verification)
        )

    def test_a_comp_maps(self):
        comp = to_comp(SAMPLE_COMP)
        self.assertIsNotNone(comp)
        self.assertEqual(comp.address, "22 Testcase Avenue")
        self.assertEqual(comp.sale_price, 239000.0)
        self.assertEqual(comp.sale_date, date(2025, 11, 2))
        self.assertEqual(comp.distance_miles, 0.3)

    def test_a_comp_without_a_price_is_not_a_comp(self):
        self.assertIsNone(to_comp({"address": {"line1": "x"}}))

    def test_a_comp_without_an_address_is_not_a_comp(self):
        self.assertIsNone(to_comp({"salePrice": 200000}))


class UnverifiedEndpointRefusalTests(unittest.TestCase):
    """The core safety property: an unconfirmed path is never requested."""

    def setUp(self):
        self.client = FakeClient(responses=[{"results": [SAMPLE_RECORD]}])
        self.provider = make_provider(self.client, allow_unverified=False)

    def test_search_is_refused_and_no_request_is_sent(self):
        response = self.provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(response.data, [])
        self.assertIn("not confirmed", response.reason)
        self.assertEqual(self.client.calls, [], "no request may be sent")

    def test_detail_and_comps_are_refused_too(self):
        lead = Lead(address="x", property_id="PR-1")
        self.assertIn("not confirmed", self.provider.get_property(lead).reason)
        self.assertIn("not confirmed", self.provider.get_comps(lead).reason)
        self.assertEqual(self.client.calls, [])

    def test_the_refusal_is_recorded_in_usage(self):
        self.provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertIn("Property Search", self.provider.usage.refused_unverified)
        self.assertEqual(self.provider.usage.used, 0, "a refusal is not a call")

    def test_health_check_reports_the_unverified_paths_without_calling(self):
        with mock.patch.dict(os.environ, {"PROPERTYREACH_API_KEY": "TEST-KEY"}):
            ok, message = self.provider.health_check()
        self.assertFalse(ok)
        self.assertIn("unverified", message)
        self.assertEqual(self.client.calls, [])

    def test_health_check_without_a_key_says_not_connected(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROPERTYREACH_API_KEY", None)
            ok, message = self.provider.health_check()
        self.assertFalse(ok)
        self.assertIn("NOT CONNECTED", message)


class SearchTests(unittest.TestCase):
    """With the override on, the finished transport and mapping are exercised."""

    def test_search_returns_mapped_leads(self):
        client = FakeClient(responses=[{"results": [SAMPLE_RECORD]}])
        provider = make_provider(client)
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0].address, "18 Testcase Avenue")
        self.assertEqual(provider.usage.search_calls, 1)
        self.assertEqual(client.calls[0]["method"], "POST")

    def test_the_search_body_carries_geography_price_and_signals(self):
        provider = make_provider()
        criteria = HuntCriteria(
            states=("FL", "TX", "MO"),
            min_price=MIN_PROPERTY_PRICE,
            max_price=MAX_PROPERTY_PRICE,
            required_signals=("vacant", "high_equity"),
        )
        body = provider.build_search_body(criteria)
        self.assertEqual(body["state"], ["FL", "TX", "MO"])
        self.assertEqual(body["minValue"], 0)
        self.assertEqual(body["maxValue"], 2_200_000)
        self.assertTrue(body["vacant"])
        self.assertTrue(body["highEquity"])

    def test_empty_results_are_reported_as_empty_not_as_a_failure(self):
        provider = make_provider(FakeClient(responses=[{"results": []}]))
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertTrue(response.supported)
        self.assertIn("no properties", response.reason)
        self.assertIn(response.data, ([], None))

    def test_a_record_with_no_address_is_skipped_not_invented(self):
        client = FakeClient(responses=[{"results": [{"propertyId": "X"}, SAMPLE_RECORD]}])
        provider = make_provider(client)
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(len(response.data), 1)
        self.assertTrue(any("no usable" in w for w in provider.warnings))

    def test_a_bare_json_array_is_accepted(self):
        provider = make_provider(FakeClient(responses=[[SAMPLE_RECORD]]))
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(len(response.data), 1)


class ErrorHandlingTests(unittest.TestCase):

    def _failing(self, status, message="boom"):
        return make_provider(FakeClient(error=HttpError(message, status=status)))

    def test_authentication_failure_is_explained_not_retried(self):
        provider = self._failing(401)
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertTrue(response.supported)
        self.assertIn("rejected the credential", response.reason)
        self.assertIn("PROPERTYREACH_API_KEY", response.reason)
        self.assertEqual(provider.usage.errors, 1)

    def test_forbidden_is_also_an_auth_failure(self):
        response = self._failing(403).search_properties(HuntCriteria(states=("MO",)))
        self.assertIn("rejected the credential", response.reason)

    def test_rate_limit_says_the_key_works_and_the_quota_does_not(self):
        response = self._failing(429).search_properties(HuntCriteria(states=("MO",)))
        self.assertIn("rate limit", response.reason.lower())

    def test_a_timeout_fails_the_call_without_failing_the_run(self):
        provider = self._failing(None, "timed out after 3 attempt(s)")
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertTrue(response.supported)
        self.assertIn("timed out", response.reason)
        self.assertEqual(response.data, [])

    def test_a_malformed_response_is_rejected_not_parsed(self):
        provider = make_provider(FakeClient(responses=["this is not JSON object"]))
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertIn("not JSON", response.reason)
        self.assertEqual(response.data, [])

    def test_an_unparseable_record_does_not_lose_the_good_ones(self):
        client = FakeClient(responses=[{"results": [{"address": {"line1": None}},
                                                    SAMPLE_RECORD]}])
        provider = make_provider(client)
        response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(len(response.data), 1)

    def test_a_nested_object_never_becomes_a_stringified_address(self):
        """Regression: str({'line1': None}) is not an address."""
        lead = to_lead({"address": {"line1": None}, "state": "MO"})
        self.assertEqual(lead.address, "")


class BudgetTests(unittest.TestCase):

    def test_max_reach_stops_the_run_and_says_so(self):
        client = FakeClient(responses=[{"results": [SAMPLE_RECORD]}, {"results": []}])
        provider = make_provider(client, max_reach=1)
        provider.search_properties(HuntCriteria(states=("MO",)))
        second = provider.search_properties(HuntCriteria(states=("TX",)))
        self.assertIn("MAX_REACH", second.reason)
        self.assertTrue(provider.usage.stopped_by_budget)
        self.assertEqual(len(client.calls), 1, "the second call must not be sent")

    def test_usage_counts_and_remaining(self):
        usage = ReachUsage(limit=10, search_calls=1, detail_calls=2, comp_calls=3)
        self.assertEqual(usage.used, 6)
        self.assertEqual(usage.remaining, 4)
        self.assertFalse(usage.exhausted)
        self.assertIn("Calls remaining", usage.render())

    def test_defaults_are_the_configured_limits(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("MAX_REACH", "MAX_COMPS", "MAX_SKIP_TRACES"):
                os.environ.pop(name, None)
            budget = ApiBudget.from_env()
        self.assertEqual(budget.max_reach, 100)
        self.assertEqual(budget.max_comps, 25)
        self.assertEqual(budget.max_skip_traces, 10)

    def test_every_limit_stays_configurable(self):
        with mock.patch.dict(os.environ, {"MAX_REACH": "7", "MAX_COMPS": "3",
                                          "MAX_SKIP_TRACES": "1"}):
            budget = ApiBudget.from_env()
        self.assertEqual((budget.max_reach, budget.max_comps, budget.max_skip_traces),
                         (7, 3, 1))

    def test_the_run_report_shows_used_remaining_and_whether_a_cap_stopped_it(self):
        report = UsageReport()
        provider = make_provider(FakeClient(responses=[{"results": [SAMPLE_RECORD]}]))
        provider.search_properties(HuntCriteria(states=("MO",)))
        report.adopt_provider_usage(provider)
        text = report.render(ApiBudget())
        self.assertEqual(report.provider_calls_used, 1)
        self.assertEqual(report.provider_calls_remaining, 99)
        self.assertIn("calls used", text)
        self.assertIn("calls remaining", text)
        self.assertIn("BUDGET STOPPED RUN", text)
        self.assertIn("skip traces", text)


class DetailSliceTests(unittest.TestCase):
    """Owner, equity, distress, foreclosure and tax all come from one call."""

    def test_one_detail_call_serves_every_slice(self):
        provider = make_provider(FakeClient(responses=[{"results": [SAMPLE_RECORD]}]))
        lead = Lead(address="x", property_id="PR-TEST-001")
        owner = provider.get_owner(lead)
        self.assertEqual(owner.data["owner_name"], "TEST OWNER")
        self.assertEqual(provider.usage.detail_calls, 1)

    def test_equity_slice_carries_the_mortgage_balance(self):
        provider = make_provider(FakeClient(responses=[{"results": [SAMPLE_RECORD]}]))
        equity = provider.get_equity(Lead(address="x", property_id="PR-1"))
        self.assertEqual(equity.data["mortgage_balance"], "42000.0")

    def test_valuation_is_labelled_an_unverified_arv(self):
        provider = make_provider(FakeClient(responses=[{"results": [SAMPLE_RECORD]}]))
        response = provider.get_valuation(Lead(address="x", property_id="PR-1"))
        self.assertIn("unverified ARV", response.reason)

    def test_a_slice_the_response_did_not_carry_is_empty_not_invented(self):
        provider = make_provider(FakeClient(responses=[{"results": [
            {"address": {"line1": "x"}}
        ]}]))
        response = provider.get_foreclosure_data(Lead(address="x", property_id="PR-1"))
        self.assertIsNone(response.data)
        self.assertIn("no foreclosure", response.reason)

    def test_comps_are_returned_as_comps(self):
        provider = make_provider(FakeClient(responses=[{"comparables": [SAMPLE_COMP]}]))
        response = provider.get_comps(Lead(address="x", property_id="PR-1"))
        self.assertEqual(len(response.data), 1)
        self.assertEqual(provider.usage.comp_calls, 1)


class CredentialTests(unittest.TestCase):

    def test_no_key_means_not_connected_at_construction(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROPERTYREACH_API_KEY", None)
            with self.assertRaises(ProviderNotConfigured) as ctx:
                PropertyReachProvider(settings=ProviderSettings())
        self.assertIn("PROPERTYREACH_API_KEY", str(ctx.exception))

    def test_a_key_builds_an_https_client_with_the_x_api_key_header(self):
        with mock.patch.dict(os.environ, {"PROPERTYREACH_API_KEY": "TEST-KEY-123"}):
            provider = PropertyReachProvider(settings=ProviderSettings())
        self.assertEqual(provider.client.base_url, "https://api.propertyreach.com/v1")
        self.assertEqual(provider.client.auth_header, "x-api-key")
        headers = provider.client._headers()
        self.assertEqual(headers["x-api-key"], "TEST-KEY-123")

    def test_the_key_never_appears_in_a_repr(self):
        with mock.patch.dict(os.environ, {"PROPERTYREACH_API_KEY": "TEST-KEY-123"}):
            provider = PropertyReachProvider(settings=ProviderSettings())
        self.assertNotIn("TEST-KEY-123", repr(provider.client))

    def test_a_plain_http_base_url_is_refused(self):
        with mock.patch.dict(os.environ, {
            "PROPERTYREACH_API_KEY": "TEST-KEY",
            "PROPERTYREACH_BASE_URL": "http://api.propertyreach.com/v1",
        }):
            with self.assertRaises(ValueError):
                PropertyReachProvider(settings=ProviderSettings())

    def test_settings_expose_the_propertyreach_variables(self):
        with mock.patch.dict(os.environ, {"PROPERTYREACH_API_KEY": "TEST-KEY"}):
            settings = ProviderSettings.from_env(load_file=False)
        self.assertTrue(settings.has_propertyreach)
        self.assertIn("propertyreach", settings.describe())


class TransportTests(unittest.TestCase):
    """The real SafeHttpClient, with urlopen monkeypatched. No socket is opened."""

    def _client(self):
        return SafeHttpClient(
            schema.DEFAULT_BASE_URL,
            "TEST-KEY",
            HttpConfig(min_interval_seconds=0.0, max_retries=2, backoff_base_seconds=0.0),
            auth_header=schema.AUTH_HEADER,
            auth_scheme=schema.AUTH_SCHEME,
        )

    def test_a_401_is_not_retried(self):
        error = urllib.error.HTTPError(
            schema.DEFAULT_BASE_URL, 401, "Unauthorized", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error) as opener:
            provider = make_provider(self._client())
            response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(opener.call_count, 1, "a rejected key must not be retried")
        self.assertIn("rejected the credential", response.reason)

    def test_the_key_never_reaches_the_error_message(self):
        error = urllib.error.HTTPError(
            schema.DEFAULT_BASE_URL + "?api_key=TEST-KEY", 500, "boom", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            provider = make_provider(self._client())
            response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertNotIn("TEST-KEY", response.reason)

    def test_a_successful_response_is_decoded(self):
        payload = json.dumps({"results": [SAMPLE_RECORD]}).encode("utf-8")

        class FakeResponse:
            status = 200

            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            provider = make_provider(self._client())
            response = provider.search_properties(HuntCriteria(states=("MO",)))
        self.assertEqual(len(response.data), 1)


class RegistryTests(unittest.TestCase):

    def test_source_propertyreach_resolves(self):
        self.assertIn("propertyreach", registry.registered_names())

    def test_it_declares_every_capability_propertyreach_documents(self):
        for capability in Capability:
            self.assertTrue(
                registry.supports("propertyreach", capability),
                f"{capability} should be declared",
            )

    def test_it_is_not_connected_without_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROPERTYREACH_API_KEY", None)
            entry = registry.registration("propertyreach")
            self.assertEqual(
                entry.missing_settings(ProviderSettings()), ["PROPERTYREACH_API_KEY"]
            )
            with self.assertRaises(ProviderNotConfigured):
                registry.get_provider("propertyreach", ProviderSettings())

    def test_it_is_not_a_local_or_test_provider(self):
        entry = registry.registration("propertyreach")
        self.assertFalse(entry.is_local)
        self.assertFalse(entry.is_test_provider)

    def test_the_listing_names_the_variable_that_is_missing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROPERTYREACH_API_KEY", None)
            text = registry.describe_sources(ProviderSettings())
        self.assertIn("propertyreach", text)
        self.assertIn("PROPERTYREACH_API_KEY", text)

    def test_the_factory_reads_max_reach_from_the_environment(self):
        with mock.patch.dict(os.environ, {"PROPERTYREACH_API_KEY": "TEST-KEY",
                                          "MAX_REACH": "12"}):
            provider = registry.get_provider("propertyreach", ProviderSettings())
        self.assertEqual(provider.usage.limit, 12)


class PriceRangeTests(unittest.TestCase):
    """The buyer-capacity ceiling: no low cap, and $2.2M is inside the range."""

    def test_the_defaults_are_zero_to_two_point_two_million(self):
        self.assertEqual(MIN_PROPERTY_PRICE, 0.0)
        self.assertEqual(MAX_PROPERTY_PRICE, 2_200_000.0)

    def test_a_two_point_one_five_million_property_is_searchable(self):
        criteria = HuntCriteria(
            states=("FL", "TX", "MO"),
            min_price=MIN_PROPERTY_PRICE,
            max_price=MAX_PROPERTY_PRICE,
        )
        self.assertTrue(criteria.matches_price(2_150_000))
        self.assertTrue(criteria.matches_price(1_400_000))
        self.assertTrue(criteria.matches_price(45_000))
        self.assertFalse(criteria.matches_price(2_300_000))

    def test_an_unknown_price_never_rejects(self):
        criteria = HuntCriteria(min_price=MIN_PROPERTY_PRICE, max_price=MAX_PROPERTY_PRICE)
        self.assertTrue(criteria.matches_price(None))

    def test_the_cli_defaults_to_the_configured_range(self):
        from wholesale_engine.main import build_parser, criteria_from_args
        from wholesale_engine.config import DEFAULT_LEAD_CONFIG

        args = build_parser().parse_args(["--hunt", "--states", "FL,TX,MO"])
        criteria = criteria_from_args(args, DEFAULT_LEAD_CONFIG)
        self.assertEqual(criteria.min_price, 0.0)
        self.assertEqual(criteria.max_price, 2_200_000.0)
        self.assertEqual(criteria.states, ("FL", "TX", "MO"))

    def test_the_cli_overrides_win(self):
        from wholesale_engine.main import build_parser, criteria_from_args
        from wholesale_engine.config import DEFAULT_LEAD_CONFIG

        args = build_parser().parse_args(
            ["--hunt", "--min-price", "250000", "--max-price", "1800000"]
        )
        criteria = criteria_from_args(args, DEFAULT_LEAD_CONFIG)
        self.assertEqual(criteria.min_price, 250_000)
        self.assertEqual(criteria.max_price, 1_800_000)

    def test_no_module_imposes_a_low_global_price_ceiling(self):
        """Regression: nothing may quietly cap the search at a starter-home price."""
        from wholesale_engine.config import DEFAULT_LEAD_CONFIG

        self.assertIsNone(DEFAULT_LEAD_CONFIG.max_asking_price)
        self.assertIsNone(HuntCriteria().max_price)
        self.assertGreaterEqual(MAX_PROPERTY_PRICE, 2_200_000.0)


class NoLiveCallTests(unittest.TestCase):
    """TEST mode must not reach PropertyReach, whatever it is asked for."""

    def test_the_default_provider_makes_no_request_at_all(self):
        with mock.patch("urllib.request.urlopen") as opener:
            provider = make_provider(FakeClient(), allow_unverified=False)
            criteria = HuntCriteria(
                states=("FL", "TX", "MO"),
                min_price=MIN_PROPERTY_PRICE,
                max_price=MAX_PROPERTY_PRICE,
            )
            provider.search_properties(criteria)
            lead = Lead(address="x", property_id="PR-1")
            provider.get_property(lead)
            provider.get_owner(lead)
            provider.get_equity(lead)
            provider.get_distress_data(lead)
            provider.get_foreclosure_data(lead)
            provider.get_tax_data(lead)
            provider.get_valuation(lead)
            provider.get_comps(lead)
            provider.health_check()
        opener.assert_not_called()
        self.assertEqual(provider.usage.used, 0)


if __name__ == "__main__":
    unittest.main()
