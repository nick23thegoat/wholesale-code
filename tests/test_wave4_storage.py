"""Wave 4 — SQLite store, duplicate identity, change detection and outputs."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from wholesale_engine.hunt import run_hunt
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS
from wholesale_engine.outputs import (
    CsvAdapter,
    GoogleSheetsAdapter,
    JsonAdapter,
    publish_all,
)
from wholesale_engine.providers import CsvProvider, HuntCriteria
from wholesale_engine.reports.hunt_report import (
    DAILY_LEADS,
    DEALS_TO_REVIEW,
    HOT_LEADS,
    HUNT_COLUMNS,
    REJECTED_LEADS,
    write_hunt_outputs,
)
from wholesale_engine.storage import (
    LEAD_STATUSES,
    LeadSnapshot,
    STATUS_CONTACT,
    STATUS_HOT,
    STATUS_NEW,
    LeadStore,
    dedupe_key,
    detect_changes,
)
from wholesale_engine.storage.changes import (
    NEW_SIGNAL,
    PRICE_DROP,
    PRICE_INCREASE,
)


def lead(**kwargs) -> Lead:
    base = dict(
        lead_id="L1", address="123 Main St", city="Tampa", state="FL",
        zip_code="33601", asking_price=125_000, source="csv",
    )
    base.update(kwargs)
    return Lead(**base)


def read_rows(path: Path):
    """Read a CSV fully and close the handle."""
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_a_new_lead_starts_as_new(self):
        stored = self.store.upsert_lead(lead(), lead_score=70.0)
        self.assertEqual(stored.status, STATUS_NEW)
        self.assertEqual(stored.times_seen, 1)
        self.assertTrue(stored.is_new)

    def test_the_watchlist_statuses_all_exist(self):
        self.assertEqual(
            set(LEAD_STATUSES),
            {
                "NEW", "WATCH", "RESEARCHED", "HOT", "CONTACT", "OFFER_SENT",
                "UNDER_CONTRACT", "ASSIGNED", "CLOSED", "PASSED", "DEAD",
            },
        )

    def test_an_unknown_status_is_refused(self):
        stored = self.store.upsert_lead(lead())
        with self.assertRaises(ValueError):
            self.store.set_status(stored.lead_row_id, "MAYBE")

    def test_seeing_a_lead_again_updates_it_in_place(self):
        self.store.upsert_lead(lead(), lead_score=70.0)
        stored = self.store.upsert_lead(lead(asking_price=105_000), lead_score=82.0)
        self.assertEqual(stored.times_seen, 2)
        self.assertEqual(stored.asking_price, 105_000)
        self.assertEqual(self.store.count(), 1)

    def test_first_seen_never_moves(self):
        first = self.store.upsert_lead(lead(), seen_at=date(2026, 1, 1))
        again = self.store.upsert_lead(lead(), seen_at=date(2026, 6, 1))
        self.assertEqual(again.first_seen, first.first_seen)
        self.assertEqual(again.last_seen, "2026-06-01")

    def test_a_status_you_set_survives_the_next_sighting(self):
        stored = self.store.upsert_lead(lead())
        self.store.set_status(stored.lead_row_id, STATUS_CONTACT)
        again = self.store.upsert_lead(lead())
        self.assertEqual(again.status, STATUS_CONTACT)

    def test_identity_is_address_city_state_zip(self):
        key = dedupe_key(lead())
        self.assertIn("tampa", key)
        self.assertIn("fl", key)
        self.assertIn("33601", key)

    def test_address_variants_are_the_same_property(self):
        self.store.upsert_lead(lead(address="123 Main Street"))
        self.store.upsert_lead(lead(address="123 MAIN ST."))
        self.assertEqual(self.store.count(), 1)

    def test_different_units_are_different_properties(self):
        self.store.upsert_lead(lead(address="123 Main St #1"))
        self.store.upsert_lead(lead(address="123 Main St #2"))
        self.assertEqual(self.store.count(), 2)

    def test_a_different_city_is_a_different_property(self):
        self.store.upsert_lead(lead())
        self.store.upsert_lead(lead(city="Orlando"))
        self.assertEqual(self.store.count(), 2)

    def test_every_sighting_is_kept_in_history(self):
        self.store.upsert_lead(lead(), lead_score=70.0)
        stored = self.store.upsert_lead(lead(asking_price=99_000), lead_score=80.0)
        self.assertEqual(len(self.store.history(stored.lead_row_id)), 2)

    def test_a_later_sighting_backfills_blanks(self):
        self.store.upsert_lead(lead(sqft=None, county=""))
        self.store.upsert_lead(lead(sqft=1400, county="Hillsborough"))
        row = self.store.connection.execute(
            "SELECT sqft, county FROM properties"
        ).fetchone()
        self.assertEqual(row["sqft"], 1400)
        self.assertEqual(row["county"], "Hillsborough")

    def test_leads_can_be_listed_by_status(self):
        stored = self.store.upsert_lead(lead())
        self.store.set_status(stored.lead_row_id, STATUS_HOT)
        self.assertEqual(len(self.store.all_leads(status=STATUS_HOT)), 1)
        self.assertEqual(len(self.store.all_leads(status=STATUS_NEW)), 0)

    def test_the_database_file_is_created_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "leads.db"
            store = LeadStore(path)
            store.upsert_lead(lead())
            store.close()
            self.assertTrue(path.exists())


class ChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.store.upsert_lead(lead(), lead_score=61.0, deal_score=55.0)
        self.stored = self.store.get_for_lead(lead())

    def tearDown(self):
        self.store.close()

    def test_a_first_sighting_is_new_and_has_no_changes(self):
        changes = detect_changes(None, address="123 Main St")
        self.assertTrue(changes.is_new)
        self.assertFalse(changes.has_changes)
        self.assertEqual(changes.summary(), "NEW")

    def test_a_price_drop_is_detected_and_quantified(self):
        changes = detect_changes(self.stored, asking_price=105_000)
        drop = changes.of_kind(PRICE_DROP)[0]
        self.assertIn("$125,000 -> $105,000", drop.description)
        self.assertGreater(drop.priority, 0)

    def test_a_bigger_drop_earns_more_priority(self):
        small = detect_changes(self.stored, asking_price=120_000).priority_bump
        large = detect_changes(self.stored, asking_price=90_000).priority_bump
        self.assertGreater(large, small)

    def test_a_trivial_price_move_is_ignored(self):
        self.assertFalse(detect_changes(self.stored, asking_price=124_500).has_changes)

    def test_a_price_increase_is_reported_without_a_priority_bump(self):
        changes = detect_changes(self.stored, asking_price=150_000)
        self.assertTrue(changes.of_kind(PRICE_INCREASE))
        self.assertEqual(changes.priority_bump, 0)

    def test_a_new_distress_signal_is_detected(self):
        changes = detect_changes(self.stored, signals={"vacant": True})
        self.assertTrue(changes.of_kind(NEW_SIGNAL))
        self.assertIn("NEW VACANCY", changes.summary())

    def test_foreclosure_outranks_a_static_attribute(self):
        foreclosure = detect_changes(self.stored, signals={"pre_foreclosure": True})
        absentee = detect_changes(self.stored, signals={"absentee_owner": True})
        self.assertGreater(foreclosure.priority_bump, absentee.priority_bump)

    def test_a_signal_going_quiet_is_not_treated_as_a_fact(self):
        # Known -> unknown is a source that stopped reporting, not a change.
        store = LeadStore(":memory:")
        store.upsert_lead(lead(vacant=True))
        stored = store.get_for_lead(lead())
        self.assertFalse(detect_changes(stored, signals={"vacant": None}).has_changes)
        store.close()

    def test_score_movement_is_reported(self):
        changes = detect_changes(self.stored, lead_score=82.0, deal_score=70.0)
        self.assertIn("LEAD SCORE: 61 -> 82", changes.summary())
        self.assertIn("DEAL SCORE: 55 -> 70", changes.summary())

    def test_the_priority_bump_is_capped(self):
        changes = detect_changes(
            self.stored,
            asking_price=60_000,
            signals={name: True for name in (
                "vacant", "pre_foreclosure", "foreclosure", "tax_delinquent",
                "probate", "inherited", "code_violation", "absentee_owner",
                "high_equity", "tired_landlord",
            )},
            lead_score=99.0,
        )
        from wholesale_engine.storage.changes import MAX_PRIORITY_BUMP

        self.assertLessEqual(changes.priority_bump, MAX_PRIORITY_BUMP)

    def test_the_worked_example_from_the_spec(self):
        changes = detect_changes(
            self.stored, address="123 Main St", asking_price=105_000, lead_score=82.0
        )
        rendered = changes.render()
        self.assertIn("PRICE DROP:", rendered)
        self.assertIn("$125,000 -> $105,000", rendered)
        self.assertIn("LEAD SCORE: 61 -> 82", rendered)

    def test_changes_survive_a_real_hunt_across_two_runs(self):
        store = LeadStore(":memory:")
        provider = CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS)
        first = run_hunt(provider, HuntCriteria(), store=store)
        self.assertTrue(all(c.is_new for c in first.changes.values()))
        second = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store
        )
        self.assertFalse(any(c.is_new for c in second.changes.values()))
        store.close()


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = LeadStore(":memory:")
        cls.result = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            HuntCriteria(min_lead_score=60, min_deal_score=60),
            store=store,
        )
        store.close()

    def test_all_five_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = write_hunt_outputs(self.result, Path(tmp))
            for label in (DAILY_LEADS, HOT_LEADS, DEALS_TO_REVIEW, REJECTED_LEADS):
                self.assertTrue(written[label].exists(), label)
            self.assertTrue(written[DAILY_LEADS + ".json"].exists())

    def test_every_required_column_is_present(self):
        for column in (
            "lead_score", "deal_score", "data_confidence", "arv_confidence",
            "comp_confidence", "potential_wholesale_fee", "target_wholesale_fee",
            "deal_cushion", "mao", "recommended_offer", "final_decision",
            "risk_flags", "missing_data",
        ):
            self.assertIn(column, HUNT_COLUMNS)

    def test_lead_score_and_deal_score_are_separate_columns(self):
        self.assertIn("lead_score", HUNT_COLUMNS)
        self.assertIn("deal_score", HUNT_COLUMNS)
        self.assertNotEqual(
            HUNT_COLUMNS.index("lead_score"), HUNT_COLUMNS.index("deal_score")
        )

    def test_cushion_and_fee_are_separate_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_hunt_outputs(self.result, Path(tmp))[DAILY_LEADS]
            rows = [r for r in read_rows(path) if r["mao"]]
        row = rows[0]
        self.assertNotEqual(row["deal_cushion"], row["potential_wholesale_fee"])

    def test_the_json_output_carries_the_call_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_hunt_outputs(self.result, Path(tmp))[DAILY_LEADS + ".json"]
            document = json.loads(path.read_text())
        self.assertIn("provider_calls", document)
        self.assertIn("estimated_api_calls", document["provider_calls"])

    def test_rejected_leads_carry_their_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_hunt_outputs(self.result, Path(tmp))[REJECTED_LEADS]
            rows = read_rows(path)
        self.assertTrue(rows)
        self.assertTrue(any(r["needs_verification"] or r["missing_data"] for r in rows))

    def test_hot_leads_are_a_subset_of_daily_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = write_hunt_outputs(self.result, Path(tmp))
            daily = {r["address"] for r in read_rows(written[DAILY_LEADS])}
            hot = {r["address"] for r in read_rows(written[HOT_LEADS])}
        self.assertTrue(hot <= daily)

    def test_google_sheets_fails_loudly_rather_than_silently(self):
        with self.assertRaises(NotImplementedError):
            GoogleSheetsAdapter().publish([], [], "x")

    def test_an_unavailable_adapter_is_skipped_by_publish_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = publish_all(
                [CsvAdapter(Path(tmp)), GoogleSheetsAdapter()],
                [{"a": 1}], ["a"], "test",
            )
        self.assertEqual(len(written), 1)

    def test_json_and_csv_agree_on_row_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = write_hunt_outputs(self.result, Path(tmp))
            csv_rows = read_rows(written[DAILY_LEADS])
            document = json.loads(written[DAILY_LEADS + ".json"].read_text())
        self.assertEqual(len(csv_rows), document["count"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Expanded change detection (price drops, ARV, days on market, new listings)
# ---------------------------------------------------------------------------


class ExpandedChangeTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.store.upsert_lead(
            lead(days_on_market=90),
            lead_score=61.0,
            deal_score=55.0,
            snapshot=LeadSnapshot(arv=200_000, days_on_market=90),
        )
        self.stored = self.store.get_for_lead(lead())

    def tearDown(self):
        self.store.close()

    def test_a_first_sighting_is_reported_as_a_new_listing(self):
        from wholesale_engine.storage.changes import NEW_LISTING

        changes = detect_changes(None, address="1 A St", asking_price=125_000)
        self.assertTrue(changes.is_new)
        self.assertTrue(changes.of_kind(NEW_LISTING))

    def test_the_price_drop_amount_and_percentage_are_available(self):
        changes = detect_changes(self.stored, asking_price=105_000)
        self.assertTrue(changes.is_price_drop)
        self.assertEqual(changes.price_drop_amount, 20_000)
        self.assertAlmostEqual(changes.price_drop_percentage, 0.16, places=2)

    def test_the_worked_example_reads_exactly_as_specified(self):
        changes = detect_changes(self.stored, asking_price=99_000)
        self.assertEqual(changes.price_drop_amount, 26_000)
        self.assertIn("$125,000 -> $99,000", changes.summary())

    def test_no_drop_means_no_amount_rather_than_zero(self):
        changes = detect_changes(self.stored, asking_price=125_000)
        self.assertIsNone(changes.price_drop_amount)
        self.assertIsNone(changes.price_drop_percentage)

    def test_an_arv_change_is_detected(self):
        from wholesale_engine.storage.changes import ARV_CHANGE

        changes = detect_changes(self.stored, arv=230_000)
        self.assertTrue(changes.of_kind(ARV_CHANGE))

    def test_a_days_on_market_jump_is_detected_and_raises_priority(self):
        from wholesale_engine.storage.changes import DOM_CHANGE

        changes = detect_changes(self.stored, days_on_market=150)
        self.assertTrue(changes.of_kind(DOM_CHANGE))
        self.assertGreater(changes.priority_bump, 0)

    def test_a_trivial_days_on_market_move_is_ignored(self):
        self.assertFalse(detect_changes(self.stored, days_on_market=95).has_changes)

    def test_new_signals_are_listed_by_name(self):
        changes = detect_changes(self.stored, signals={"pre_foreclosure": True})
        self.assertIn("pre_foreclosure", changes.new_signals)
