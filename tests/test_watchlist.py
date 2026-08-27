"""Watchlist, notes, activity history, search, and the new CLI commands."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.hunt import run_hunt
from wholesale_engine.lead_hunter.models import Lead
from wholesale_engine.main import SAMPLE_LEAD_COMPS, SAMPLE_LEADS, run
from wholesale_engine.providers import CsvProvider, HuntCriteria
from wholesale_engine.reports.deal_tables import DEAL_COLUMNS, deal_rows, render_deal_table
from wholesale_engine.reports.dossier import render_dossier
from wholesale_engine.storage import (
    ACTIVE_STATUSES,
    ACTIVITY_NOTE_ADDED,
    ACTIVITY_STATUS_CHANGED,
    CLOSED_STATUSES,
    LEAD_STATUSES,
    SORT_KEYS,
    LeadSnapshot,
    LeadStore,
    SearchQuery,
    STATUS_ASSIGNED,
    STATUS_CONTACTED,
    STATUS_CONTACT_READY,
    STATUS_CONVERSATION,
    STATUS_DEAD,
    STATUS_NEGOTIATING,
    STATUS_RESEARCHING,
    STATUS_BUYER_SEARCH,
    STATUS_HOT,
    STATUS_OFFER_SENT,
    STATUS_PASSED,
    STATUS_UNDER_CONTRACT,
    STATUS_WATCH,
    dedupe_key,
)


def lead(**kwargs) -> Lead:
    base = dict(
        lead_id="L1", address="123 Main St", city="Tampa", state="FL",
        county="Hillsborough", zip_code="33601", asking_price=120_000, source="csv",
    )
    base.update(kwargs)
    return Lead(**base)


def populated_store() -> LeadStore:
    store = LeadStore(":memory:")
    run_hunt(CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=store)
    return store


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.row = self.store.upsert_lead(lead(), lead_score=80.0)

    def tearDown(self):
        self.store.close()

    def test_the_full_status_vocabulary(self):
        for name in (
            "NEW", "RESEARCHING", "HOT", "CONTACT_READY", "CONTACTED",
            "CONVERSATION", "FOLLOW_UP", "OFFER_PREPARING", "OFFER_SENT",
            "NEGOTIATING", "UNDER_CONTRACT", "BUYER_SEARCH", "ASSIGNED",
            "CLOSED", "DEAD", "PASSED",
        ):
            self.assertIn(name, LEAD_STATUSES)

    def test_a_lead_can_walk_the_whole_pipeline(self):
        path = [
            STATUS_RESEARCHING, STATUS_HOT, STATUS_CONTACT_READY,
            STATUS_CONTACTED, STATUS_CONVERSATION, STATUS_OFFER_SENT,
            STATUS_NEGOTIATING, STATUS_UNDER_CONTRACT, STATUS_BUYER_SEARCH,
            STATUS_ASSIGNED,
        ]
        for status in path:
            self.store.set_status(self.row.lead_row_id, status)
        history = self.store.status_history(self.row.lead_row_id)
        self.assertEqual([h["to_status"] for h in history], ["NEW"] + path)

    def test_each_move_records_where_it_came_from(self):
        self.store.set_status(self.row.lead_row_id, STATUS_HOT, "verified ARV")
        last = self.store.status_history(self.row.lead_row_id)[-1]
        self.assertEqual(last["from_status"], "NEW")
        self.assertEqual(last["to_status"], "HOT")
        self.assertEqual(last["reason"], "verified ARV")

    def test_a_no_op_move_is_not_recorded(self):
        before = len(self.store.status_history(self.row.lead_row_id))
        self.store.set_status(self.row.lead_row_id, "NEW")
        self.assertEqual(len(self.store.status_history(self.row.lead_row_id)), before)

    def test_a_lead_can_move_backwards(self):
        self.store.set_status(self.row.lead_row_id, STATUS_UNDER_CONTRACT)
        self.store.set_status(self.row.lead_row_id, STATUS_DEAD, "seller relisted")
        self.assertEqual(self.store.find_one("L1").status, STATUS_DEAD)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.set_status(self.row.lead_row_id, "MAYBE")

    def test_the_watchlist_shows_only_active_leads(self):
        self.store.set_status(self.row.lead_row_id, STATUS_CONTACTED)
        other = self.store.upsert_lead(lead(lead_id="L2", address="9 Elm St"))
        self.store.set_status(other.lead_row_id, STATUS_PASSED)
        addresses = [r.address for r in self.store.watchlist()]
        self.assertIn("123 Main St", addresses)
        self.assertNotIn("9 Elm St", addresses)

    def test_closed_and_active_statuses_do_not_overlap(self):
        self.assertFalse(set(ACTIVE_STATUSES) & set(CLOSED_STATUSES))

    def test_status_counts_cover_every_status(self):
        counts = self.store.status_counts()
        self.assertEqual(set(counts), set(LEAD_STATUSES))
        self.assertEqual(counts["NEW"], 1)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.row = self.store.upsert_lead(lead())

    def tearDown(self):
        self.store.close()

    def test_a_note_is_stored_and_read_back(self):
        self.store.add_note(self.row.lead_row_id, "Seller wants quick close.")
        notes = self.store.notes(self.row.lead_row_id)
        self.assertEqual(notes[0]["body"], "Seller wants quick close.")

    def test_notes_keep_their_order(self):
        for body in ("Roof appears newer.", "Needs kitchen remodel.", "Called seller 8/22."):
            self.store.add_note(self.row.lead_row_id, body)
        bodies = [n["body"] for n in self.store.notes(self.row.lead_row_id)]
        self.assertEqual(bodies[0], "Roof appears newer.")
        self.assertEqual(bodies[-1], "Called seller 8/22.")

    def test_an_author_can_be_recorded(self):
        self.store.add_note(self.row.lead_row_id, "Buyer interested.", author="nick")
        self.assertEqual(self.store.notes(self.row.lead_row_id)[0]["author"], "nick")

    def test_an_empty_note_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.add_note(self.row.lead_row_id, "   ")

    def test_a_note_can_be_deleted(self):
        note_id = self.store.add_note(self.row.lead_row_id, "wrong property")
        self.assertTrue(self.store.delete_note(note_id))
        self.assertEqual(self.store.notes(self.row.lead_row_id), [])

    def test_the_engine_writes_no_notes_of_its_own(self):
        # Notes are the user's. A freshly hunted lead must have none.
        store = populated_store()
        for row in store.all_leads():
            self.assertEqual(store.notes(row.lead_row_id), [], row.address)
        store.close()


# ---------------------------------------------------------------------------
# Activity history
# ---------------------------------------------------------------------------


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_creating_a_lead_is_logged(self):
        row = self.store.upsert_lead(lead())
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn("lead_created", types)

    def test_seeing_a_lead_again_is_logged(self):
        row = self.store.upsert_lead(lead())
        self.store.upsert_lead(lead())
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn("lead_updated", types)

    def test_a_price_change_is_logged_as_such(self):
        row = self.store.upsert_lead(lead())
        self.store.upsert_lead(
            lead(asking_price=99_000), change_summary="PRICE DROP: $120,000 -> $99,000"
        )
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn("price_changed", types)

    def test_a_status_change_is_logged(self):
        row = self.store.upsert_lead(lead())
        self.store.set_status(row.lead_row_id, STATUS_HOT)
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn(ACTIVITY_STATUS_CHANGED, types)

    def test_a_note_is_logged(self):
        row = self.store.upsert_lead(lead())
        self.store.add_note(row.lead_row_id, "Called seller.")
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn(ACTIVITY_NOTE_ADDED, types)

    def test_research_and_offer_calculation_are_logged(self):
        row = self.store.upsert_lead(
            lead(),
            snapshot=LeadSnapshot(
                researched=True, research_note="8 fields known",
                mao=90_000, recommended_offer=82_000, potential_fee=19_000,
                fee_status="MEETS TARGET",
            ),
        )
        types = [a["activity_type"] for a in self.store.activities(row.lead_row_id)]
        self.assertIn("research_completed", types)
        self.assertIn("offer_calculated", types)

    def test_every_activity_has_a_timestamp_and_a_description(self):
        row = self.store.upsert_lead(lead())
        for entry in self.store.activities(row.lead_row_id):
            self.assertTrue(entry["created_at"])
            self.assertTrue(entry["activity_type"])
            self.assertIsNotNone(entry["description"])

    def test_activity_is_newest_first(self):
        row = self.store.upsert_lead(lead())
        self.store.add_note(row.lead_row_id, "later")
        entries = self.store.activities(row.lead_row_id)
        self.assertEqual(entries[0]["activity_type"], ACTIVITY_NOTE_ADDED)

    def test_an_unknown_activity_type_is_refused(self):
        row = self.store.upsert_lead(lead())
        with self.assertRaises(ValueError):
            self.store.log_activity(row.lead_row_id, "danced", "nope")

    def test_the_global_log_spans_every_lead(self):
        self.store.upsert_lead(lead())
        self.store.upsert_lead(lead(lead_id="L2", address="9 Elm St"))
        self.assertGreaterEqual(len(self.store.recent_activity()), 2)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = populated_store()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_an_empty_query_returns_everything(self):
        self.assertEqual(len(self.store.search()), self.store.count())

    def test_state_filter(self):
        for row in self.store.search(SearchQuery(states=("MO",))):
            self.assertEqual(row.state.upper(), "MO")

    def test_county_and_city_filters(self):
        rows = self.store.search(SearchQuery(counties=("Greene",)))
        self.assertTrue(all(r.county.lower() == "greene" for r in rows))
        rows = self.store.search(SearchQuery(cities=("Tampa",)))
        self.assertTrue(all(r.city.lower() == "tampa" for r in rows))

    def test_zip_filter(self):
        rows = self.store.search(SearchQuery(zip_codes=("65804",)))
        self.assertTrue(all(r.zip_code == "65804" for r in rows))

    def test_price_band(self):
        rows = self.store.search(SearchQuery(min_price=60_000, max_price=150_000))
        for row in rows:
            self.assertGreaterEqual(row.asking_price, 60_000)
            self.assertLessEqual(row.asking_price, 150_000)

    def test_arv_band(self):
        rows = self.store.search(SearchQuery(min_arv=200_000))
        self.assertTrue(all(r.arv >= 200_000 for r in rows))

    def test_score_filters(self):
        rows = self.store.search(SearchQuery(min_lead_score=70, min_deal_score=60))
        for row in rows:
            self.assertGreaterEqual(row.lead_score, 70)
            self.assertGreaterEqual(row.deal_score, 60)

    def test_priority_score_filter(self):
        rows = self.store.search(SearchQuery(min_priority_score=60))
        self.assertTrue(all(r.priority_score >= 60 for r in rows))

    def test_fee_filter(self):
        rows = self.store.search(SearchQuery(min_fee=15_000))
        self.assertTrue(all(r.potential_fee >= 15_000 for r in rows))

    def test_signal_filters(self):
        rows = self.store.search(SearchQuery(vacant=True))
        self.assertTrue(rows)
        self.assertTrue(all(r.signal("vacant") is True for r in rows))

    def test_several_signals_are_and_ed(self):
        rows = self.store.search(SearchQuery(vacant=True, probate=True))
        for row in rows:
            self.assertTrue(row.signal("vacant"))
            self.assertTrue(row.signal("probate"))

    def test_days_on_market_filters(self):
        rows = self.store.search(SearchQuery(min_days_on_market=180))
        self.assertTrue(all(r.days_on_market >= 180 for r in rows))

    def test_property_type_filter(self):
        rows = self.store.search(SearchQuery(property_types=("duplex",)))
        self.assertTrue(all("DUPLEX" in r.property_type.upper() for r in rows))

    def test_text_search_matches_the_address(self):
        rows = self.store.search(SearchQuery(text="Sabal"))
        self.assertTrue(any("Sabal" in r.address for r in rows))

    def test_limit_is_applied(self):
        self.assertLessEqual(len(self.store.search(SearchQuery(limit=3))), 3)

    def test_results_are_sorted_best_first(self):
        rows = self.store.search(SearchQuery(min_priority_score=0))
        scores = [r.priority_score for r in rows if r.priority_score is not None]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_every_sort_key_works(self):
        for key in SORT_KEYS:
            self.assertIsInstance(self.store.search(SearchQuery(sort_by=key)), list)

    def test_an_unknown_sort_key_falls_back_rather_than_raising(self):
        self.assertIsInstance(self.store.search(SearchQuery(sort_by="nonsense")), list)

    def test_filters_combine(self):
        rows = self.store.search(SearchQuery(states=("MO",), min_lead_score=70))
        for row in rows:
            self.assertEqual(row.state.upper(), "MO")
            self.assertGreaterEqual(row.lead_score, 70)

    def test_find_one_matches_a_lead_id_or_an_address_fragment(self):
        self.assertIsNotNone(self.store.find_one("LH-011"))
        self.assertIsNotNone(self.store.find_one("Sabal"))
        self.assertIsNone(self.store.find_one("no such property anywhere"))


class RankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = populated_store()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_top_deals_are_analyzed_and_ranked_by_priority(self):
        rows = self.store.top_deals(10)
        self.assertTrue(rows)
        self.assertTrue(all(r.deal_score is not None for r in rows))
        scores = [r.priority_score for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_deals_respects_its_limit(self):
        self.assertLessEqual(len(self.store.top_deals(3)), 3)

    def test_hot_leads_are_sorted_by_the_documented_order(self):
        rows = self.store.hot_leads()
        keys = [
            (
                -(r.priority_score or -1), -(r.deal_score or -1),
                -(r.lead_score or -1), -(r.potential_fee or -1e12),
            )
            for r in rows
        ]
        self.assertEqual(keys, sorted(keys))

    def test_hot_leads_exclude_leads_you_have_killed(self):
        row = self.store.hot_leads()[0]
        self.store.set_status(row.lead_row_id, STATUS_DEAD)
        self.assertNotIn(row.address, [r.address for r in self.store.hot_leads()])
        self.store.set_status(row.lead_row_id, STATUS_HOT)

    def test_a_below_target_fee_can_still_be_a_hot_lead(self):
        below = [
            r for r in self.store.hot_leads()
            if r.potential_fee is not None and r.potential_fee < 18_000
        ]
        self.assertTrue(below, "a below-target fee must not disqualify a hot lead")

    def test_the_deal_table_renders_every_required_column(self):
        text = render_deal_table(self.store.top_deals(5), "TOP DEALS")
        for header in ("ADDRESS", "LEAD", "DEAL", "PRIO", "ARV", "MAO", "OFFER", "FEE"):
            self.assertIn(header, text)

    def test_the_export_row_carries_every_column(self):
        rows = deal_rows(self.store.top_deals(3))
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), set(DEAL_COLUMNS))

    def test_export_rows_are_ranked_from_one(self):
        rows = deal_rows(self.store.top_deals(3))
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])

    def test_an_empty_table_says_so_rather_than_erroring(self):
        self.assertIn("Nothing matched", render_deal_table([], "TOP DEALS"))


# ---------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------


class DossierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = LeadStore(":memory:")
        cls.hunt = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            HuntCriteria(min_lead_score=60),
            store=cls.store,
        )
        cls.entry = cls.hunt.prioritized[0]
        cls.key = dedupe_key(cls.entry.lead)
        cls.stored = cls.store.get_for_lead(cls.entry.lead)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def dossier(self, **overrides):
        kwargs = dict(
            result=self.entry,
            research=self.hunt.research.get(self.key),
            priority=self.hunt.priorities.get(self.key),
            stored=self.stored,
            changes=self.hunt.changes.get(self.key),
            notes=self.store.notes(self.stored.lead_row_id),
            activities=self.store.activities(self.stored.lead_row_id),
            status_history=self.store.status_history(self.stored.lead_row_id),
        )
        kwargs.update(overrides)
        return render_dossier(**kwargs)

    def test_every_required_section_is_present(self):
        text = self.dossier()
        for section in (
            "PROPERTY", "OWNER", "DISTRESS", "EQUITY", "VALUATION", "COMPS",
            "REPAIRS", "MAO AND OFFER", "WHOLESALE ECONOMICS", "SCORES",
            "FINAL DECISION", "RISK FLAGS", "MISSING DATA", "STATUS",
            "ACTIVITY HISTORY", "NOTES",
        ):
            self.assertIn(section, text, section)

    def test_all_three_scores_are_shown_and_named(self):
        text = self.dossier()
        self.assertIn("LEAD SCORE:", text)
        self.assertIn("DEAL SCORE:", text)
        self.assertIn("PRIORITY SCORE:", text)

    def test_the_fee_is_shown_with_the_price_it_was_measured_at(self):
        text = self.dossier()
        self.assertIn("Target Wholesale Fee:", text)
        self.assertIn("at asking", text)
        self.assertIn("Fee Status:", text)

    def test_it_states_the_target_is_not_a_rejection(self):
        self.assertIn("never rejects a deal on its own", self.dossier())

    def test_it_refuses_to_imply_contact_data_exists(self):
        text = self.dossier()
        self.assertIn("never generate contact details", text)

    def test_unknowns_are_shown_as_unknown(self):
        self.assertIn("unknown", self.dossier())

    def test_it_renders_with_nothing_but_a_stored_row(self):
        text = render_dossier(stored=self.stored)
        self.assertIn("PROPERTY DOSSIER", text)
        self.assertIn("STATUS", text)

    def test_it_renders_with_no_arguments_at_all(self):
        self.assertIn("PROPERTY DOSSIER", render_dossier())

    def test_notes_appear_when_present(self):
        self.store.add_note(self.stored.lead_row_id, "Seller wants quick close.")
        text = self.dossier(notes=self.store.notes(self.stored.lead_row_id))
        self.assertIn("Seller wants quick close.", text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def _run(self, argv, expect: int = 0) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = run(argv)
        self.assertEqual(code, expect, buffer.getvalue())
        return buffer.getvalue()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "leads.db")
        self._run(["--hunt", "--source", "csv", "--db", self.db,
                   "--out-dir", str(self.tmp), "--quiet"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_top_deals(self):
        output = self._run(["--top-deals", "--db", self.db, "--limit", "5"])
        self.assertIn("TOP DEALS", output)
        self.assertIn("PRIO", output)

    def test_hot_leads(self):
        output = self._run(["--hot-leads", "--db", self.db])
        self.assertIn("HOT LEADS", output)

    def test_search_by_state_and_lead_score(self):
        output = self._run(
            ["--search", "--db", self.db, "--states", "MO", "--min-lead-score", "70"]
        )
        self.assertIn("SEARCH RESULTS", output)
        self.assertIn("states: MO", output)

    def test_search_by_signal(self):
        output = self._run(["--search", "--db", self.db, "--vacant"])
        self.assertIn("vacant=yes", output)

    def test_watchlist(self):
        self.assertIn("DEAL WATCHLIST", self._run(["--watchlist", "--db", self.db]))

    def test_property_dossier(self):
        output = self._run(["--property", "LH-011", "--db", self.db])
        self.assertIn("PROPERTY DOSSIER", output)
        self.assertIn("PRIORITY SCORE:", output)

    def test_an_unknown_property_is_reported_not_invented(self):
        output = self._run(["--property", "nowhere at all", "--db", self.db], expect=1)
        self.assertIn("No stored property matches", output)

    def test_set_status_and_note_through_the_cli(self):
        self._run([
            "--property", "LH-011", "--db", self.db,
            "--set-status", "HOT", "--reason", "verified ARV",
            "--note", "Called seller 8/22.",
        ])
        output = self._run(["--property", "LH-011", "--db", self.db])
        self.assertIn("Called seller 8/22.", output)
        self.assertIn("NEW -> HOT", output)

    def test_an_invalid_status_is_refused(self):
        output = self._run(
            ["--property", "LH-011", "--db", self.db, "--set-status", "MAYBE"], expect=2
        )
        self.assertIn("Unknown status", output)

    def test_the_activity_log(self):
        output = self._run(["--activity", "--db", self.db, "--limit", "10"])
        self.assertIn("lead_created", output)

    def test_exports_write_csv_and_json(self):
        self._run([
            "--export-hot", "--export-top-deals", "--export-watchlist",
            "--db", self.db, "--out-dir", str(self.tmp),
        ])
        for name in ("hot_leads_export", "top_deals", "watchlist"):
            self.assertTrue((self.tmp / f"{name}.csv").exists(), name)
            self.assertTrue((self.tmp / f"{name}.json").exists(), name)

    def test_csv_only_export(self):
        self._run([
            "--export-top-deals", "--format", "csv",
            "--db", self.db, "--out-dir", str(self.tmp),
        ])
        self.assertTrue((self.tmp / "top_deals.csv").exists())

    def test_the_exported_csv_carries_all_three_scores(self):
        self._run([
            "--export-top-deals", "--db", self.db, "--out-dir", str(self.tmp), "--quiet",
        ])
        with open(self.tmp / "top_deals.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        for column in ("lead_score", "deal_score", "priority_score", "potential_fee"):
            self.assertIn(column, rows[0])

    def test_the_exported_json_is_valid(self):
        self._run([
            "--export-hot", "--format", "json",
            "--db", self.db, "--out-dir", str(self.tmp), "--quiet",
        ])
        document = json.loads((self.tmp / "hot_leads_export.json").read_text())
        self.assertIn("rows", document)
        self.assertEqual(document["count"], len(document["rows"]))

    def test_the_earlier_waves_still_run(self):
        self._run(["--sample", "--quiet"])
        self._run(["--sample-leads", "--quiet"])
        self.assertIn("csv", self._run(["--list-sources"]))


if __name__ == "__main__":
    unittest.main()


class DecisionMatchingTests(unittest.TestCase):
    """'GO' is a substring of 'NEGOTIATE'. Decisions must match exactly."""

    def setUp(self):
        self.store = LeadStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_a_negotiate_decision_is_not_treated_as_a_go(self):
        row = self.store.upsert_lead(
            lead(), lead_score=52.0, deal_score=70.0,
            final_decision="🟠 NEGOTIATE",
            snapshot=LeadSnapshot(priority_score=52.0, priority_band="🟡 REVIEW"),
        )
        self.assertNotIn(
            row.address, [r.address for r in self.store.hot_leads()],
            "a NEGOTIATE decision must not reach the hot-lead call list",
        )

    def test_a_go_decision_is_a_hot_lead(self):
        row = self.store.upsert_lead(
            lead(lead_id="L9", address="9 Go Lane"),
            lead_score=52.0, deal_score=80.0, final_decision="🔥 GO",
            snapshot=LeadSnapshot(priority_score=52.0, priority_band="🟡 REVIEW"),
        )
        self.assertIn(row.address, [r.address for r in self.store.hot_leads()])

    def test_a_negotiate_lead_does_not_land_in_the_hot_leads_export(self):
        from wholesale_engine.reports.hunt_report import HOT_LEADS, split_outputs

        hunt = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS), HuntCriteria(), store=self.store
        )
        rows = {
            id(r): {"address": r.lead.address} for r in hunt.prioritized
        }
        datasets = split_outputs(hunt.prioritized, rows)
        hot_addresses = {row["address"] for row in datasets[HOT_LEADS]}
        for entry in hunt.prioritized:
            if entry.analysis and "NEGOTIATE" in str(entry.analysis.decision):
                self.assertNotIn(entry.lead.address, hot_addresses)
