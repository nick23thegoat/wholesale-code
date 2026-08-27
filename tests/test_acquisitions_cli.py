"""Wave 5 CLI: every new command, end to end against a real database."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from wholesale_engine.main import run


class AcquisitionCliTests(unittest.TestCase):
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
        self.base = ["--db", self.db, "--out-dir", str(self.tmp)]
        self._run(["--hunt", "--source", "csv", "--quiet"] + self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def cmd(self, *args, expect: int = 0) -> str:
        return self._run(list(args) + self.base, expect=expect)

    # --- screens -------------------------------------------------------

    def test_dashboard(self):
        output = self.cmd("--dashboard")
        self.assertIn("ACQUISITION DASHBOARD", output)
        self.assertIn("PROJECTED ECONOMICS", output)
        self.assertIn("NOT EARNED", output)

    def test_daily(self):
        # Wave 6 turned --daily into the full production run: the report plus
        # the ranked priority list.
        output = self.cmd("--daily")
        self.assertIn("DAILY ACQUISITIONS REPORT", output)
        self.assertIn("DAILY PRIORITY", output)
        self.assertIn("Nothing here has been sent", output)

    def test_contact_queue(self):
        output = self.cmd("--contact-queue", "--limit", "5")
        self.assertIn("CONTACT QUEUE", output)
        self.assertIn("NEXT ACTION", output)
        self.assertIn("SKIP TRACE", output)

    def test_follow_ups_with_nothing_scheduled(self):
        output = self.cmd("--follow-ups")
        self.assertIn("FOLLOW-UPS", output)
        self.assertIn("Nothing scheduled", output)

    def test_deal_room(self):
        output = self.cmd("--deal-room", "LH-011")
        for section in (
            "DEAL ROOM", "PROPERTY", "OWNER", "CONTACT", "SCORES", "DISTRESS",
            "EQUITY", "ECONOMICS", "CONTACT HISTORY", "FOLLOW-UP",
            "OFFER HISTORY", "CONTRACT STATUS", "BUYER STATUS", "RISK FLAGS",
            "MISSING DATA",
        ):
            self.assertIn(section, output, section)

    def test_deal_room_for_an_unknown_property_exits_non_zero(self):
        output = self.cmd("--deal-room", "nowhere at all", expect=1)
        self.assertIn("No stored property matches", output)

    # --- status --------------------------------------------------------

    def test_set_status(self):
        self.cmd("--property", "LH-011", "--set-status", "CONTACT_READY",
                 "--reason", "has a phone")
        output = self.cmd("--property", "LH-011")
        self.assertIn("CONTACT_READY", output)
        self.assertIn("has a phone", output)

    def test_an_invalid_status_is_refused(self):
        output = self.cmd("--property", "LH-011", "--set-status", "MAYBE", expect=2)
        self.assertIn("Unknown status", output)

    def test_every_pipeline_status_is_settable(self):
        for status in (
            "RESEARCHING", "HOT", "CONTACT_READY", "CONTACTED", "CONVERSATION",
            "FOLLOW_UP", "OFFER_PREPARING", "OFFER_SENT", "NEGOTIATING",
            "UNDER_CONTRACT", "BUYER_SEARCH", "ASSIGNED", "CLOSED",
        ):
            self.cmd("--property", "LH-011", "--set-status", status, "--quiet")

    # --- skip trace ----------------------------------------------------

    def test_skip_trace_refuses_without_a_provider(self):
        output = self.cmd("--skip-trace", "--property", "LH-011")
        self.assertIn("No skip-trace provider is connected", output)
        self.assertIn("never generate", output)

    def test_bulk_skip_tracing_asks_before_spending(self):
        output = self.cmd("--skip-trace", "--skip-trace-provider", "mock", expect=0)
        self.assertIn("qualify for a skip trace", output)
        self.assertIn("Cancelled", output)

    def test_skip_trace_with_the_mock_warns_it_is_fictional(self):
        output = self.cmd(
            "--skip-trace", "--skip-trace-provider", "mock", "--limit", "3", "--yes"
        )
        self.assertIn("FICTIONAL TEST DATA", output)
        self.assertIn("Do not dial", output)

    def test_a_mock_contact_shows_as_test_data_in_the_queue(self):
        self.cmd("--skip-trace", "--skip-trace-provider", "mock", "--limit", "5",
                 "--yes", "--quiet")
        output = self.cmd("--contact-queue", "--limit", "10")
        self.assertIn("TEST DATA", output)

    # --- outreach ------------------------------------------------------

    def test_log_call_with_an_outcome_and_a_follow_up(self):
        output = self.cmd(
            "--property", "LH-011", "--log-call", "--outcome", "CONNECTED",
            "--note", "Seller is motivated and wants to discuss price.",
            "--follow-up", "2026-08-24",
        )
        self.assertIn("Logged CALL", output)
        self.assertIn("Nothing was sent", output)
        self.assertIn("2026-08-24", output)

    def test_log_call_works_without_a_phone_number_on_file(self):
        output = self.cmd("--property", "LH-011", "--log-call", "--outcome", "NO_ANSWER")
        self.assertIn("Logged CALL", output)
        self.assertNotIn("Traceback", output)

    def test_log_text_and_email(self):
        self.assertIn(
            "Logged TEXT",
            self.cmd("--property", "LH-011", "--log-text", "--outcome", "NO_ANSWER"),
        )
        self.assertIn(
            "Logged EMAIL",
            self.cmd("--property", "LH-011", "--log-email", "--outcome", "NO_ANSWER"),
        )

    def test_log_note_records_without_a_channel_outcome(self):
        output = self.cmd("--property", "LH-011", "--log-note", "--note", "Drove by.")
        self.assertIn("Logged OTHER", output)

    def test_an_unknown_outcome_is_refused_with_the_valid_list(self):
        output = self.cmd(
            "--property", "LH-011", "--log-call", "--outcome", "MAYBE", expect=2
        )
        self.assertIn("unknown outcome", output)

    def test_a_bad_follow_up_date_is_refused(self):
        output = self.cmd(
            "--property", "LH-011", "--log-call", "--follow-up", "next tuesday", expect=2
        )
        self.assertIn("YYYY-MM-DD", output)

    def test_a_logged_outcome_shows_in_the_deal_room(self):
        self.cmd("--property", "LH-011", "--log-call", "--outcome", "CONNECTED",
                 "--note", "Good conversation.", "--quiet")
        output = self.cmd("--deal-room", "LH-011")
        self.assertIn("CONNECTED", output)
        self.assertIn("Good conversation.", output)

    def test_a_follow_up_appears_in_the_follow_ups_screen(self):
        self.cmd("--property", "LH-011", "--log-call", "--outcome", "CALL_BACK",
                 "--follow-up", "2020-01-01", "--quiet")
        output = self.cmd("--follow-ups")
        self.assertIn("OVERDUE", output)
        self.assertIn("2020-01-01", output)

    # --- offers --------------------------------------------------------

    def test_make_offer(self):
        output = self.cmd("--property", "LH-021", "--make-offer", "59500")
        self.assertIn("OFFER RECORDED", output)
        self.assertIn("$59,500", output)
        self.assertIn("Target wholesale fee", output)

    def test_an_offer_above_mao_warns_and_is_still_recorded(self):
        output = self.cmd("--property", "LH-021", "--make-offer", "200000")
        self.assertIn("OFFER EXCEEDS MAO", output)
        self.assertIn("not a block", output)
        self.assertIn("$200,000", self.cmd("--deal-room", "LH-021"))

    def test_a_below_target_offer_warns_without_rejecting(self):
        output = self.cmd("--property", "LH-021", "--make-offer", "72000")
        self.assertIn("BELOW TARGET", output)
        self.assertIn("still be worth doing", output)

    def test_a_counter_is_recorded_with_the_distances(self):
        self.cmd("--property", "LH-021", "--make-offer", "59500", "--quiet")
        output = self.cmd("--property", "LH-021", "--counter", "71000")
        self.assertIn("NEGOTIATION", output)
        self.assertIn("Seller counter", output)
        self.assertIn("Distance to MAO", output)
        self.assertIn("Distance to target fee", output)

    def test_a_counter_without_an_offer_says_so(self):
        output = self.cmd("--property", "LH-013", "--counter", "50000")
        self.assertIn("No offer on file", output)

    # --- contract, buyers, assignment ----------------------------------

    def test_record_a_contract(self):
        output = self.cmd(
            "--property", "LH-021", "--contract", "--purchase-price", "59500",
            "--closing-date", "2026-09-30", "--inspection-deadline", "2026-09-05",
            "--earnest-money", "2500", "--assignment-allowed", "yes",
        )
        self.assertIn("Contract recorded", output)
        self.assertIn("no legal advice", output)
        room = self.cmd("--deal-room", "LH-021")
        self.assertIn("2026-09-30", room)
        self.assertIn("UNDER_CONTRACT", room)

    def test_add_and_list_buyers(self):
        self.cmd("--add-buyer", "FICTIONAL BUYER ONE", "--buyer-company", "TEST LLC",
                 "--buyer-states", "MO,KS", "--buyer-min", "40000", "--buyer-max", "250000")
        output = self.cmd("--buyers")
        self.assertIn("FICTIONAL BUYER ONE", output)
        self.assertIn("MO, KS", output)

    def test_record_an_assignment_and_its_fee(self):
        self.cmd("--property", "LH-021", "--contract", "--purchase-price", "59500", "--quiet")
        self.cmd("--add-buyer", "FICTIONAL BUYER ONE", "--quiet")
        output = self.cmd(
            "--property", "LH-021", "--assign", "FICTIONAL BUYER ONE",
            "--assignment-price", "77500",
        )
        self.assertIn("ASSIGNMENT_SIGNED", output)
        self.assertIn("$18,000", output)

    def test_the_dashboard_reflects_the_work_done(self):
        self.cmd("--property", "LH-021", "--make-offer", "59500", "--quiet")
        output = self.cmd("--dashboard")
        self.assertIn("OFFER_SENT", output)
        self.assertIn("Offers open", output)

    # --- exports -------------------------------------------------------

    def test_every_acquisition_export_writes_csv_and_json(self):
        self.cmd("--skip-trace", "--skip-trace-provider", "mock", "--limit", "3",
                 "--yes", "--quiet")
        self.cmd("--property", "LH-011", "--log-call", "--outcome", "CONNECTED", "--quiet")
        self.cmd("--property", "LH-021", "--make-offer", "59500", "--quiet")
        self.cmd("--property", "LH-021", "--contract", "--purchase-price", "59500", "--quiet")
        self.cmd("--add-buyer", "TEST BUYER", "--quiet")
        self.cmd("--property", "LH-021", "--assign", "TEST BUYER",
                 "--assignment-price", "77500", "--quiet")
        self.cmd(
            "--export-contacts", "--export-outreach", "--export-follow-ups",
            "--export-offers", "--export-contracts", "--export-buyers",
            "--export-assignments", "--export-pipeline",
        )
        for name in (
            "contacts", "outreach", "follow_ups", "offers", "contracts",
            "buyers", "assignments", "acquisition_pipeline",
        ):
            self.assertTrue((self.tmp / f"{name}.csv").exists(), name)
            self.assertTrue((self.tmp / f"{name}.json").exists(), name)

    def test_the_contacts_export_marks_test_data(self):
        self.cmd("--skip-trace", "--skip-trace-provider", "mock", "--limit", "5",
                 "--yes", "--quiet")
        self.cmd("--export-contacts", "--format", "csv", "--quiet")
        with open(self.tmp / "contacts.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertIn("is_test_data", rows[0])
        self.assertTrue(any(r["is_test_data"] == "True" for r in rows))

    def test_the_pipeline_export_is_valid_json(self):
        self.cmd("--export-pipeline", "--format", "json", "--quiet")
        document = json.loads((self.tmp / "acquisition_pipeline.json").read_text())
        self.assertEqual(document["count"], len(document["rows"]))

    def test_the_wave_4_exports_still_work(self):
        self.cmd("--export-hot", "--export-top-deals", "--export-watchlist", "--quiet")
        for name in ("hot_leads_export", "top_deals", "watchlist"):
            self.assertTrue((self.tmp / f"{name}.csv").exists(), name)

    # --- earlier waves -------------------------------------------------

    def test_the_earlier_commands_all_still_run(self):
        self._run(["--sample", "--quiet"])
        self._run(["--sample-leads", "--quiet"])
        self._run(["--list-sources"])
        self.cmd("--top-deals", "--limit", "20")
        self.cmd("--hot-leads")
        self.cmd("--search", "--states", "MO", "--min-lead-score", "70")
        self.cmd("--property", "LH-011")
        self.cmd("--watchlist")


if __name__ == "__main__":
    unittest.main()
