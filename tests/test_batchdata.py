"""BatchData skip-trace adapter tests. **No live requests — the transport is a fake.**

Skip tracing is the most sensitive thing this engine does and the only thing
it does that is billed per person looked up. These tests pin down the two
properties that matter most: a response body never reaches a log line, and a
contact is never invented to fill a gap.
"""

from __future__ import annotations

import io
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout

from wholesale_engine.acquisitions.batchdata import (
    BatchDataSkipTraceProvider,
    _normalize_contacts,
)
from wholesale_engine.acquisitions.batchdata_schema import (
    EMAIL_VALUE_KEYS,
    MAX_BULK_PROPERTIES,
    PHONE_TYPE_KEYS,
    PHONE_VALUE_KEYS,
)
from wholesale_engine.acquisitions.skip_trace import (
    SKIP_TRACE_PROVIDERS,
    SkipTraceNotConfigured,
    get_skip_trace_provider,
)
from wholesale_engine.providers.http_client import HttpError
from wholesale_engine.research.facts import Confidence

#: A response shaped the way BatchData's documentation describes one. The
#: numbers are in the reserved 555-01xx fiction range and the address is on
#: the reserved .invalid TLD, so this fixture can never reach a real person.
SAMPLE_RESPONSE = {
    "results": [
        {
            "phoneNumbers": [
                {"number": "5555550100", "type": "Mobile", "confidenceScore": "HIGH"},
                {"number": "5555550101", "type": "Landline", "confidenceScore": "LOW"},
            ],
            "emailAddresses": [{"address": "sample.owner@example.invalid"}],
            "mailingAddress": {
                "street": "PO Box 1", "city": "Austin", "state": "TX", "zip": "78701",
            },
        }
    ]
}


class StubClient:
    """Stands in for SafeHttpClient. Records calls; never touches a network."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, path, params=None, method="GET", body=None):
        self.calls.append((path, method, body))
        if self.error is not None:
            raise self.error
        return self.response


def make_provider(response=None, error=None):
    return BatchDataSkipTraceProvider(client=StubClient(response, error))


class Transport(unittest.TestCase):
    def test_it_goes_through_the_safe_http_client(self):
        # Not a hand-rolled urllib loop: the redaction, the bounded backoff,
        # the honoured Retry-After and the never-retry-a-rejected-credential
        # rule all live in SafeHttpClient and must not be re-implemented.
        import inspect

        from wholesale_engine.acquisitions import batchdata

        source = inspect.getsource(batchdata)
        self.assertIn("SafeHttpClient", source)
        self.assertNotIn("urllib.request.urlopen", source)

    def test_the_base_url_is_https(self):
        from wholesale_engine.acquisitions.batchdata_schema import BASE_URL

        self.assertTrue(BASE_URL.startswith("https://"))

    def test_it_posts_to_the_documented_path(self):
        provider = make_provider(SAMPLE_RESPONSE)
        provider.skip_trace("p1", address="1 Main St", city="Austin", state="TX", zip_code="78701")
        path, method, body = provider.client.calls[0]
        self.assertEqual(path, "property/skip-trace")
        self.assertEqual(method, "POST")
        self.assertEqual(body["requests"][0]["address"], "1 Main St")


class NeverLogsPii(unittest.TestCase):
    """A skip-trace payload must never reach stdout, stderr or a log record."""

    def _run_capturing(self, call):
        out, err = io.StringIO(), io.StringIO()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                call()
        finally:
            root.removeHandler(handler)
        return out.getvalue() + err.getvalue() + "\n".join(records)

    def test_a_successful_lookup_prints_nothing(self):
        provider = make_provider(SAMPLE_RESPONSE)
        text = self._run_capturing(
            lambda: provider.skip_trace("p1", address="1 Main St", city="Austin", state="TX")
        )
        self.assertEqual(text.strip(), "")
        for secret in ("5555550100", "sample.owner@example.invalid", "PO Box 1"):
            self.assertNotIn(secret, text)

    def test_a_failed_lookup_prints_nothing(self):
        provider = make_provider(error=HttpError("upstream said no", status=500))
        text = self._run_capturing(
            lambda: provider.skip_trace("p1", address="1 Main St")
        )
        self.assertEqual(text.strip(), "")

    def test_the_source_contains_no_debug_print(self):
        import inspect

        from wholesale_engine.acquisitions import batchdata

        for line in inspect.getsource(batchdata).splitlines():
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("print(") or "file=sys.stderr" in stripped,
                f"debug print left in batchdata.py: {stripped}",
            )


class Parsing(unittest.TestCase):
    def test_a_phone_under_a_vendor_key_still_reaches_the_contact(self):
        # BatchData nests the number under "number"; SkipTraceResult reads
        # "number" too, but the value keys differ across vendor versions and
        # a mismatch silently turns a found number into a blank contact.
        provider = make_provider(SAMPLE_RESPONSE)
        result = provider.skip_trace("p1", address="1 Main St", owner_name="Jane Sample")
        self.assertEqual(len(result.phones), 2)
        self.assertEqual(result.best_phone()["number"], "5555550100")
        self.assertEqual(result.to_contact("p1").phone, "5555550100")

    def test_the_highest_confidence_phone_wins(self):
        provider = make_provider(SAMPLE_RESPONSE)
        result = provider.skip_trace("p1", address="1 Main St")
        self.assertIs(
            Confidence.parse(result.best_phone()["confidence"]), Confidence.HIGH
        )

    def test_a_mailing_address_object_is_flattened(self):
        provider = make_provider(SAMPLE_RESPONSE)
        result = provider.skip_trace("p1", address="1 Main St")
        self.assertEqual(result.mailing_address, "PO Box 1, Austin, TX, 78701")

    def test_a_live_result_is_never_stamped_as_test_data(self):
        provider = make_provider(SAMPLE_RESPONSE)
        self.assertFalse(provider.skip_trace("p1", address="1 Main St").is_test_data)

    def test_an_unmatched_shape_finds_nothing_and_says_so(self):
        provider = make_provider({"results": [{"somethingElse": True}]})
        result = provider.skip_trace("p1", address="1 Main St")
        self.assertFalse(result.found_anything)
        self.assertIn("candidate keys", result.notes)
        self.assertIsNone(result.to_contact("p1").phone)

    def test_an_entry_with_no_value_is_dropped_not_kept_empty(self):
        self.assertEqual(
            _normalize_contacts(
                [{"type": "Mobile"}, {"number": "5555550100"}],
                PHONE_VALUE_KEYS, "number", PHONE_TYPE_KEYS,
            ),
            [{"number": "5555550100"}],
        )

    def test_bare_strings_are_accepted(self):
        self.assertEqual(
            _normalize_contacts(["owner@example.invalid"], EMAIL_VALUE_KEYS, "address"),
            [{"address": "owner@example.invalid"}],
        )


class Refusals(unittest.TestCase):
    def test_no_address_means_no_request_and_no_guess(self):
        provider = make_provider(SAMPLE_RESPONSE)
        result = provider.skip_trace("p1", owner_name="Jane Sample")
        self.assertEqual(provider.client.calls, [])
        self.assertFalse(result.found_anything)
        self.assertIn("refusing to guess", result.notes)

    def test_a_rejected_credential_names_the_variable_and_finds_nothing(self):
        provider = make_provider(error=HttpError("nope", status=401))
        result = provider.skip_trace("p1", address="1 Main St")
        self.assertFalse(result.found_anything)
        self.assertIn("BATCHDATA_API_KEY", result.notes)

    def test_a_rate_limit_is_reported_as_quota_not_as_no_contact(self):
        provider = make_provider(error=HttpError("slow down", status=429))
        result = provider.skip_trace("p1", address="1 Main St")
        self.assertIn("429", result.notes)

    def test_a_failed_lookup_is_not_counted_as_one(self):
        provider = make_provider(error=HttpError("boom", status=500))
        provider.skip_trace("p1", address="1 Main St")
        self.assertEqual(provider.lookups, 0)

    def test_pricing_is_not_invented(self):
        provider = make_provider(SAMPLE_RESPONSE)
        self.assertEqual(provider.cost_per_lookup, 0.0)
        self.assertIn("unknown", provider.describe())


class Bulk(unittest.TestCase):
    def test_one_request_covers_the_whole_batch(self):
        provider = make_provider({"results": [SAMPLE_RESPONSE["results"][0], {}]})
        results = provider.skip_trace_bulk([
            {"property_id": "a", "address": "1 Main St", "city": "Austin", "state": "TX"},
            {"property_id": "b", "address": "2 Main St", "city": "Austin", "state": "TX"},
        ])
        self.assertEqual(len(provider.client.calls), 1)
        self.assertEqual([r.property_id for r in results], ["a", "b"])
        self.assertTrue(results[0].found_anything)
        self.assertFalse(results[1].found_anything)

    def test_a_short_response_leaves_the_rest_empty_not_mismatched(self):
        # Matching by position means a shortfall must produce empty results,
        # never someone else's phone number attached to the wrong property.
        provider = make_provider({"results": [SAMPLE_RESPONSE["results"][0]]})
        results = provider.skip_trace_bulk([
            {"property_id": "a", "address": "1 Main St"},
            {"property_id": "b", "address": "2 Main St"},
        ])
        self.assertTrue(results[0].found_anything)
        self.assertFalse(results[1].found_anything)
        self.assertTrue(any("matched by position" in w for w in provider.warnings))

    def test_the_documented_batch_limit_is_enforced_before_the_request(self):
        provider = make_provider(SAMPLE_RESPONSE)
        oversize = [{"property_id": str(i), "address": f"{i} Main St"}
                    for i in range(MAX_BULK_PROPERTIES + 1)]
        with self.assertRaises(ValueError):
            provider.skip_trace_bulk(oversize)
        self.assertEqual(provider.client.calls, [])

    def test_an_empty_batch_costs_nothing(self):
        provider = make_provider(SAMPLE_RESPONSE)
        self.assertEqual(provider.skip_trace_bulk([]), [])
        self.assertEqual(provider.client.calls, [])


class Registration(unittest.TestCase):
    def test_batchdata_is_registered_but_refuses_without_a_key(self):
        import os

        self.assertIn("batchdata", SKIP_TRACE_PROVIDERS)
        saved = os.environ.pop("BATCHDATA_API_KEY", None)
        try:
            with self.assertRaises(SkipTraceNotConfigured):
                get_skip_trace_provider("batchdata")
        finally:
            if saved is not None:
                os.environ["BATCHDATA_API_KEY"] = saved

    def test_the_default_is_still_the_one_that_refuses(self):
        self.assertIsInstance(
            get_skip_trace_provider(), type(get_skip_trace_provider("none"))
        )


if __name__ == "__main__":
    unittest.main()
