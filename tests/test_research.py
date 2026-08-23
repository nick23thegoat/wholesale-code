"""Wave 4 research engine: facts, property research, owner, distress, equity.

The rule every test here defends: an unknown stays visibly unknown, and
nothing in this layer manufactures a value to fill a gap.
"""

from __future__ import annotations

import unittest
from datetime import date

from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.models.enums import Condition, Occupancy, PropertyType
from wholesale_engine.providers import Capability, CsvProvider, HuntCriteria
from wholesale_engine.providers.base import ProviderResponse
from wholesale_engine.research import (
    DISTRESS_SIGNALS,
    URGENT_SIGNALS,
    Confidence,
    DistressProfile,
    EquityStatus,
    Fact,
    OwnerResearchService,
    PropertyResearchService,
    assess_equity,
    best,
    looks_like_entity,
    profile_from_lead,
    profile_from_public_records,
)
from wholesale_engine.research.models import (
    FORECLOSURE_PRE,
    TAX_STATUS_DELINQUENT,
)


def lead(**kwargs) -> Lead:
    base = dict(
        lead_id="L1", address="123 Main St", city="Tampa", state="FL",
        zip_code="33601", source="test-list",
    )
    base.update(kwargs)
    return Lead(**base)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


class FactTests(unittest.TestCase):
    def test_an_unknown_fact_has_no_value_and_no_source(self):
        fact = Fact.unknown("nobody looked")
        self.assertFalse(fact.is_known)
        self.assertIsNone(fact.value)
        self.assertIs(fact.confidence, Confidence.UNKNOWN)

    def test_reporting_none_collapses_to_unknown(self):
        # The one way a value could sneak in without a source.
        self.assertFalse(Fact.reported(None, "county_records").is_known)

    def test_a_reported_fact_carries_its_source_and_confidence(self):
        fact = Fact.reported(True, "county_records", Confidence.HIGH)
        self.assertTrue(fact.is_true)
        self.assertEqual(fact.source, "county_records")
        self.assertIs(fact.confidence, Confidence.HIGH)

    def test_false_is_a_known_fact_not_an_unknown(self):
        fact = Fact.reported(False, "county_records", Confidence.HIGH)
        self.assertTrue(fact.is_known)
        self.assertFalse(fact.is_true)

    def test_best_prefers_the_stronger_source(self):
        county = Fact.reported(True, "county_records", Confidence.HIGH)
        listing = Fact.reported(False, "lead_list", Confidence.LOW)
        self.assertIs(best(listing, county), county)

    def test_best_falls_back_to_unknown_when_nothing_is_known(self):
        self.assertFalse(best(Fact.unknown(), Fact.unknown()).is_known)

    def test_confidence_ranks_and_weights_are_ordered(self):
        order = [Confidence.UNKNOWN, Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH]
        self.assertEqual([c.rank for c in order], sorted(c.rank for c in order))
        self.assertEqual([c.weight for c in order], sorted(c.weight for c in order))

    def test_confidence_parses_forgivingly(self):
        self.assertIs(Confidence.parse("high"), Confidence.HIGH)
        self.assertIs(Confidence.parse("nonsense"), Confidence.UNKNOWN)
        self.assertIs(Confidence.parse(None), Confidence.UNKNOWN)


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------


class EquityTests(unittest.TestCase):
    def test_the_full_calculation(self):
        result = assess_equity(estimated_value=200_000, mortgage_balance=60_000, liens=5_000)
        self.assertEqual(result.equity_amount, 135_000)
        self.assertIs(result.equity_status, EquityStatus.CALCULATED)
        self.assertAlmostEqual(result.equity_percentage, 0.675)

    def test_liens_default_to_none_not_zero(self):
        # No lien search was run, so the caveat has to say so.
        result = assess_equity(estimated_value=200_000, mortgage_balance=60_000)
        self.assertEqual(result.equity_amount, 140_000)
        self.assertTrue(any("lien search" in c for c in result.caveats))

    def test_value_minus_asking_is_labelled_a_spread_not_equity(self):
        result = assess_equity(estimated_value=200_000, asking_price=120_000)
        self.assertIs(result.equity_status, EquityStatus.DERIVED)
        self.assertFalse(result.is_verified_enough_to_lean_on)
        self.assertTrue(any("NOT equity" in c for c in result.caveats))

    def test_a_missing_mortgage_never_becomes_zero(self):
        result = assess_equity(estimated_value=200_000)
        self.assertIsNone(result.equity_amount)
        self.assertIs(result.equity_status, EquityStatus.UNKNOWN)

    def test_no_inputs_is_unknown_with_a_reason(self):
        result = assess_equity()
        self.assertIs(result.equity_status, EquityStatus.UNKNOWN)
        self.assertIn("need", result.basis)

    def test_a_source_supplied_figure_is_labelled_reported(self):
        result = assess_equity(estimated_value=200_000, reported_equity=90_000)
        self.assertIs(result.equity_status, EquityStatus.REPORTED)
        self.assertIs(result.equity_confidence, Confidence.LOW)

    def test_negative_equity_is_flagged_not_clamped(self):
        result = assess_equity(estimated_value=100_000, mortgage_balance=140_000)
        self.assertEqual(result.equity_amount, -40_000)
        self.assertTrue(any("Negative equity" in c for c in result.caveats))

    def test_confidence_never_exceeds_the_weakest_input(self):
        result = assess_equity(
            estimated_value=200_000, mortgage_balance=60_000,
            value_confidence=Confidence.LOW, mortgage_confidence=Confidence.HIGH,
        )
        self.assertIs(result.equity_confidence, Confidence.LOW)

    def test_high_equity_uses_the_thirty_five_percent_bar(self):
        self.assertTrue(
            assess_equity(estimated_value=200_000, mortgage_balance=100_000).is_high_equity
        )
        self.assertFalse(
            assess_equity(estimated_value=200_000, mortgage_balance=140_000).is_high_equity
        )


# ---------------------------------------------------------------------------
# Distress
# ---------------------------------------------------------------------------


class DistressTests(unittest.TestCase):
    def test_every_signal_starts_unknown(self):
        profile = DistressProfile()
        self.assertEqual(profile.count, 0)
        self.assertEqual(len(profile.unknown), len(DISTRESS_SIGNALS))

    def test_deferred_maintenance_is_a_recognised_signal(self):
        self.assertIn("deferred_maintenance", DISTRESS_SIGNALS)

    def test_each_signal_carries_a_source_and_confidence(self):
        profile = profile_from_public_records({"pre_foreclosure": True})
        fact = profile.get("pre_foreclosure")
        self.assertTrue(fact.is_true)
        self.assertEqual(fact.source, "county_records")
        self.assertIs(fact.confidence, Confidence.HIGH)

    def test_a_missing_key_leaves_the_signal_unknown(self):
        # A record that does not mention foreclosure is not a record saying no.
        profile = profile_from_public_records({"vacant": True})
        self.assertFalse(profile.get("foreclosure").is_known)

    def test_a_non_boolean_value_is_ignored(self):
        profile = profile_from_public_records({"probate": "maybe", "vacant": None})
        self.assertFalse(profile.get("probate").is_known)
        self.assertFalse(profile.get("vacant").is_known)

    def test_vacancy_is_read_from_occupancy(self):
        profile = profile_from_lead(lead(occupancy=Occupancy.VACANT))
        self.assertTrue(profile.get("vacant").is_true)

    def test_heavy_condition_implies_deferred_maintenance(self):
        profile = profile_from_lead(lead(condition=Condition.HEAVY))
        self.assertTrue(profile.get("deferred_maintenance").is_true)

    def test_turnkey_condition_rules_deferred_maintenance_out(self):
        profile = profile_from_lead(lead(condition=Condition.TURNKEY))
        self.assertIs(profile.get("deferred_maintenance").value, False)

    def test_notes_can_evidence_deferred_maintenance_at_low_confidence(self):
        profile = profile_from_lead(lead(notes="Sold as-is, roof leak reported."))
        fact = profile.get("deferred_maintenance")
        self.assertTrue(fact.is_true)
        self.assertIs(fact.confidence, Confidence.LOW)

    def test_merging_prefers_the_public_record(self):
        listing = profile_from_lead(lead(pre_foreclosure=False))
        county = profile_from_public_records({"pre_foreclosure": True})
        merged = listing.merge(county)
        self.assertTrue(merged.get("pre_foreclosure").is_true)
        self.assertEqual(merged.get("pre_foreclosure").source, "county_records")

    def test_urgent_signals_are_counted_separately(self):
        profile = profile_from_lead(lead(probate=True, absentee_owner=True))
        self.assertEqual(profile.count, 2)
        self.assertEqual(profile.urgent_count, 1)
        self.assertIn("probate", URGENT_SIGNALS)

    def test_a_signal_reported_false_is_ruled_out_not_unknown(self):
        profile = profile_from_lead(lead(vacant=False))
        self.assertIn("vacant", profile.ruled_out)
        self.assertNotIn("vacant", profile.unknown)


# ---------------------------------------------------------------------------
# Owner research
# ---------------------------------------------------------------------------


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.service = OwnerResearchService()

    def test_no_source_means_no_owner_and_no_invention(self):
        record = self.service.research(lead())
        self.assertFalse(record.owner_name.is_known)
        self.assertIn("owner_name", record.missing_fields)

    def test_a_placeholder_name_is_not_treated_as_an_owner(self):
        record = self.service.research(lead(owner_name="unknown"))
        self.assertFalse(record.owner_name.is_known)
        self.assertTrue(any("placeholder" in n for n in record.notes))

    def test_an_llc_is_detected_as_an_entity(self):
        record = self.service.research(lead(owner_name="SUNSHINE HOLDINGS LLC"))
        self.assertTrue(record.is_entity.is_true)
        self.assertEqual(record.entity_type.value, "LLC")

    def test_a_trust_is_detected(self):
        record = self.service.research(lead(owner_name="SMITH FAMILY TRUST"))
        self.assertEqual(record.entity_type.value, "TRUST")

    def test_a_person_is_not_an_entity(self):
        record = self.service.research(lead(owner_name="JANE DOE"))
        self.assertIs(record.is_entity.value, False)
        self.assertFalse(looks_like_entity("JANE DOE"))

    def test_an_entity_owner_raises_a_signing_note(self):
        record = self.service.research(lead(owner_name="ACME PROPERTIES LLC"))
        self.assertTrue(any("authorised signer" in n for n in record.notes))

    def test_no_contact_field_exists_anywhere_on_the_record(self):
        record = self.service.research(lead(owner_name="JANE DOE"))
        for forbidden in ("phone", "email", "mobile", "cell"):
            self.assertFalse(
                any(forbidden in name for name in record.fields),
                f"an owner record must never carry a {forbidden} field",
            )

    def test_a_provider_record_outranks_the_lead_list(self):
        class OwnerProvider(CsvProvider):
            capabilities = (Capability.SEARCH, Capability.OWNER)

            def get_owner(self, lead):
                return ProviderResponse(
                    data={"owner_name": "REAL OWNER OF RECORD", "properties_owned": 4},
                    source="county_records",
                )

        service = OwnerResearchService(OwnerProvider(SAMPLE_LEADS))
        record = service.research(lead(owner_name="LIST GUESS"))
        self.assertEqual(record.owner_name.value, "REAL OWNER OF RECORD")
        self.assertTrue(record.is_likely_portfolio_owner)

    def test_absentee_is_inferred_from_a_mailing_address_elsewhere(self):
        class MailProvider(CsvProvider):
            capabilities = (Capability.SEARCH, Capability.OWNER)

            def get_owner(self, lead):
                return ProviderResponse(
                    data={
                        "owner_name": "JANE DOE",
                        "owner_mailing_address": "900 Elsewhere Ave, Denver CO",
                    },
                    source="county_records",
                )

        record = OwnerResearchService(MailProvider(SAMPLE_LEADS)).research(lead())
        self.assertTrue(record.absentee_owner.is_true)


# ---------------------------------------------------------------------------
# Property research service
# ---------------------------------------------------------------------------


class PropertyResearchTests(unittest.TestCase):
    def setUp(self):
        self.provider = CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS)
        self.service = PropertyResearchService(self.provider)
        self.leads = self.provider.search_properties(HuntCriteria()).data

    def test_it_works_with_no_provider_at_all(self):
        result = PropertyResearchService().research(lead(asking_price=100_000))
        self.assertEqual(result.address, "123 Main St")
        self.assertTrue(result.missing_fields)

    def test_every_researched_field_is_a_fact(self):
        result = self.service.research(self.leads[0])
        for name, fact in result.researched_fields.items():
            self.assertTrue(hasattr(fact, "confidence"), name)
            self.assertTrue(hasattr(fact, "source"), name)

    def test_missing_fields_are_reported_not_filled(self):
        result = self.service.research(self.leads[0])
        self.assertIn("mortgage_balance", result.missing_fields)
        self.assertFalse(result.mortgage_balance.is_known)

    def test_the_lead_list_is_never_high_confidence(self):
        result = self.service.research(self.leads[0])
        self.assertNotEqual(result.estimated_value.confidence, Confidence.HIGH)

    def test_tax_status_stays_unknown_without_a_signal(self):
        result = self.service.research(lead(asking_price=100_000))
        self.assertFalse(result.tax_status.is_known)
        self.assertIn("no tax record checked", result.tax_status.note)

    def test_tax_status_follows_the_delinquency_signal(self):
        result = self.service.research(lead(tax_delinquent=True))
        self.assertEqual(result.tax_status.value, TAX_STATUS_DELINQUENT)

    def test_foreclosure_status_stays_unknown_when_nobody_checked(self):
        result = self.service.research(lead())
        self.assertFalse(result.foreclosure_status.is_known)

    def test_foreclosure_status_rolls_up_the_signals(self):
        result = self.service.research(lead(pre_foreclosure=True))
        self.assertEqual(result.foreclosure_status.value, FORECLOSURE_PRE)

    def test_active_foreclosure_outranks_pre_foreclosure(self):
        result = self.service.research(lead(pre_foreclosure=True, foreclosure=True))
        self.assertEqual(result.foreclosure_status.value, "FORECLOSURE")

    def test_equity_is_derived_and_labelled(self):
        result = self.service.research(lead(estimated_value=200_000, asking_price=120_000))
        self.assertIs(result.equity.equity_status, EquityStatus.DERIVED)

    def test_high_equity_is_not_set_from_a_mere_spread(self):
        # A spread is not equity, so it must not light the scoring signal.
        result = self.service.research(
            lead(estimated_value=300_000, asking_price=100_000)
        )
        derived = result.distress.get("high_equity")
        self.assertFalse(
            derived.is_known and derived.source == "derived",
            "a value-minus-asking spread must not set the high-equity signal",
        )

    def test_high_equity_is_set_from_a_real_calculation(self):
        class RecordsProvider(CsvProvider):
            capabilities = (Capability.SEARCH, Capability.DISTRESS)

            def get_distress_data(self, lead):
                return ProviderResponse(
                    data={"mortgage_balance": 40_000}, source="county_records"
                )

        service = PropertyResearchService(RecordsProvider(SAMPLE_LEADS))
        result = service.research(lead(estimated_value=200_000, asking_price=150_000))
        self.assertIs(result.equity.equity_status, EquityStatus.CALCULATED)
        self.assertTrue(result.distress.get("high_equity").is_true)

    def test_research_records_its_sources(self):
        result = self.service.research(self.leads[0])
        self.assertTrue(result.sources_used)

    def test_completeness_reflects_what_is_actually_known(self):
        sparse = self.service.research(lead())
        rich = self.service.research(self.leads[0])
        self.assertLess(sparse.completeness, rich.completeness)

    def test_the_export_view_flattens_facts_to_values(self):
        row = self.service.research(self.leads[0]).as_dict()
        self.assertIn("missing_fields", row)
        self.assertIn("distress_signals", row)
        self.assertNotIsInstance(row["estimated_value"], Fact)

    def test_research_costs_at_most_one_call_per_capability(self):
        class FullProvider(CsvProvider):
            capabilities = (Capability.SEARCH, Capability.PROPERTY, Capability.DISTRESS)

            def get_property(self, lead):
                return ProviderResponse(data=lead, source="provider")

            def get_distress_data(self, lead):
                return ProviderResponse(data={"vacant": True}, source="county_records")

        provider = FullProvider(SAMPLE_LEADS)
        service = PropertyResearchService(provider)
        service.research(lead())
        self.assertEqual(provider.metrics.detail_calls, 1)
        self.assertEqual(provider.metrics.distress_calls, 1)

    def test_research_never_asks_for_comps(self):
        # Comps are the expensive stage and are sequenced by the hunt, not here.
        self.service.research(self.leads[0])
        self.assertEqual(self.provider.metrics.comp_calls, 1)  # the one bulk file read

    def test_batch_research_is_keyed_by_lead_id(self):
        results = self.service.research_all(self.leads[:3])
        self.assertEqual(len(results), 3)
        for key, value in results.items():
            self.assertEqual(value.lead_id or value.display_id(), key)


if __name__ == "__main__":
    unittest.main()
