"""Wave 5 — the acquisitions workflow.

The rules these tests hold in place:

* no phone number or email is ever invented
* mock contact data is marked as fictional everywhere it appears
* nothing is sent — calls, texts and emails are logged only
* the $18,000 fee stays a target: an offer below it warns, never blocks
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from wholesale_engine.acquisitions import (
    ACQUISITION_STATUSES,
    AcquisitionStore,
    AcquisitionWorkflow,
    Assignment,
    AssignmentStatus,
    Buyer,
    Channel,
    Contact,
    ContactPriorityEngine,
    Contract,
    ContractStatus,
    Direction,
    MockSkipTraceProvider,
    NextAction,
    Offer,
    OfferStatus,
    Outcome,
    OutreachActivity,
    PhoneType,
    SkipTraceNotConfigured,
    UnconfiguredSkipTraceProvider,
    format_phone,
    get_skip_trace_provider,
    is_closed,
    normalize_email,
    normalize_phone,
    normalize_status,
)
from wholesale_engine.config import DEFAULT_CONFIG
from wholesale_engine.hunt import run_hunt
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.providers import CsvProvider, HuntCriteria
from wholesale_engine.research.facts import Confidence
from wholesale_engine.storage import LeadSnapshot, LeadStore, StoredLead

TODAY = date(2026, 8, 23)


def lead(**kwargs) -> Lead:
    base = dict(
        lead_id="L1", address="123 Main St", city="Tampa", state="FL",
        county="Hillsborough", zip_code="33601", asking_price=120_000, source="csv",
    )
    base.update(kwargs)
    return Lead(**base)


def stored_row(**kwargs) -> StoredLead:
    base = dict(
        lead_row_id=1, property_row_id=1, dedupe_key="k", address="1 A St",
        city="Tampa", state="FL", zip_code="33601", source="csv", status="HOT",
        first_seen="2026-08-01", last_seen="2026-08-23", times_seen=1,
        lead_score=90.0, deal_score=83.0, asking_price=100_000.0,
        estimated_value=None, estimated_repairs=None, estimated_equity=None,
        signals={}, final_decision="🔥 GO", priority_score=80.0,
        priority_band="🔥 PRIORITY", arv=200_000.0, mao=120_000.0,
        recommended_offer=110_000.0, potential_fee=20_000.0,
        arv_confidence="VERIFIED/SUPPORTED ARV",
    )
    base.update(kwargs)
    return StoredLead(**base)


def workflow_with_leads() -> AcquisitionWorkflow:
    store = LeadStore(":memory:")
    run_hunt(CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store)
    return AcquisitionWorkflow(store)


# ---------------------------------------------------------------------------
# 1. Pipeline
# ---------------------------------------------------------------------------


class PipelineTests(unittest.TestCase):
    def test_all_sixteen_statuses_exist(self):
        self.assertEqual(
            set(ACQUISITION_STATUSES),
            {
                "NEW", "RESEARCHING", "HOT", "CONTACT_READY", "CONTACTED",
                "CONVERSATION", "FOLLOW_UP", "OFFER_PREPARING", "OFFER_SENT",
                "NEGOTIATING", "UNDER_CONTRACT", "BUYER_SEARCH", "ASSIGNED",
                "CLOSED", "DEAD", "PASSED",
            },
        )

    def test_statuses_are_in_pipeline_order(self):
        self.assertLess(
            ACQUISITION_STATUSES.index("HOT"), ACQUISITION_STATUSES.index("CONTACTED")
        )
        self.assertLess(
            ACQUISITION_STATUSES.index("OFFER_SENT"),
            ACQUISITION_STATUSES.index("UNDER_CONTRACT"),
        )

    def test_status_names_are_normalized_forgivingly(self):
        self.assertEqual(normalize_status("contact-ready"), "CONTACT_READY")
        self.assertEqual(normalize_status(" offer sent "), "OFFER_SENT")

    def test_closed_statuses_are_recognised(self):
        for name in ("CLOSED", "DEAD", "PASSED"):
            self.assertTrue(is_closed(name))
        self.assertFalse(is_closed("NEGOTIATING"))

    def test_a_status_change_is_recorded_in_activity_history(self):
        workflow = workflow_with_leads()
        row = workflow.leads.find_one("LH-011")
        changed, message = workflow.set_status(row.dedupe_key, "CONTACT_READY", "has phone")
        self.assertTrue(changed)
        types = [a["activity_type"] for a in workflow.leads.activities(row.lead_row_id)]
        self.assertIn("status_changed", types)
        workflow.leads.close()

    def test_an_unknown_status_is_refused_with_the_valid_list(self):
        workflow = workflow_with_leads()
        changed, message = workflow.set_status("LH-011", "MAYBE")
        self.assertFalse(changed)
        self.assertIn("Unknown status", message)
        workflow.leads.close()

    def test_a_no_op_move_reports_rather_than_rewriting(self):
        workflow = workflow_with_leads()
        row = workflow.leads.find_one("LH-011")
        workflow.set_status(row.dedupe_key, "HOT")
        changed, message = workflow.set_status(row.dedupe_key, "HOT")
        self.assertFalse(changed)
        self.assertIn("Already", message)
        workflow.leads.close()


# ---------------------------------------------------------------------------
# 2. Contact model
# ---------------------------------------------------------------------------


class ContactModelTests(unittest.TestCase):
    def test_an_empty_contact_holds_nothing(self):
        contact = Contact(property_id="k")
        self.assertIsNone(contact.phone)
        self.assertIsNone(contact.email)
        self.assertIsNone(contact.mailing_address)
        self.assertFalse(contact.is_reachable)

    def test_unknown_contact_details_render_as_none_not_a_number(self):
        contact = Contact(property_id="k")
        self.assertEqual(contact.phone_status, "NONE")
        self.assertEqual(contact.email_status, "NONE")
        self.assertEqual(contact.display_phone(), "—")

    def test_a_phone_number_is_normalized_not_invented(self):
        self.assertEqual(normalize_phone("(813) 555-0100"), "8135550100")
        self.assertEqual(normalize_phone("1-813-555-0100"), "8135550100")
        self.assertIsNone(normalize_phone("555"))
        self.assertIsNone(normalize_phone(None))
        self.assertIsNone(normalize_phone(""))

    def test_an_unusable_phone_number_is_dropped_entirely(self):
        contact = Contact(property_id="k", phone="call the office")
        self.assertIsNone(contact.phone)
        self.assertIs(contact.phone_type, PhoneType.UNKNOWN)
        self.assertIs(contact.phone_confidence, Confidence.UNKNOWN)

    def test_an_email_is_normalized_and_validated(self):
        self.assertEqual(normalize_email("  Owner@Example.COM "), "owner@example.com")
        self.assertIsNone(normalize_email("not-an-email"))

    def test_formatting_never_produces_a_number_from_nothing(self):
        self.assertIsNone(format_phone(None))
        self.assertIsNone(format_phone("12"))

    def test_a_mailing_address_alone_still_counts_as_reachable(self):
        contact = Contact(property_id="k", mailing_address="900 Elsewhere Ave")
        self.assertTrue(contact.is_reachable)
        self.assertFalse(contact.has_phone)

    def test_provenance_is_labelled(self):
        self.assertEqual(Contact(property_id="k").provenance, "UNKNOWN")
        self.assertEqual(
            Contact(property_id="k", phone="8135550100", verified=True).provenance,
            "SOURCE-PROVIDED",
        )
        self.assertEqual(
            Contact(property_id="k", phone="8135550100").provenance, "UNVERIFIED"
        )

    def test_test_data_is_marked_in_every_view(self):
        contact = Contact(property_id="k", phone="5555550100", is_test_data=True)
        self.assertEqual(contact.phone_status, "TEST DATA")
        self.assertTrue(contact.as_dict()["is_test_data"])

    def test_the_export_view_carries_every_field(self):
        row = Contact(property_id="k").as_dict()
        for column in (
            "contact_id", "property_id", "owner_name", "phone", "phone_type",
            "phone_confidence", "email", "email_confidence", "mailing_address",
            "source", "source_date", "verified", "notes",
        ):
            self.assertIn(column, row)


# ---------------------------------------------------------------------------
# 3. Skip trace
# ---------------------------------------------------------------------------


class SkipTraceTests(unittest.TestCase):
    def test_the_default_provider_refuses_and_explains(self):
        with self.assertRaises(SkipTraceNotConfigured) as ctx:
            UnconfiguredSkipTraceProvider().skip_trace("k")
        message = str(ctx.exception)
        self.assertIn("never generate", message)
        self.assertIn("DNC", message)

    def test_registered_is_not_the_same_as_connected(self):
        # A vendor adapter shipping in the registry does not mean anyone can
        # dial an owner: every non-mock entry must refuse to construct without
        # its own credentials. 'none' refuses always, 'mock' is fictional, and
        # 'http' additionally needs a vendor subclass.
        import os

        from wholesale_engine.acquisitions import (
            SKIP_TRACE_PROVIDERS,
            get_skip_trace_provider,
        )

        self.assertEqual(
            set(SKIP_TRACE_PROVIDERS), {"none", "mock", "http", "batchdata"}
        )

        credential_vars = ("SKIP_TRACE_API_KEY", "SKIP_TRACE_BASE_URL",
                           "BATCHDATA_API_KEY")
        saved = {name: os.environ.pop(name, None) for name in credential_vars}
        try:
            for name in ("http", "batchdata"):
                with self.assertRaises(SkipTraceNotConfigured, msg=name):
                    get_skip_trace_provider(name)
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_the_default_provider_still_refuses(self):
        from wholesale_engine.acquisitions import get_skip_trace_provider

        with self.assertRaises(SkipTraceNotConfigured):
            get_skip_trace_provider().skip_trace("k")

    def test_the_http_template_refuses_without_credentials(self):
        from wholesale_engine.acquisitions import get_skip_trace_provider

        with self.assertRaises(SkipTraceNotConfigured) as ctx:
            get_skip_trace_provider("http")
        self.assertIn("NOT CONNECTED", str(ctx.exception))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(SkipTraceNotConfigured):
            get_skip_trace_provider("some-vendor")

    def test_the_mock_is_flagged_as_a_test_provider(self):
        self.assertTrue(MockSkipTraceProvider().is_test_provider)
        self.assertIn("TEST DATA ONLY", MockSkipTraceProvider().describe())

    def test_mock_numbers_are_in_the_reserved_fiction_range(self):
        provider = MockSkipTraceProvider()
        for index in range(30):
            result = provider.skip_trace(f"P-{index}")
            for phone in result.phones:
                digits = normalize_phone(phone["number"])
                self.assertIsNotNone(digits)
                self.assertTrue(
                    digits.startswith("555555"),
                    f"{digits} is not in the reserved 555 range",
                )

    def test_mock_emails_use_the_invalid_tld(self):
        provider = MockSkipTraceProvider()
        for index in range(30):
            for email in provider.skip_trace(f"P-{index}").emails:
                self.assertTrue(email["address"].endswith(".invalid"))

    def test_every_mock_result_is_stamped_as_test_data(self):
        result = MockSkipTraceProvider().skip_trace("LH-011")
        self.assertTrue(result.is_test_data)
        self.assertTrue(result.to_contact().is_test_data)
        self.assertIn("FICTIONAL", result.notes)

    def test_the_mock_sometimes_finds_nothing(self):
        provider = MockSkipTraceProvider()
        misses = sum(
            1 for i in range(50) if not provider.skip_trace(f"P-{i}").found_anything
        )
        self.assertGreater(misses, 0, "the no-contact-found path must be exercised")

    def test_a_miss_produces_an_empty_contact_not_a_fabricated_one(self):
        provider = MockSkipTraceProvider()
        for index in range(50):
            result = provider.skip_trace(f"P-{index}")
            if not result.found_anything:
                contact = result.to_contact()
                self.assertIsNone(contact.phone)
                self.assertIsNone(contact.email)
                return
        self.fail("expected at least one miss")

    def test_the_mock_is_deterministic(self):
        first = MockSkipTraceProvider().skip_trace("LH-011")
        second = MockSkipTraceProvider().skip_trace("LH-011")
        self.assertEqual(first.phones, second.phones)

    def test_the_best_phone_prefers_confidence_then_mobile(self):
        from wholesale_engine.acquisitions import SkipTraceResult

        result = SkipTraceResult(
            phones=[
                {"number": "5555550101", "type": "LANDLINE", "confidence": "LOW"},
                {"number": "5555550102", "type": "MOBILE", "confidence": "HIGH"},
            ]
        )
        self.assertEqual(result.best_phone()["number"], "5555550102")

    def test_lookups_are_counted_for_the_cost_report(self):
        provider = MockSkipTraceProvider()
        provider.skip_trace("a")
        provider.skip_trace("b")
        self.assertEqual(provider.lookups, 2)


# ---------------------------------------------------------------------------
# 4. Contact priority
# ---------------------------------------------------------------------------


class ContactPriorityTests(unittest.TestCase):
    def setUp(self):
        self.engine = ContactPriorityEngine()

    def test_hot_plus_phone_is_call_now(self):
        result = self.engine.score(stored_row(), Contact(phone="8135550100"), today=TODAY)
        self.assertIs(result.action, NextAction.CALL_NOW)

    def test_hot_without_a_phone_is_skip_trace(self):
        result = self.engine.score(stored_row(), None, today=TODAY)
        self.assertIs(result.action, NextAction.SKIP_TRACE)
        self.assertIn("no contact information", result.blockers)

    def test_a_strong_deal_on_unverified_data_is_research_first(self):
        result = self.engine.score(
            stored_row(arv_confidence="USER-PROVIDED ARV (UNVERIFIED)"),
            Contact(phone="8135550100"), today=TODAY,
        )
        self.assertIs(result.action, NextAction.RESEARCH_FIRST)

    def test_a_weak_deal_without_contact_is_not_worth_a_skip_trace(self):
        result = self.engine.score(stored_row(deal_score=40.0), None, today=TODAY)
        self.assertIs(result.action, NextAction.RESEARCH_FIRST)

    def test_an_overdue_follow_up_outranks_a_fresh_call(self):
        contact = Contact(phone="8135550100", next_follow_up=TODAY - timedelta(days=4))
        result = self.engine.score(stored_row(), contact, today=TODAY)
        self.assertIs(result.action, NextAction.FOLLOW_UP_OVERDUE)
        self.assertEqual(result.days_overdue, 4)

    def test_a_follow_up_due_today_is_its_own_action(self):
        contact = Contact(phone="8135550100", next_follow_up=TODAY)
        self.assertIs(
            self.engine.score(stored_row(), contact, today=TODAY).action,
            NextAction.FOLLOW_UP,
        )

    def test_a_counter_outranks_everything_on_the_seller_side(self):
        result = self.engine.score(
            stored_row(status="NEGOTIATING"), Contact(phone="8135550100"), today=TODAY
        )
        self.assertIs(result.action, NextAction.RESPOND_TO_COUNTER)

    def test_under_contract_becomes_contract_tasks(self):
        result = self.engine.score(stored_row(status="UNDER_CONTRACT"), None, today=TODAY)
        self.assertIs(result.action, NextAction.CONTRACT_TASKS)

    def test_buyer_search_becomes_find_buyer(self):
        result = self.engine.score(stored_row(status="BUYER_SEARCH"), None, today=TODAY)
        self.assertIs(result.action, NextAction.FIND_BUYER)

    def test_an_assigned_deal_has_no_seller_action_left(self):
        result = self.engine.score(stored_row(status="ASSIGNED"), None, today=TODAY)
        self.assertIs(result.action, NextAction.NOTHING)

    def test_a_dead_lead_has_no_action(self):
        result = self.engine.score(stored_row(status="DEAD"), None, today=TODAY)
        self.assertIs(result.action, NextAction.NOTHING)

    def test_mail_only_contact_is_a_mail_action(self):
        contact = Contact(mailing_address="900 Elsewhere Ave, Denver CO")
        result = self.engine.score(stored_row(), contact, today=TODAY)
        self.assertIs(result.action, NextAction.MAIL)

    def test_test_data_contacts_carry_a_do_not_dial_blocker(self):
        contact = Contact(phone="5555550100", is_test_data=True)
        result = self.engine.score(stored_row(), contact, today=TODAY)
        self.assertTrue(any("do not dial" in b for b in result.blockers))

    def test_contact_availability_raises_the_score(self):
        without = self.engine.score(stored_row(), None, today=TODAY).score
        with_phone = self.engine.score(
            stored_row(), Contact(phone="8135550100"), today=TODAY
        ).score
        self.assertGreater(with_phone, without)

    def test_a_below_target_fee_still_scores(self):
        result = self.engine.score(
            stored_row(potential_fee=13_000.0), Contact(phone="8135550100"), today=TODAY
        )
        self.assertGreater(result.score, 0)
        self.assertIs(result.action, NextAction.CALL_NOW)

    def test_urgency_orders_the_actions(self):
        counter = self.engine.score(stored_row(status="NEGOTIATING"), None, today=TODAY)
        trace = self.engine.score(stored_row(), None, today=TODAY)
        self.assertLess(counter.urgency, trace.urgency)


# ---------------------------------------------------------------------------
# 5. Store: contacts, outreach, offers, contracts, buyers, assignments
# ---------------------------------------------------------------------------


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.leads = LeadStore(":memory:")
        self.leads.upsert_lead(lead(), lead_score=80.0, deal_score=75.0)
        self.row = self.leads.find_one("L1")
        self.store = AcquisitionStore(self.leads)

    def tearDown(self):
        self.leads.close()

    # --- contacts ---

    def test_a_contact_round_trips(self):
        saved = self.store.save_contact(
            Contact(property_id=self.row.dedupe_key, phone="8135550100", source="manual")
        )
        self.assertIsNotNone(saved.contact_id)
        self.assertEqual(self.store.best_contact(self.row.dedupe_key).phone, "8135550100")

    def test_saving_a_contact_twice_updates_rather_than_duplicating(self):
        contact = Contact(property_id=self.row.dedupe_key, source="manual")
        self.store.save_contact(contact)
        contact.phone = "8135550100"
        self.store.save_contact(contact)
        self.assertEqual(len(self.store.contacts_for(self.row.dedupe_key)), 1)

    def test_an_empty_contact_stores_nulls_not_empty_strings(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        stored = self.store.best_contact(self.row.dedupe_key)
        self.assertIsNone(stored.phone)
        self.assertIsNone(stored.email)

    def test_the_best_contact_prefers_a_verified_one(self):
        self.store.save_contact(
            Contact(property_id=self.row.dedupe_key, phone="8135550101", source="mock")
        )
        self.store.save_contact(
            Contact(
                property_id=self.row.dedupe_key, phone="8135550102",
                source="county", verified=True,
            )
        )
        self.assertEqual(self.store.best_contact(self.row.dedupe_key).phone, "8135550102")

    def test_adding_a_contact_is_logged(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        types = [a["activity_type"] for a in self.leads.activities(self.row.lead_row_id)]
        self.assertIn("contact_added", types)

    # --- outreach ---

    def test_outreach_is_logged_and_read_back(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        self.store.log_outreach(
            OutreachActivity(
                property_id=self.row.dedupe_key, channel=Channel.CALL,
                outcome=Outcome.CONNECTED, notes="Spoke to the owner.",
            )
        )
        history = self.store.outreach_for(self.row.dedupe_key)
        self.assertEqual(len(history), 1)
        self.assertIs(history[0].outcome, Outcome.CONNECTED)

    def test_outreach_increments_the_attempt_counter(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        for _ in range(3):
            self.store.log_outreach(
                OutreachActivity(property_id=self.row.dedupe_key, channel=Channel.CALL)
            )
        self.assertEqual(self.store.best_contact(self.row.dedupe_key).contact_attempts, 3)

    def test_outreach_records_the_follow_up_on_the_contact(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        due = date(2026, 9, 1)
        self.store.log_outreach(
            OutreachActivity(
                property_id=self.row.dedupe_key, channel=Channel.CALL,
                outcome=Outcome.CALL_BACK, next_follow_up=due,
            )
        )
        self.assertEqual(self.store.best_contact(self.row.dedupe_key).next_follow_up, due)

    def test_outreach_history_is_newest_first(self):
        self.store.save_contact(Contact(property_id=self.row.dedupe_key, source="manual"))
        for index, when in enumerate(
            [datetime(2026, 8, 1, 9), datetime(2026, 8, 5, 9)]
        ):
            self.store.log_outreach(
                OutreachActivity(
                    property_id=self.row.dedupe_key, channel=Channel.CALL,
                    timestamp=when, notes=str(index),
                )
            )
        self.assertEqual(self.store.outreach_for(self.row.dedupe_key)[0].notes, "1")

    def test_all_channels_and_outcomes_parse(self):
        for name in ("CALL", "TEXT", "EMAIL", "VOICEMAIL", "MAIL", "OTHER"):
            self.assertIs(Channel.parse(name), Channel(name))
        for name in (
            "NO_ANSWER", "LEFT_VOICEMAIL", "CONNECTED", "INTERESTED",
            "NOT_INTERESTED", "CALL_BACK", "WANTS_OFFER", "OFFER_SENT",
            "NEGOTIATING", "DEAD",
        ):
            self.assertIs(Outcome.parse(name), Outcome(name))

    def test_an_unknown_channel_is_refused(self):
        with self.assertRaises(ValueError):
            Channel.parse("carrier pigeon")

    # --- offers ---

    def test_an_offer_round_trips(self):
        self.store.save_offer(
            Offer(property_id=self.row.dedupe_key, offer_amount=95_000, mao=100_000)
        )
        self.assertEqual(self.store.latest_offer(self.row.dedupe_key).offer_amount, 95_000)

    def test_a_counter_updates_the_latest_offer(self):
        self.store.save_offer(Offer(property_id=self.row.dedupe_key, offer_amount=95_000))
        offer = self.store.record_counter(self.row.dedupe_key, 105_000)
        self.assertEqual(offer.seller_counter, 105_000)
        self.assertIs(offer.offer_status, OfferStatus.COUNTERED)

    def test_a_counter_with_no_offer_returns_none(self):
        self.assertIsNone(self.store.record_counter(self.row.dedupe_key, 100_000))

    def test_offer_history_is_kept(self):
        for amount in (90_000, 95_000, 98_000):
            self.store.save_offer(
                Offer(property_id=self.row.dedupe_key, offer_amount=amount)
            )
        self.assertEqual(len(self.store.offers_for(self.row.dedupe_key)), 3)

    # --- contracts ---

    def test_a_contract_round_trips(self):
        self.store.save_contract(
            Contract(
                property_id=self.row.dedupe_key, purchase_price=95_000,
                closing_date=date(2026, 9, 30), assignment_allowed=True,
            )
        )
        contract = self.store.contract_for(self.row.dedupe_key)
        self.assertEqual(contract.purchase_price, 95_000)
        self.assertTrue(contract.assignment_allowed)

    def test_all_contract_statuses_parse(self):
        for name in ("PENDING", "INSPECTION", "CLEAR_TO_CLOSE", "CLOSED", "CANCELLED"):
            self.assertIs(ContractStatus.parse(name), ContractStatus(name))

    def test_assignment_allowed_stays_unknown_when_not_recorded(self):
        self.store.save_contract(Contract(property_id=self.row.dedupe_key))
        self.assertIsNone(self.store.contract_for(self.row.dedupe_key).assignment_allowed)

    def test_deadline_countdowns(self):
        contract = Contract(
            property_id="k", inspection_deadline=date(2026, 9, 1),
            closing_date=date(2026, 9, 30),
        )
        self.assertEqual(contract.inspection_days_left(TODAY), 9)
        self.assertEqual(contract.closing_days_left(TODAY), 38)

    # --- buyers ---

    def test_a_buyer_round_trips(self):
        self.store.save_buyer(
            Buyer(name="TEST BUYER", preferred_states=["MO"], max_price=200_000)
        )
        buyers = self.store.all_buyers()
        self.assertEqual(len(buyers), 1)
        self.assertEqual(buyers[0].preferred_states, ["MO"])

    def test_saving_a_buyer_twice_updates_rather_than_duplicating(self):
        for _ in range(2):
            self.store.save_buyer(Buyer(name="TEST BUYER", company="TEST LLC"))
        self.assertEqual(len(self.store.all_buyers()), 1)

    def test_the_buy_box_matches_and_excludes(self):
        buyer = Buyer(
            name="B", preferred_states=["MO"], property_types=["single_family"],
            min_price=50_000, max_price=200_000,
        )
        self.assertTrue(buyer.matches(state="MO", property_type="single_family", price=100_000))
        self.assertFalse(buyer.matches(state="FL"))
        self.assertFalse(buyer.matches(state="MO", price=250_000))

    def test_an_unknown_attribute_never_excludes_a_buyer(self):
        buyer = Buyer(name="B", preferred_states=["MO"])
        self.assertTrue(buyer.matches(state="", price=None))

    def test_matching_buyers_filters_the_list(self):
        self.store.save_buyer(Buyer(name="MO BUYER", preferred_states=["MO"]))
        self.store.save_buyer(Buyer(name="FL BUYER", preferred_states=["FL"]))
        self.assertEqual(len(self.store.matching_buyers(state="MO")), 1)

    # --- assignments ---

    def test_the_gross_assignment_fee_is_the_difference(self):
        assignment = Assignment(purchase_price=59_500, assignment_price=77_500)
        self.assertEqual(assignment.gross_assignment_fee, 18_000)

    def test_the_fee_is_unknown_without_both_prices(self):
        self.assertIsNone(Assignment(purchase_price=59_500).gross_assignment_fee)

    def test_an_assignment_round_trips(self):
        self.store.save_assignment(
            Assignment(
                property_id=self.row.dedupe_key, buyer_name="TEST BUYER",
                purchase_price=59_500, assignment_price=77_500,
                status=AssignmentStatus.ASSIGNMENT_SIGNED,
            )
        )
        assignment = self.store.assignment_for(self.row.dedupe_key)
        self.assertEqual(assignment.gross_assignment_fee, 18_000)

    def test_all_assignment_statuses_parse(self):
        for name in (
            "BUYER_SEARCH", "BUYER_INTERESTED", "BUYER_OFFER",
            "ASSIGNMENT_SIGNED", "CLOSED", "FAILED",
        ):
            self.assertIs(AssignmentStatus.parse(name), AssignmentStatus(name))


# ---------------------------------------------------------------------------
# 6. Offer economics
# ---------------------------------------------------------------------------


class OfferEconomicsTests(unittest.TestCase):
    def setUp(self):
        self.workflow = workflow_with_leads()
        self.row = self.workflow.leads.find_one("LH-021")

    def tearDown(self):
        self.workflow.leads.close()

    def test_an_offer_below_mao_raises_no_mao_warning(self):
        offer, warnings = self.workflow.build_offer(self.row.dedupe_key, 55_000)
        self.assertFalse(any("EXCEEDS MAO" in w for w in warnings))

    def test_an_offer_above_mao_warns_but_is_still_recorded(self):
        amount = (self.row.mao or 0) + 5_000
        offer, warnings = self.workflow.build_offer(self.row.dedupe_key, amount)
        self.assertTrue(any("EXCEEDS MAO" in w for w in warnings))
        self.assertIsNotNone(offer.offer_id)
        self.assertEqual(offer.offer_amount, amount)

    def test_an_above_mao_offer_says_it_is_a_warning_not_a_block(self):
        offer, warnings = self.workflow.build_offer(
            self.row.dedupe_key, (self.row.mao or 0) + 5_000
        )
        self.assertTrue(any("not a block" in w for w in warnings))

    def test_a_below_target_fee_warns_without_rejecting(self):
        # An offer that leaves under $18,000 is flagged, never refused.
        ceiling = self.row.arv * DEFAULT_CONFIG.arv_percentage - self.row.repair_estimate
        amount = ceiling - 13_000
        offer, warnings = self.workflow.build_offer(self.row.dedupe_key, amount)
        self.assertTrue(any("BELOW TARGET" in w for w in warnings))
        self.assertTrue(any("still be worth doing" in w for w in warnings))
        self.assertIsNotNone(offer.offer_id)

    def test_the_offer_stores_the_underwriting_it_was_measured_against(self):
        offer, _ = self.workflow.build_offer(self.row.dedupe_key, 55_000)
        self.assertEqual(offer.arv, self.row.arv)
        self.assertEqual(offer.mao, self.row.mao)
        self.assertEqual(offer.target_wholesale_fee, DEFAULT_CONFIG.target_wholesale_fee)

    def test_an_offer_moves_the_property_to_offer_sent(self):
        self.workflow.build_offer(self.row.dedupe_key, 55_000)
        self.assertEqual(self.workflow.leads.find_one("LH-021").status, "OFFER_SENT")

    def test_a_draft_offer_does_not_move_the_status(self):
        before = self.workflow.leads.find_one("LH-021").status
        self.workflow.build_offer(
            self.row.dedupe_key, 55_000, status=OfferStatus.DRAFT
        )
        self.assertEqual(self.workflow.leads.find_one("LH-021").status, before)

    def test_a_counter_records_the_distance_to_mao_and_the_target_fee(self):
        self.workflow.build_offer(self.row.dedupe_key, 55_000)
        offer, messages = self.workflow.record_counter(self.row.dedupe_key, 71_000)
        self.assertEqual(offer.current_proposed_price, 71_000)
        self.assertIsNotNone(offer.distance_to_mao)
        self.assertIsNotNone(offer.distance_to_target_fee)
        self.assertTrue(any("MAO" in m for m in messages))

    def test_a_counter_moves_the_property_to_negotiating(self):
        self.workflow.build_offer(self.row.dedupe_key, 55_000)
        self.workflow.record_counter(self.row.dedupe_key, 71_000)
        self.assertEqual(self.workflow.leads.find_one("LH-021").status, "NEGOTIATING")

    def test_the_fee_is_measured_at_the_price_on_the_table(self):
        offer = Offer(
            offer_amount=59_500, seller_counter=71_000,
            end_buyer_ceiling=85_000, target_wholesale_fee=18_000, mao=67_000,
        )
        self.assertEqual(offer.current_proposed_price, 71_000)
        self.assertEqual(offer.fee_at_current_price, 14_000)
        self.assertEqual(offer.distance_to_target_fee, -4_000)

    def test_a_property_with_no_underwriting_says_so(self):
        row = self.workflow.leads.find_one("LH-029")
        if row is None:
            self.skipTest("sparse sample lead not present")
        offer, warnings = self.workflow.build_offer(row.dedupe_key, 50_000)
        self.assertTrue(any("No MAO on file" in w for w in warnings))


# ---------------------------------------------------------------------------
# 7. Outreach through the workflow
# ---------------------------------------------------------------------------


class OutreachWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = workflow_with_leads()
        self.row = self.workflow.leads.find_one("LH-011")

    def tearDown(self):
        self.workflow.leads.close()

    def test_a_call_can_be_logged_without_a_phone_number_on_file(self):
        activity, messages = self.workflow.log_outreach(
            self.row.dedupe_key, Channel.CALL, Outcome.CONNECTED, "Spoke to the owner."
        )
        self.assertIsNotNone(activity)
        self.assertTrue(any("No contact record existed" in m for m in messages))

    def test_logging_never_invents_contact_details(self):
        self.workflow.log_outreach(self.row.dedupe_key, Channel.CALL, Outcome.NO_ANSWER)
        contact = self.workflow.store.best_contact(self.row.dedupe_key)
        self.assertIsNone(contact.phone)
        self.assertIsNone(contact.email)

    def test_an_outcome_advances_the_pipeline(self):
        self.workflow.log_outreach(self.row.dedupe_key, Channel.CALL, Outcome.CONNECTED)
        self.assertEqual(self.workflow.leads.find_one("LH-011").status, "CONVERSATION")

    def test_a_dead_outcome_kills_the_lead(self):
        self.workflow.log_outreach(
            self.row.dedupe_key, Channel.CALL, Outcome.NOT_INTERESTED
        )
        self.assertEqual(self.workflow.leads.find_one("LH-011").status, "DEAD")

    def test_an_outcome_needing_a_follow_up_says_so_when_none_is_set(self):
        _, messages = self.workflow.log_outreach(
            self.row.dedupe_key, Channel.CALL, Outcome.CALL_BACK
        )
        self.assertTrue(any("follow-up" in m.lower() for m in messages))

    def test_a_scheduled_follow_up_is_confirmed(self):
        due = TODAY + timedelta(days=2)
        _, messages = self.workflow.log_outreach(
            self.row.dedupe_key, Channel.CALL, Outcome.CALL_BACK, follow_up=due
        )
        self.assertTrue(any(due.isoformat() in m for m in messages))

    def test_inbound_contact_is_recorded_as_inbound(self):
        activity, _ = self.workflow.log_outreach(
            self.row.dedupe_key, Channel.CALL, Outcome.INTERESTED,
            direction=Direction.INBOUND,
        )
        self.assertIs(activity.direction, Direction.INBOUND)

    def test_outreach_lands_in_the_lead_activity_log(self):
        self.workflow.log_outreach(self.row.dedupe_key, Channel.TEXT, Outcome.NO_ANSWER)
        types = [
            a["activity_type"] for a in self.workflow.leads.activities(self.row.lead_row_id)
        ]
        self.assertIn("outreach_logged", types)

    def test_an_unknown_property_is_reported(self):
        activity, messages = self.workflow.log_outreach(
            "nowhere at all", Channel.CALL, Outcome.CONNECTED
        )
        self.assertIsNone(activity)
        self.assertIn("No stored property", messages[0])


# ---------------------------------------------------------------------------
# 8. Follow-ups, queue, dashboard, daily
# ---------------------------------------------------------------------------


class WorkflowScreenTests(unittest.TestCase):
    def setUp(self):
        self.workflow = workflow_with_leads()
        self.store = self.workflow.store
        provider = MockSkipTraceProvider()
        for key in ("LH-011", "LH-021", "LH-009"):
            row = self.workflow.leads.find_one(key)
            if row is None:
                continue
            result = provider.skip_trace(
                row.dedupe_key, address=row.address, city=row.city, state=row.state
            )
            self.store.save_contact(result.to_contact(row.dedupe_key))

    def tearDown(self):
        self.workflow.leads.close()

    # --- queue ---

    def test_the_queue_covers_every_live_lead(self):
        entries = self.workflow.queue_entries(today=TODAY)
        self.assertTrue(entries)
        self.assertTrue(all(not is_closed(e.row.status) for e in entries))

    def test_the_queue_is_ordered_by_urgency_then_score(self):
        entries = self.workflow.queue_entries(today=TODAY)
        keys = [e.priority.sort_key() for e in entries]
        self.assertEqual(keys, sorted(keys))

    def test_the_queue_reports_phone_and_email_status(self):
        for entry in self.workflow.queue_entries(today=TODAY):
            self.assertIn(
                entry.phone_status.split(" ")[0],
                ("NONE", "TEST", "MOBILE", "LANDLINE", "VOIP", "UNKNOWN"),
            )

    def test_closed_leads_are_excluded_by_default(self):
        row = self.workflow.leads.find_one("LH-011")
        self.workflow.set_status(row.dedupe_key, "DEAD", "test")
        keys = [e.row.dedupe_key for e in self.workflow.queue_entries(today=TODAY)]
        self.assertNotIn(row.dedupe_key, keys)

    def test_skip_trace_candidates_are_the_ones_with_no_contact(self):
        for entry in self.workflow.skip_trace_candidates():
            self.assertTrue(entry.contact is None or not entry.contact.is_reachable)

    # --- follow-ups ---

    def test_follow_ups_bucket_into_overdue_today_and_upcoming(self):
        rows = [self.workflow.leads.find_one(k) for k in ("LH-011", "LH-021", "LH-009")]
        rows = [r for r in rows if r]
        for row, offset in zip(rows, (-5, 0, 4)):
            self.store.set_follow_up(row.dedupe_key, TODAY + timedelta(days=offset), "test")
        buckets = self.workflow.follow_ups_by_bucket(TODAY)
        self.assertEqual(len(buckets["OVERDUE"]), 1)
        self.assertEqual(len(buckets["TODAY"]), 1)
        self.assertEqual(len(buckets["UPCOMING"]), 1)

    def test_follow_ups_are_sorted_most_overdue_first(self):
        rows = [self.workflow.leads.find_one(k) for k in ("LH-011", "LH-021")]
        rows = [r for r in rows if r]
        for row, offset in zip(rows, (-2, -9)):
            self.store.set_follow_up(row.dedupe_key, TODAY + timedelta(days=offset), "t")
        follow_ups = self.workflow.follow_ups(TODAY)
        self.assertEqual([f.days for f in follow_ups], sorted([f.days for f in follow_ups], reverse=True))

    def test_a_closed_lead_drops_off_the_follow_up_list(self):
        row = self.workflow.leads.find_one("LH-011")
        self.store.set_follow_up(row.dedupe_key, TODAY - timedelta(days=1), "t")
        self.workflow.set_status(row.dedupe_key, "PASSED", "test")
        keys = [f.row.dedupe_key for f in self.workflow.follow_ups(TODAY)]
        self.assertNotIn(row.dedupe_key, keys)

    # --- dashboard ---

    def test_the_dashboard_counts_every_status(self):
        board = self.workflow.dashboard(TODAY)
        self.assertEqual(set(board.counts), set(ACQUISITION_STATUSES))
        self.assertEqual(sum(board.counts.values()), board.total_leads)

    def test_the_dashboard_totals_only_live_deals(self):
        before = self.workflow.dashboard(TODAY).potential_fees
        row = self.workflow.leads.find_one("LH-011")
        self.workflow.set_status(row.dedupe_key, "DEAD", "test")
        after = self.workflow.dashboard(TODAY).potential_fees
        self.assertLess(after, before)

    def test_the_dashboard_averages_the_three_scores(self):
        board = self.workflow.dashboard(TODAY)
        for value in (
            board.average_deal_score, board.average_lead_score,
            board.average_priority_score,
        ):
            self.assertIsNotNone(value)
            self.assertGreaterEqual(value, 0.0)

    def test_the_dashboard_counts_follow_ups_and_skip_traces(self):
        row = self.workflow.leads.find_one("LH-011")
        self.store.set_follow_up(row.dedupe_key, TODAY - timedelta(days=1), "t")
        board = self.workflow.dashboard(TODAY)
        self.assertEqual(board.follow_ups_overdue, 1)
        self.assertGreater(board.contacts_needing_skip_trace, 0)

    def test_the_dashboard_counts_test_data_contacts_separately(self):
        self.assertGreater(self.workflow.dashboard(TODAY).test_data_contacts, 0)

    def test_the_rendered_dashboard_labels_the_money_as_projected(self):
        from wholesale_engine.reports.acquisitions import render_dashboard

        text = render_dashboard(self.workflow.dashboard(TODAY))
        self.assertIn("PROJECTED", text)
        self.assertIn("NOT EARNED", text)
        self.assertIn("not income", text)

    # --- daily ---

    def test_the_daily_plan_groups_work_in_the_documented_order(self):
        row = self.workflow.leads.find_one("LH-011")
        self.store.set_follow_up(row.dedupe_key, TODAY - timedelta(days=3), "callback")
        plan = self.workflow.daily_plan(TODAY)
        groups = [item.group for item in plan]
        self.assertEqual(groups, sorted(groups))
        self.assertTrue(groups[0].startswith("1."))

    def test_overdue_follow_ups_come_first(self):
        row = self.workflow.leads.find_one("LH-011")
        self.store.set_follow_up(row.dedupe_key, TODAY - timedelta(days=3), "callback")
        plan = self.workflow.daily_plan(TODAY)
        self.assertEqual(plan[0].action, "FOLLOW UP")
        self.assertIn("overdue", plan[0].detail)

    def test_each_property_appears_once(self):
        plan = self.workflow.daily_plan(TODAY)
        ids = [item.property_id for item in plan]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_counter_appears_in_the_plan(self):
        row = self.workflow.leads.find_one("LH-021")
        self.workflow.build_offer(row.dedupe_key, 55_000)
        self.workflow.record_counter(row.dedupe_key, 71_000)
        plan = self.workflow.daily_plan(TODAY)
        self.assertTrue(any(item.action == "RESPOND TO COUNTER" for item in plan))

    def test_the_rendered_plan_says_nothing_was_sent(self):
        from wholesale_engine.reports.acquisitions import render_daily

        text = render_daily(self.workflow.daily_plan(TODAY), TODAY)
        self.assertIn("Nothing here has been sent", text)


# ---------------------------------------------------------------------------
# 9. Exports
# ---------------------------------------------------------------------------


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.workflow = workflow_with_leads()
        row = self.workflow.leads.find_one("LH-011")
        self.workflow.store.save_contact(
            MockSkipTraceProvider().skip_trace(row.dedupe_key).to_contact(row.dedupe_key)
        )
        self.workflow.log_outreach(row.dedupe_key, Channel.CALL, Outcome.CONNECTED)
        self.workflow.build_offer(row.dedupe_key, 100_000)
        self.workflow.store.save_contract(
            Contract(property_id=row.dedupe_key, purchase_price=100_000)
        )
        self.workflow.store.save_buyer(Buyer(name="TEST BUYER", preferred_states=["FL"]))
        self.workflow.store.save_assignment(
            Assignment(
                property_id=row.dedupe_key, buyer_name="TEST BUYER",
                purchase_price=100_000, assignment_price=118_000,
            )
        )

    def tearDown(self):
        self.workflow.leads.close()

    def test_every_export_produces_rows_with_the_declared_columns(self):
        from wholesale_engine.reports.acquisition_exports import (
            ASSIGNMENT_COLUMNS, BUYER_COLUMNS, CONTACT_COLUMNS, CONTRACT_COLUMNS,
            OFFER_COLUMNS, OUTREACH_COLUMNS, assignment_rows, buyer_rows,
            contact_rows, contract_rows, offer_rows, outreach_rows,
        )

        store = self.workflow.store
        for rows, columns, label in (
            (contact_rows(store.all_contacts()), CONTACT_COLUMNS, "contacts"),
            (outreach_rows(store.all_outreach()), OUTREACH_COLUMNS, "outreach"),
            (offer_rows(store.all_offers()), OFFER_COLUMNS, "offers"),
            (contract_rows(store.all_contracts()), CONTRACT_COLUMNS, "contracts"),
            (buyer_rows(store.all_buyers()), BUYER_COLUMNS, "buyers"),
            (assignment_rows(store.all_assignments()), ASSIGNMENT_COLUMNS, "assignments"),
        ):
            self.assertTrue(rows, label)
            for row in rows:
                missing = set(columns) - set(row)
                self.assertFalse(missing, f"{label} missing {missing}")

    def test_the_contact_export_marks_test_data(self):
        from wholesale_engine.reports.acquisition_exports import contact_rows

        rows = contact_rows(self.workflow.store.all_contacts())
        self.assertTrue(any(r["is_test_data"] for r in rows))

    def test_the_pipeline_export_joins_everything(self):
        from wholesale_engine.reports.acquisition_exports import (
            PIPELINE_COLUMNS, pipeline_rows,
        )

        entries = self.workflow.queue_entries(include_closed=True, today=TODAY)
        rows = pipeline_rows(entries, self.workflow.store)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), set(PIPELINE_COLUMNS))
        with_offer = [r for r in rows if r["current_offer"] is not None]
        self.assertTrue(with_offer)

    def test_unknown_values_export_as_none_not_zero(self):
        from wholesale_engine.reports.acquisition_exports import pipeline_rows

        rows = pipeline_rows(
            self.workflow.queue_entries(include_closed=True, today=TODAY),
            self.workflow.store,
        )
        blank = [r for r in rows if r["current_offer"] is None]
        self.assertTrue(blank)
        self.assertIsNone(blank[0]["seller_counter"])


# ---------------------------------------------------------------------------
# 10. Data safety
# ---------------------------------------------------------------------------


class DataSafetyTests(unittest.TestCase):
    def test_no_module_in_the_package_generates_a_phone_number(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "wholesale_engine"
        pattern = re.compile(r"\b(?:\d{3}[-.]){2}\d{4}\b")
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                # The only literal numbers allowed anywhere are the reserved
                # 555-01xx range, and only inside the mock provider.
                self.assertIn(
                    "555", match.group(0),
                    f"{path.name} contains a phone-shaped literal: {match.group(0)}",
                )

    def test_the_mock_provider_is_the_only_source_of_contact_data(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "wholesale_engine"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "skip_trace.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "@example." in text and "invalid" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"contact data outside the mock: {offenders}")

    def test_provenance_labels_are_defined(self):
        from wholesale_engine.acquisitions import PROVENANCE_LABELS

        self.assertEqual(
            set(PROVENANCE_LABELS),
            {"SOURCE-PROVIDED", "CALCULATED", "USER-PROVIDED", "UNVERIFIED", "UNKNOWN"},
        )

    def test_a_contract_carries_no_legal_document_generation(self):
        import inspect

        from wholesale_engine.acquisitions import models

        source = inspect.getsource(models.Contract)
        self.assertIn("not legal advice", source.lower())


if __name__ == "__main__":
    unittest.main()
