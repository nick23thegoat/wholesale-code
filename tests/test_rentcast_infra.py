"""Quota ledger and response cache — the two things standing between a
50-request-per-month plan and an accidental overage bill.

Nothing here touches a network.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from wholesale_engine.providers.cache import (
    CREDENTIAL_PARAMS,
    ResponseCache,
    TTL_PROPERTY_RECORDS,
)
from wholesale_engine.providers.quota import (
    QuotaExceeded,
    QuotaLedger,
    current_period,
)
from wholesale_engine.settings import ProviderSettings


class QuotaLedgerTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "usage.json"

    def ledger(self, limit=50):
        return QuotaLedger.load("rentcast", self.path, limit=limit)

    def test_a_fresh_ledger_has_the_whole_plan_available(self):
        ledger = self.ledger()
        self.assertEqual(ledger.used, 0)
        self.assertEqual(ledger.remaining, 50)
        self.assertFalse(ledger.exhausted)

    def test_spending_persists_across_processes(self):
        self.ledger().record(3)
        self.assertEqual(self.ledger().used, 3)
        self.assertEqual(self.ledger().remaining, 47)

    def test_a_cache_hit_is_never_billed(self):
        ledger = self.ledger()
        ledger.record_cache_hit(25)
        self.assertEqual(ledger.used, 0, "a cache hit must not spend quota")
        self.assertEqual(ledger.remaining, 50)
        self.assertIn("free — not billed", ledger.render())

    def test_the_cap_is_enforced_before_the_request_not_after(self):
        ledger = self.ledger()
        ledger.record(49)
        self.assertTrue(ledger.can_spend(1))
        self.assertFalse(ledger.can_spend(2))
        ledger.require(1)                      # affordable: no raise
        with self.assertRaises(QuotaExceeded):
            ledger.require(2)

    def test_the_refusal_says_what_to_do_about_it(self):
        ledger = self.ledger()
        ledger.record(50)
        with self.assertRaises(QuotaExceeded) as ctx:
            ledger.require(1)
        message = str(ctx.exception)
        self.assertIn("50/50", message)
        self.assertIn("MAX_RENTCAST", message)
        self.assertIn("app.rentcast.io", message)

    def test_a_new_month_starts_fresh_without_anyone_resetting_it(self):
        august = datetime(2026, 8, 20, tzinfo=timezone.utc)
        september = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with mock.patch(
            "wholesale_engine.providers.quota.datetime"
        ) as clock:
            clock.now.return_value = august
            ledger = self.ledger()
            ledger.record(50)
            self.assertTrue(ledger.exhausted)
            clock.now.return_value = september
            self.assertEqual(ledger.used, 0)
            self.assertEqual(ledger.remaining, 50)

    def test_previous_months_are_kept_not_overwritten(self):
        raw = {"2026-07": {"rentcast": 50}}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        ledger = self.ledger()
        ledger.record(2)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["2026-07"]["rentcast"], 50)
        self.assertEqual(stored[current_period()]["rentcast"], 2)

    def test_a_corrupt_ledger_fails_safe_as_spent_not_as_unlimited(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        ledger = self.ledger()
        self.assertTrue(
            ledger.exhausted,
            "an unreadable ledger must not read as 'nothing spent'",
        )

    def test_the_limit_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"MAX_RENTCAST": "500"}):
            self.assertEqual(QuotaLedger.load("rentcast", self.path).limit, 500)

    def test_a_nonsense_limit_falls_back_to_the_plan_default(self):
        for value in ("abc", "-5", ""):
            with mock.patch.dict(os.environ, {"MAX_RENTCAST": value}):
                self.assertEqual(QuotaLedger.load("rentcast", self.path).limit, 50)

    def test_recording_zero_or_negative_does_nothing(self):
        ledger = self.ledger()
        ledger.record(0)
        ledger.record(-3)
        self.assertEqual(ledger.used, 0)


class ResponseCacheTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cache = ResponseCache(directory=self.dir, provider="rentcast")
        self.params = {"zipCode": "33607", "limit": 500}
        self.payload = {"data": [{"formattedAddress": "1 TEST St"}]}

    def test_a_miss_then_a_hit(self):
        self.assertIsNone(self.cache.get("properties", self.params))
        self.cache.put("properties", self.params, self.payload)
        self.assertEqual(self.cache.get("properties", self.params), self.payload)
        self.assertEqual(self.cache.stats.hits, 1)

    def test_a_hit_is_a_billable_request_avoided(self):
        self.cache.put("properties", self.params, self.payload)
        self.cache.get("properties", self.params)
        self.cache.get("properties", self.params)
        self.assertEqual(self.cache.stats.requests_saved, 2)

    def test_different_parameters_are_different_entries(self):
        self.cache.put("properties", {"zipCode": "33607"}, self.payload)
        self.assertIsNone(self.cache.get("properties", {"zipCode": "33609"}))

    def test_parameter_order_does_not_change_the_key(self):
        a = self.cache.key("properties", {"zipCode": "33607", "limit": 500})
        b = self.cache.key("properties", {"limit": 500, "zipCode": "33607"})
        self.assertEqual(a, b)

    def test_the_api_key_is_excluded_from_the_cache_key(self):
        """Rotating your key must not invalidate the whole cache."""
        without = self.cache.key("properties", {"zipCode": "33607"})
        for name in CREDENTIAL_PARAMS:
            with_secret = self.cache.key(
                "properties", {"zipCode": "33607", name: "SECRET-VALUE"}
            )
            self.assertEqual(without, with_secret, f"{name} leaked into the key")

    def test_a_credential_never_reaches_a_cache_file(self):
        self.cache.put(
            "properties",
            {"zipCode": "33607", "api_key": "SECRET-VALUE"},
            self.payload,
        )
        for path in self.dir.glob("*.json"):
            self.assertNotIn("SECRET-VALUE", path.read_text(encoding="utf-8"))

    def test_an_expired_entry_is_a_miss(self):
        self.cache.put("properties", self.params, self.payload)
        self.assertIsNone(self.cache.get("properties", self.params, ttl_seconds=0))
        self.assertEqual(self.cache.stats.expired, 1)

    def test_a_fresh_entry_inside_its_ttl_is_a_hit(self):
        self.cache.put("properties", self.params, self.payload)
        self.assertIsNotNone(
            self.cache.get("properties", self.params, ttl_seconds=TTL_PROPERTY_RECORDS)
        )

    def test_a_corrupt_entry_is_a_miss_not_an_exception(self):
        self.cache.put("properties", self.params, self.payload)
        for path in self.dir.glob("*.json"):
            path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.cache.get("properties", self.params))

    def test_an_entry_missing_its_timestamp_is_a_miss(self):
        self.cache.put("properties", self.params, self.payload)
        for path in self.dir.glob("*.json"):
            path.write_text(json.dumps({"response": {"a": 1}}), encoding="utf-8")
        self.assertIsNone(self.cache.get("properties", self.params))

    def test_disabling_the_cache_makes_every_lookup_miss(self):
        self.cache.put("properties", self.params, self.payload)
        disabled = ResponseCache(directory=self.dir, provider="rentcast", enabled=False)
        self.assertIsNone(disabled.get("properties", self.params))
        disabled.put("properties", {"zipCode": "99999"}, self.payload)
        self.assertEqual(disabled.stats.writes, 0)

    def test_a_none_response_is_never_stored(self):
        self.cache.put("properties", self.params, None)
        self.assertEqual(self.cache.entries(), 0)

    def test_clear_removes_only_this_providers_entries(self):
        self.cache.put("properties", self.params, self.payload)
        other = ResponseCache(directory=self.dir, provider="somethingelse")
        other.put("properties", self.params, self.payload)
        self.assertEqual(self.cache.clear(), 1)
        self.assertEqual(other.entries(), 1)

    def test_providers_do_not_collide_on_the_same_endpoint(self):
        other = ResponseCache(directory=self.dir, provider="propertyreach")
        self.cache.put("properties", self.params, {"who": "rentcast"})
        other.put("properties", self.params, {"who": "propertyreach"})
        self.assertEqual(self.cache.get("properties", self.params)["who"], "rentcast")
        self.assertEqual(other.get("properties", self.params)["who"], "propertyreach")


class SettingsTests(unittest.TestCase):

    def test_rentcast_needs_only_a_key(self):
        with mock.patch.dict(os.environ, {"RENTCAST_API_KEY": "TEST-KEY"}):
            settings = ProviderSettings.from_env(load_file=False)
        self.assertTrue(settings.has_rentcast)
        self.assertIn("rentcast", settings.describe())

    def test_no_key_means_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENTCAST_API_KEY", None)
            settings = ProviderSettings.from_env(load_file=False)
        self.assertFalse(settings.has_rentcast)

    def test_the_key_is_never_in_the_summary_line(self):
        with mock.patch.dict(os.environ, {"RENTCAST_API_KEY": "TEST-KEY-123"}):
            settings = ProviderSettings.from_env(load_file=False)
        self.assertNotIn("TEST-KEY-123", settings.describe())


if __name__ == "__main__":
    unittest.main()
