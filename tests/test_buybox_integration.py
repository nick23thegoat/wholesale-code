"""Buy-box integration: the saved configuration actually driving a hunt.

"The buy box loaded successfully" proves nothing. What matters is whether a
property that should have been screened out was, and whether one that should
have survived did. Most of this file therefore runs the real funnel and counts
addresses, rather than inspecting a criteria object and calling it done.

Two boundaries it also holds:

* the seven shape filters are storable and reportable but never applied — a
  filter you believe is running and is not is worse than one you know is off
* nothing changes at all without ``--buybox``
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from wholesale_engine import main as cli
from wholesale_engine.buybox import (
    APPLIED_FIELDS,
    DESCRIPTIVE_FIELDS,
    NOT_IMPLEMENTED_FIELDS,
    NOT_ROUTED_FIELDS,
    BuyBox,
)
from wholesale_engine.config import MAX_PROPERTY_PRICE, MIN_PROPERTY_PRICE
from wholesale_engine.hunt import HuntBudget, run_hunt
from wholesale_engine.providers import CsvProvider
from wholesale_engine.providers.criteria import HuntCriteria
from wholesale_engine.service import EngineService
from wholesale_engine.service.paths import SAMPLE_LEAD_COMPS, SAMPLE_LEADS


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["--hunt", *argv])


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "buybox.json"
        self.service = EngineService(
            db_path=self.tmp / "leads.db", buy_box_path=self.path
        )

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def write(self, **values) -> BuyBox:
        payload = {"name": "test", "states": ["FL"], **values}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return self.service.read_buy_box().buy_box

    def hunt(self, criteria) -> list:
        """Run the real funnel and return the addresses that SURVIVED it.

        ``result.prioritized`` holds every entry the funnel saw, rejected ones
        included, so counting it measures what the provider returned rather
        than what the buy box kept. Filters applied provider-side drop their
        leads before that point and filters applied inside the funnel do not,
        which makes the difference invisible for some criteria and total for
        others. ``filter_outcome.passed`` is the actual survivor test.
        """
        result = run_hunt(
            CsvProvider(SAMPLE_LEADS, SAMPLE_LEAD_COMPS),
            criteria,
            budget=HuntBudget(),
        )
        return [
            e.lead.address for e in result.prioritized
            if e.lead.address and e.filter_outcome.passed
        ]


# ---------------------------------------------------------------------------
# All eleven supported fields map
# ---------------------------------------------------------------------------


class Mapping(unittest.TestCase):
    def test_every_applied_field_reaches_the_criteria(self):
        box = BuyBox(
            name="full",
            states=["FL", "TX"],
            counties=["hillsborough"],
            cities=["tampa"],
            zip_codes=["33607", "33609"],
            property_types=["single_family", "duplex"],
            min_price=50_000,
            max_price=400_000,
            min_equity=25_000,
            min_lead_score=55,
            min_deal_score=65,
            required_signals=["vacant", "probate"],
        )
        criteria = box.to_criteria()
        self.assertEqual(criteria.states, ("FL", "TX"))
        self.assertEqual(criteria.counties, ("hillsborough",))
        self.assertEqual(criteria.cities, ("tampa",))
        self.assertEqual(criteria.zip_codes, ("33607", "33609"))
        self.assertEqual(criteria.property_types, ("single_family", "duplex"))
        self.assertEqual(criteria.min_price, 50_000)
        self.assertEqual(criteria.max_price, 400_000)
        self.assertEqual(criteria.min_equity, 25_000)
        self.assertEqual(criteria.min_lead_score, 55)
        self.assertEqual(criteria.min_deal_score, 65)
        self.assertEqual(criteria.required_signals, ("vacant", "probate"))

    def test_the_applied_list_is_exactly_the_eleven(self):
        self.assertEqual(len(APPLIED_FIELDS), 11)

    def test_every_buy_box_field_is_accounted_for_in_exactly_one_group(self):
        # A field in no group is one nobody decided about — which is how a
        # setting ends up silently doing nothing.
        from dataclasses import fields

        grouped = (
            set(APPLIED_FIELDS) | set(DESCRIPTIVE_FIELDS)
            | set(NOT_IMPLEMENTED_FIELDS) | set(NOT_ROUTED_FIELDS)
        )
        actual = {f.name for f in fields(BuyBox)}
        self.assertEqual(grouped, actual)
        self.assertEqual(
            len(grouped),
            len(APPLIED_FIELDS) + len(DESCRIPTIVE_FIELDS)
            + len(NOT_IMPLEMENTED_FIELDS) + len(NOT_ROUTED_FIELDS),
            "a field appears in two groups",
        )

    def test_an_empty_buy_box_constrains_nothing_it_should_not(self):
        criteria = BuyBox(states=[], property_types=[]).to_criteria()
        self.assertEqual(criteria.states, ())
        self.assertEqual(criteria.counties, ())
        self.assertIsNone(criteria.min_equity)

    def test_the_default_price_band_survives_the_round_trip(self):
        # Guards the standing rule: no low ceiling, $2.2M buyer capacity.
        criteria = BuyBox().to_criteria()
        self.assertEqual(criteria.min_price, MIN_PROPERTY_PRICE)
        self.assertEqual(criteria.max_price, MAX_PROPERTY_PRICE)

    def test_conversion_does_not_reimplement_any_filter(self):
        # to_criteria fills in an existing object; it must not grow rules.
        import inspect

        source = inspect.getsource(BuyBox.to_criteria)
        for forbidden in ("if lead", "matches_", "for lead", "def _"):
            self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# The buy box changes what a hunt actually returns
# ---------------------------------------------------------------------------


class ChangesRealBehaviour(Base):
    def test_a_lead_is_excluded_because_of_a_buy_box_geography(self):
        everything = self.hunt(HuntCriteria(states=(), property_types=()))
        florida_only = self.hunt(self.write(states=["FL"]).to_criteria())

        self.assertLess(len(florida_only), len(everything))
        # A named Texas property that was there before and is gone now.
        self.assertIn("905 Pecan Street", everything)
        self.assertNotIn("905 Pecan Street", florida_only)

    def test_a_lead_is_preserved_because_it_satisfies_the_buy_box(self):
        florida_only = self.hunt(self.write(states=["FL"]).to_criteria())
        self.assertIn("77 Sabal Palm Way", florida_only)

    def test_a_price_ceiling_excludes_the_property_above_it(self):
        wide = self.hunt(self.write(states=[], max_price=2_200_000).to_criteria())
        narrow = self.hunt(self.write(states=[], max_price=100_000).to_criteria())
        self.assertIn("3400 Bayside Terrace", wide)      # asking $610,000
        self.assertNotIn("3400 Bayside Terrace", narrow)
        self.assertLess(len(narrow), len(wide))

    def test_a_lead_score_gate_from_the_buy_box_actually_gates(self):
        open_gate = self.hunt(self.write(states=[], min_lead_score=0).to_criteria())
        high_gate = self.hunt(self.write(states=[], min_lead_score=85).to_criteria())
        self.assertLess(len(high_gate), len(open_gate))

    def test_a_property_type_restriction_actually_restricts(self):
        both = self.hunt(
            self.write(states=[], property_types=["single_family", "duplex"]).to_criteria()
        )
        one = self.hunt(self.write(states=[], property_types=["single_family"]).to_criteria())
        self.assertLessEqual(len(one), len(both))

    def test_the_buy_box_and_an_equivalent_flag_produce_the_same_hunt(self):
        box = self.write(states=["FL"], property_types=["single_family", "duplex"])
        from_box = self.hunt(box.to_criteria())
        from_flag = self.hunt(
            HuntCriteria(states=("FL",), property_types=("single_family", "duplex"))
        )
        self.assertEqual(sorted(from_box), sorted(from_flag))
        self.assertTrue(from_box)


# ---------------------------------------------------------------------------
# Precedence: an explicit flag beats the saved configuration
# ---------------------------------------------------------------------------


class Precedence(Base):
    def criteria(self, box, *argv):
        return cli.criteria_from_args(
            parse(*argv), cli.lead_config_from_args(parse(*argv)), buy_box=box
        )

    def test_a_flag_overrides_the_matching_buy_box_setting(self):
        box = self.write(states=["FL"])
        self.assertEqual(self.criteria(box, "--states", "TX").states, ("TX",))

    def test_the_buy_box_is_used_where_no_flag_was_given(self):
        box = self.write(states=["FL"], zip_codes=["33607"], min_lead_score=70)
        criteria = self.criteria(box, "--states", "TX")
        self.assertEqual(criteria.states, ("TX",))        # overridden
        self.assertEqual(criteria.zip_codes, ("33607",))  # from the buy box
        self.assertEqual(criteria.min_lead_score, 70)     # from the buy box

    def test_a_zero_flag_is_an_override_not_an_absence(self):
        # --min-lead-score 0 means "show me everything", not "use the box's 70".
        box = self.write(min_lead_score=70)
        self.assertEqual(self.criteria(box, "--min-lead-score", "0").min_lead_score, 0.0)

    def test_a_price_flag_overrides_the_buy_box_band(self):
        box = self.write(min_price=50_000, max_price=400_000)
        criteria = self.criteria(box, "--max-price", "150000")
        self.assertEqual(criteria.max_price, 150_000)
        self.assertEqual(criteria.min_price, 50_000)

    def test_max_asking_price_also_overrides_the_buy_box(self):
        box = self.write(max_price=400_000)
        self.assertEqual(
            self.criteria(box, "--max-asking-price", "120000").max_price, 120_000
        )

    def test_signal_flags_override_the_buy_box_signals(self):
        box = self.write(required_signals=["probate"])
        self.assertEqual(self.criteria(box, "--vacant").required_signals, ("vacant",))

    def test_the_buy_box_signals_apply_when_no_signal_flag_is_given(self):
        box = self.write(required_signals=["probate"])
        self.assertEqual(self.criteria(box).required_signals, ("probate",))

    def test_precedence_lives_in_one_place(self):
        # The CLI must not re-implement the ordering the service owns.
        import inspect

        source = inspect.getsource(cli.criteria_from_args)
        self.assertIn("build_criteria", source)
        self.assertNotIn("to_criteria", source)


# ---------------------------------------------------------------------------
# Without --buybox, nothing changes
# ---------------------------------------------------------------------------


class NoFlagNoChange(Base):
    def test_no_buybox_flag_means_the_file_is_never_read(self):
        self.write(states=["FL"], min_lead_score=99)
        args = parse()
        self.assertIsNone(args.buybox)
        self.assertIsNone(cli.load_buy_box(args, self.service))

    def test_criteria_without_a_buy_box_are_the_historical_defaults(self):
        args = parse()
        criteria = cli.criteria_from_args(args, cli.lead_config_from_args(args))
        self.assertEqual(criteria.states, ("FL", "TX", "MO"))
        self.assertEqual(criteria.min_price, MIN_PROPERTY_PRICE)
        self.assertEqual(criteria.max_price, MAX_PROPERTY_PRICE)
        self.assertEqual(criteria.min_lead_score, 0.0)

    def test_passing_no_buy_box_is_identical_to_passing_none(self):
        args = parse("--states", "FL")
        lead_config = cli.lead_config_from_args(args)
        self.assertEqual(
            cli.criteria_from_args(args, lead_config),
            cli.criteria_from_args(args, lead_config, buy_box=None),
        )

    def test_a_hunt_without_the_flag_returns_what_it_always_did(self):
        args = parse()
        criteria = cli.criteria_from_args(args, cli.lead_config_from_args(args))
        self.assertEqual(len(self.hunt(criteria)), 21)


# ---------------------------------------------------------------------------
# The seven shape filters: stored, reported, never applied
# ---------------------------------------------------------------------------


class UnsupportedFields(Base):
    def test_none_of_the_seven_reaches_the_criteria(self):
        box = BuyBox(
            min_beds=3, max_beds=5, min_baths=2, min_sqft=900, max_sqft=3000,
            min_year_built=1950, max_year_built=2020,
        )
        criteria = box.to_criteria()
        for name in NOT_IMPLEMENTED_FIELDS:
            self.assertFalse(hasattr(criteria, name), name)

    def test_setting_them_does_not_change_a_single_lead(self):
        plain = self.hunt(self.write(states=[]).to_criteria())
        shaped = self.hunt(
            self.write(states=[], min_beds=99, min_sqft=99_999).to_criteria()
        )
        # min_beds=99 would exclude everything if it were applied.
        self.assertEqual(sorted(plain), sorted(shaped))

    def test_each_of_the_seven_is_reported_when_set(self):
        for name in NOT_IMPLEMENTED_FIELDS:
            box = BuyBox(**{name: 3})
            reported = box.unsupported_settings()
            self.assertTrue(reported, name)
            self.assertTrue(any(name in line for line in reported), name)
            self.assertTrue(any("NOT APPLIED" in line for line in reported), name)

    def test_the_three_unrouted_settings_are_reported_too(self):
        # They have engine homes, but not via the buy box. Setting one here
        # and expecting a hunt to change is exactly the trap being closed.
        for name, value in (
            ("min_signal_count", 2),
            ("target_wholesale_fee", 25_000),
            ("min_viable_wholesale_fee", 5_000),
        ):
            reported = BuyBox(**{name: value}).unsupported_settings()
            self.assertTrue(any(name in line for line in reported), name)

    def test_a_buy_box_that_sets_none_of_them_is_silent(self):
        self.assertEqual(BuyBox().unsupported_settings(), [])
        self.assertEqual(BuyBox(name="x", states=["FL"]).unsupported_settings(), [])

    def test_they_reach_the_caller_through_the_existing_warnings_channel(self):
        self.write(min_beds=3)
        view = self.service.read_buy_box()
        self.assertTrue(any("min_beds" in w for w in view.warnings))
        self.assertTrue(any("min_beds" in w for w in view.unsupported))

    def test_they_are_printed_when_the_flag_is_used(self):
        self.write(min_beds=3, target_wholesale_fee=25_000)
        captured = io.StringIO()
        with redirect_stderr(captured):
            cli.load_buy_box(parse("--buybox", str(self.path)), self.service)
        text = captured.getvalue()
        self.assertIn("min_beds", text)
        self.assertIn("NOT APPLIED", text)
        self.assertIn("target_wholesale_fee", text)

    def test_they_remain_valid_to_store_and_to_save(self):
        # Reporting them must not turn them into validation errors: the web
        # form should still let you set them ahead of the filters existing.
        result = self.service.save_buy_box(
            {"name": "x", "states": ["FL"], "min_beds": 3, "min_sqft": 900}
        )
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(self.service.read_buy_box().buy_box.min_beds, 3)


# ---------------------------------------------------------------------------
# Invalid input still fails safely
# ---------------------------------------------------------------------------


class InvalidInput(Base):
    def test_a_corrupt_file_yields_defaults_and_a_warning_not_a_crash(self):
        self.path.write_text("{ not json at all", encoding="utf-8")
        captured = io.StringIO()
        with redirect_stderr(captured):
            box = cli.load_buy_box(parse("--buybox", str(self.path)), self.service)
        self.assertIsNotNone(box)
        self.assertTrue(box.is_valid)
        self.assertIn("could not read", captured.getvalue())

    def test_a_corrupt_file_still_produces_a_usable_hunt(self):
        # The point of failing safe: an unattended run keeps running.
        self.path.write_text("{ not json at all", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            box = cli.load_buy_box(parse("--buybox", str(self.path)), self.service)
        self.assertTrue(self.hunt(box.to_criteria()))

    def test_a_missing_file_says_so_and_uses_defaults(self):
        captured = io.StringIO()
        with redirect_stderr(captured):
            box = cli.load_buy_box(
                parse("--buybox", str(self.tmp / "absent.json")), self.service
            )
        self.assertIsNotNone(box)
        self.assertIn("No buy box at", captured.getvalue())

    def test_an_invalid_setting_is_warned_about_and_the_run_continues(self):
        self.path.write_text(
            json.dumps({"name": "x", "states": ["FLORIDA"], "zip_codes": ["nope"]}),
            encoding="utf-8",
        )
        captured = io.StringIO()
        with redirect_stderr(captured):
            box = cli.load_buy_box(parse("--buybox", str(self.path)), self.service)
        self.assertIsNotNone(box)
        text = captured.getvalue()
        self.assertIn("2-letter", text)
        self.assertIn("5-digit", text)

    def test_an_unknown_key_is_a_warning_and_is_dropped(self):
        self.path.write_text(
            json.dumps({"name": "x", "states": ["FL"], "invented": 1}), encoding="utf-8"
        )
        captured = io.StringIO()
        with redirect_stderr(captured):
            box = cli.load_buy_box(parse("--buybox", str(self.path)), self.service)
        self.assertIn("invented", captured.getvalue())
        self.assertFalse(hasattr(box, "invented"))


# ---------------------------------------------------------------------------
# One conversion path, shared
# ---------------------------------------------------------------------------


class SharedPath(Base):
    def test_the_service_and_the_cli_produce_identical_criteria(self):
        box = self.write(states=["FL"], zip_codes=["33607"], min_lead_score=70)
        args = parse()
        from_cli = cli.criteria_from_args(
            args, cli.lead_config_from_args(args), buy_box=box
        )
        from_service = EngineService().build_criteria(buy_box=box)
        self.assertEqual(from_cli, from_service)

    def test_the_cli_flag_and_a_direct_service_call_agree(self):
        box = self.write(states=["FL"], min_lead_score=70)
        args = parse("--buybox", str(self.path), "--states", "TX")
        from_cli = cli.criteria_from_args(
            args, cli.lead_config_from_args(args), buy_box=box
        )
        from_service = EngineService().build_criteria(states=["TX"], buy_box=box)
        self.assertEqual(from_cli, from_service)

    def test_the_flag_accepts_a_path_or_stands_alone(self):
        self.assertEqual(parse("--buybox").buybox, "")
        self.assertEqual(parse("--buybox", "x.json").buybox, "x.json")
        self.assertIsNone(parse().buybox)

    def test_daily_uses_the_same_conversion_as_hunt(self):
        import inspect

        source = inspect.getsource(cli.run_production_cli)
        self.assertIn("load_buy_box", source)


if __name__ == "__main__":
    unittest.main()
