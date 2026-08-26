"""RentCast adapter tests. **No live requests — the transport is a fake.**

Every test here injects a stub client, so the suite never touches
api.rentcast.io and never spends a request from a fifty-request plan.

What is actually being pinned down:

* the funnel cannot spend the month on detail lookups
* a cache hit is free and a failure is not billed
* owner PII never reaches a log line
* an unmapped property type is dropped rather than sent
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.models.enums import Occupancy, PropertyType
from wholesale_engine.providers.base import Capability
from wholesale_engine.providers.cache import ResponseCache
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.providers.http_client import HttpError
from wholesale_engine.providers.quota import QuotaLedger
from wholesale_engine.providers.rentcast import (
    RentCastProvider,
    to_comp,
    to_lead,
    to_property_type,
)
from wholesale_engine.providers.rentcast_schema import to_rentcast_types

#: One record shaped the way RentCast's docs describe /properties.
SAMPLE_RECORD = {
    "id": "5500-Grand-Lake-Dr,-San-Antonio,-TX-78244",
    "formattedAddress": "5500 Grand Lake Dr, San Antonio, TX 78244",
    "addressLine1": "5500 Grand Lake Dr",
    "city": "San Antonio",
    "state": "TX",
    "zipCode": "78244",
    "county": "Bexar",
    "propertyType": "Single Family",
    "bedrooms": 3,
    "bathrooms": 2,
    "squareFootage": 1878,
    "yearBuilt": 1973,
    "lastSalePrice": 260000,
    "lastSaleDate": "2021-06-15",
    "ownerOccupied": False,
    "owner": {
        "names": ["Jane Q Sample"],
        "type": "Individual",
        "mailingAddress": {
            "addressLine1": "PO Box 1",
            "city": "Austin",
            "state": "TX",
            "zipCode": "78701",
        },
    },
    "taxAssessments": {
        "2022": {"year": 2022, "value": 200000, "land": 50000, "improvements": 150000},
        "2023": {"year": 2023, "value": 225000, "land": 55000, "improvements": 170000},
    },
    "propertyTaxes": {
        "2022": {"year": 2022, "total": 4200},
        "2023": {"year": 2023, "total": 4633},
    },
}


class StubClient:
    """Stands in for SafeHttpClient. Records calls; never touches a network."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, path, params=None, method="GET", body=None):
        self.calls.append((path, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.response


def make_provider(tmp: Path, response=None, error=None, limit=50, used=0, **kwargs):
    ledger = QuotaLedger(provider="rentcast", limit=limit, path=tmp / "usage.json")
    if used:
        ledger.record(used)
    cache = ResponseCache(directory=tmp / "cache", provider="rentcast")
    provider = RentCastProvider(
        client=StubClient(response, error), ledger=ledger, cache=cache, **kwargs
    )
    return provider


class SchemaMapping(unittest.TestCase):
    def test_unmapped_property_types_are_dropped_not_sent(self):
        # duplex/triplex/fourplex have no RentCast equivalent. Sending one
        # would silently match nothing and cost a request from a plan of 50.
        self.assertEqual(
            to_rentcast_types(("single_family", "duplex", "triplex")), "Single Family"
        )

    def test_nothing_mappable_means_no_filter_at_all(self):
        self.assertIsNone(to_rentcast_types(("houseboat", "yurt")))
        self.assertIsNone(to_rentcast_types(()))

    def test_property_type_round_trips_case_and_separator_insensitively(self):
        self.assertIs(to_property_type("Single Family"), PropertyType.SINGLE_FAMILY)
        self.assertIs(to_property_type("single-family"), PropertyType.SINGLE_FAMILY)
        self.assertIs(to_property_type(""), PropertyType.UNKNOWN)


class RecordMapping(unittest.TestCase):
    def test_a_record_becomes_a_lead_with_owner_and_tax(self):
        lead = to_lead(SAMPLE_RECORD)
        self.assertEqual(lead.address, "5500 Grand Lake Dr, San Antonio, TX 78244")
        self.assertEqual(lead.city, "San Antonio")
        self.assertEqual(lead.owner_name, "Jane Q Sample")
        self.assertEqual(lead.beds, 3)
        self.assertEqual(lead.sqft, 1878)
        self.assertIs(lead.property_type, PropertyType.SINGLE_FAMILY)

    def test_the_newest_tax_year_wins_not_an_arbitrary_key(self):
        lead = to_lead(SAMPLE_RECORD)
        self.assertEqual(lead.raw["assessed_value"], "225000.0")
        self.assertEqual(lead.raw["assessment_year"], "2023")
        self.assertEqual(lead.raw["tax_amount"], "4633.0")

    def test_last_sale_price_never_becomes_an_asking_price(self):
        # What a previous buyer paid is not what this seller wants. Confusing
        # the two corrupts every offer computed downstream.
        lead = to_lead(SAMPLE_RECORD)
        self.assertIsNone(lead.asking_price)
        self.assertEqual(lead.raw["last_sale_price"], "260000.0")

    def test_owner_occupied_false_is_a_reported_absentee_owner(self):
        lead = to_lead(SAMPLE_RECORD)
        self.assertIs(lead.absentee_owner, True)

    def test_a_non_owner_occupied_property_is_not_claimed_to_have_a_tenant(self):
        # RentCast publishes no vacancy flag, so "owner lives elsewhere" does
        # not distinguish rented from empty — and that difference is most of
        # what makes a lead worth driving to.
        self.assertIs(to_lead(SAMPLE_RECORD).occupancy, Occupancy.UNKNOWN)
        self.assertIs(
            to_lead(dict(SAMPLE_RECORD, ownerOccupied=True)).occupancy,
            Occupancy.OWNER_OCCUPIED,
        )

    def test_signals_rentcast_does_not_report_stay_unknown(self):
        # None never scores and never rejects. False would be a claim RentCast
        # never made.
        lead = to_lead(SAMPLE_RECORD)
        for signal in ("vacant", "pre_foreclosure", "foreclosure", "tax_delinquent",
                       "probate", "code_violation"):
            self.assertIsNone(getattr(lead, signal), signal)

    def test_a_missing_owner_block_leaves_the_owner_blank(self):
        record = {k: v for k, v in SAMPLE_RECORD.items() if k != "owner"}
        lead = to_lead(record)
        self.assertEqual(lead.owner_name, "")
        self.assertNotIn("owner_mailing_address", lead.raw)

    def test_a_nested_object_never_becomes_a_stringified_number(self):
        record = dict(SAMPLE_RECORD, squareFootage={"value": 1878}, bedrooms={})
        lead = to_lead(record)
        self.assertIsNone(lead.sqft)
        self.assertIsNone(lead.beds)


class Capabilities(unittest.TestCase):
    def test_property_detail_is_not_declared(self):
        # The funnel calls get_property once per researched lead (up to
        # MAX_RESEARCH=100). At one billed request each that is the whole
        # monthly plan spent twice over re-fetching what search returned.
        self.assertNotIn(Capability.PROPERTY, RentCastProvider.capabilities)

    def test_asking_for_property_detail_costs_nothing_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp))
            response = provider.get_property(Lead(address="1 Main St"))
            self.assertFalse(response.supported)
            self.assertEqual(provider.usage.used, 0)
            self.assertEqual(provider.client.calls, [])


class Search(unittest.TestCase):
    def test_one_search_is_one_billed_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            result = provider.search_properties(
                HuntCriteria(states=("TX",), zip_codes=("78244",))
            )
            self.assertEqual(len(result.data), 1)
            self.assertEqual(provider.usage.search_calls, 1)
            self.assertEqual(provider.ledger.used, 1)

    def test_the_request_asks_for_a_full_page_and_no_price_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            provider.search_properties(
                HuntCriteria(
                    states=("TX",), zip_codes=("78244",),
                    min_price=50_000, max_price=2_200_000,
                )
            )
            path, params = provider.client.calls[0]
            self.assertEqual(path, "properties")
            self.assertEqual(params["limit"], 500)
            self.assertEqual(params["zipCode"], "78244")
            # RentCast documents no price filter; sending one wastes the
            # request. The band is applied locally instead.
            self.assertNotIn("minPrice", params)
            self.assertNotIn("maxPrice", params)

    def test_extra_geographies_are_reported_not_silently_charged(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            provider.search_properties(
                HuntCriteria(states=("FL",), zip_codes=("33607", "33609", "33611"))
            )
            self.assertEqual(len(provider.client.calls), 1)
            self.assertTrue(
                any("were not searched" in w for w in provider.warnings), provider.warnings
            )

    def test_a_second_identical_search_is_served_from_cache_for_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            criteria = HuntCriteria(states=("TX",), zip_codes=("78244",))
            provider.search_properties(criteria)
            provider.search_properties(criteria)
            self.assertEqual(len(provider.client.calls), 1)
            self.assertEqual(provider.ledger.used, 1)
            self.assertEqual(provider.usage.cache_hits, 1)

    def test_a_failed_request_is_never_billed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(
                Path(tmp), error=HttpError("boom", status=500)
            )
            result = provider.search_properties(HuntCriteria(states=("TX",)))
            self.assertEqual(result.data, [])
            self.assertEqual(provider.ledger.used, 0)

    def test_a_rejected_credential_is_never_billed_and_names_the_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), error=HttpError("nope", status=401))
            result = provider.search_properties(HuntCriteria(states=("TX",)))
            self.assertEqual(provider.ledger.used, 0)
            self.assertIn("RENTCAST_API_KEY", result.reason)

    def test_a_spent_quota_refuses_before_the_request_not_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD], limit=50, used=50)
            result = provider.search_properties(HuntCriteria(states=("TX",)))
            self.assertEqual(provider.client.calls, [])
            self.assertTrue(provider.usage.stopped_by_budget)
            self.assertIn("quota", result.reason.lower())

    def test_a_record_with_no_address_is_skipped_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(
                Path(tmp), response=[SAMPLE_RECORD, {"id": "x", "city": "Tampa"}]
            )
            result = provider.search_properties(HuntCriteria(states=("TX",)))
            self.assertEqual(len(result.data), 1)
            self.assertTrue(any("no usable address" in w for w in provider.warnings))


class FreeReads(unittest.TestCase):
    def test_owner_and_tax_are_answered_from_the_record_at_no_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            lead = provider.search_properties(HuntCriteria(states=("TX",))).data[0]
            before = provider.ledger.used

            owner = provider.get_owner(lead)
            tax = provider.get_tax_data(lead)
            distress = provider.get_distress_data(lead)

            self.assertEqual(owner.data["owner_name"], "Jane Q Sample")
            self.assertEqual(tax.data["assessed_value"], "225000.0")
            self.assertIs(distress.data["absentee_owner"], True)
            self.assertEqual(provider.ledger.used, before)
            self.assertEqual(len(provider.client.calls), 1)

    def test_owner_data_never_carries_a_phone_or_an_email(self):
        # Ownership of record only. Contact data is skip tracing, which lives
        # behind its own interface and its own compliance requirements.
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            lead = provider.search_properties(HuntCriteria(states=("TX",))).data[0]
            keys = " ".join(provider.get_owner(lead).data)
            for forbidden in ("phone", "email", "mobile"):
                self.assertNotIn(forbidden, keys.lower())

    def test_distress_says_what_rentcast_cannot_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp))
            response = provider.get_distress_data(Lead(address="1 Main St"))
            self.assertIsNone(response.data)
            self.assertTrue(response.supported)
            self.assertIn("foreclosure", response.reason.lower())


class Valuation(unittest.TestCase):
    AVM = {
        "price": 315000,
        "priceRangeLow": 290000,
        "priceRangeHigh": 340000,
        "comparables": [
            {
                "formattedAddress": "1 Elm St, San Antonio, TX 78244",
                "price": 310000, "bedrooms": 3, "bathrooms": 2,
                "squareFootage": 1850, "yearBuilt": 1975, "distance": 0.4,
                "removedDate": "2024-03-01",
            },
            {"formattedAddress": "2 Oak St", "bedrooms": 3},  # no price -> dropped
        ],
    }

    def test_a_valuation_is_an_unverified_claim_not_a_verified_arv(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=self.AVM)
            response = provider.get_valuation(Lead(address="5500 Grand Lake Dr", city="San Antonio", state="TX"))
            self.assertEqual(response.data["estimated_value"], 315000.0)
            self.assertIn("unverified", response.reason.lower())

    def test_valuation_and_comps_share_one_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=self.AVM)
            lead = Lead(address="5500 Grand Lake Dr", city="San Antonio", state="TX")
            provider.get_valuation(lead)
            comps = provider.get_comps(lead)
            self.assertEqual(len(provider.client.calls), 1)
            self.assertEqual(provider.ledger.used, 1)
            self.assertEqual(len(comps.data), 1)

    def test_a_comp_without_a_price_is_never_counted(self):
        self.assertIsNone(to_comp({"formattedAddress": "2 Oak St", "bedrooms": 3}))
        self.assertIsNone(to_comp({"price": 100000}))

    def test_an_unrecognised_valuation_shape_leaves_the_value_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response={"somethingElse": 1})
            response = provider.get_valuation(Lead(address="1 Main St", city="Tampa", state="FL"))
            self.assertIsNone(response.data)
            self.assertTrue(response.supported)

    def test_the_search_reserve_stops_valuations_eating_the_month(self):
        # Four requests left, four reserved for scheduled searches: a
        # valuation must not take one of them.
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(
                Path(tmp), response=self.AVM, limit=50, used=46, search_reserve=4
            )
            response = provider.get_valuation(Lead(address="1 Main St", city="Tampa", state="FL"))
            self.assertEqual(provider.client.calls, [])
            self.assertIn("held back", response.reason)
            # A search still gets through: the reserve exists for it.
            provider.client.response = [SAMPLE_RECORD]
            self.assertEqual(
                len(provider.search_properties(HuntCriteria(states=("FL",))).data), 1
            )

    def test_a_lead_with_no_address_is_never_valued(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=self.AVM)
            response = provider.get_valuation(Lead(address=""))
            self.assertEqual(provider.client.calls, [])
            self.assertIsNone(response.data)


class HealthAndStatus(unittest.TestCase):
    def test_the_health_check_never_spends_a_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp))
            provider.health_check()
            self.assertEqual(provider.client.calls, [])

    def test_status_reports_usage_without_leaking_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(Path(tmp), response=[SAMPLE_RECORD])
            provider.search_properties(HuntCriteria(states=("TX",)))
            text = provider.status()
            self.assertIn("RENTCAST QUOTA", text)
            self.assertNotIn("Jane Q Sample", text)


class Registration(unittest.TestCase):
    def test_rentcast_is_registered_but_refuses_without_a_key(self):
        import os

        from wholesale_engine.providers import registry
        from wholesale_engine.providers.base import ProviderNotConfigured

        self.assertIn("rentcast", registry.registered_names())
        entry = registry.registration("rentcast")
        self.assertEqual(entry.required_settings, ("RENTCAST_API_KEY",))
        self.assertFalse(entry.is_local)

        saved = os.environ.pop("RENTCAST_API_KEY", None)
        try:
            with self.assertRaises(ProviderNotConfigured):
                registry.get_provider("rentcast")
        finally:
            if saved is not None:
                os.environ["RENTCAST_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
