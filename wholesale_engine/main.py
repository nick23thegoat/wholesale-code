"""Command-line entry point for the wholesale acquisition engine.

Examples::

    # Wave 1 — analyze properties you already qualified
    python -m wholesale_engine.main --sample
    python -m wholesale_engine.main --csv my_leads.csv --comps my_comps.csv

    # Wave 2 — hunt through a raw lead list
    python -m wholesale_engine.main --sample-leads
    python -m wholesale_engine.main --leads data/lead_sources/sample_leads.csv \
        --states FL,TX,MO --min-lead-score 60 --min-deal-score 60
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional

if __package__ in (None, ""):  # allow `python wholesale_engine/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wholesale_engine.analysis import analyze_properties  # noqa: E402
from wholesale_engine.config import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_LEAD_CONFIG,
    EngineConfig,
    LeadHunterConfig,
)
from wholesale_engine.data.csv_loader import (  # noqa: E402
    LoadReport,
    load_properties_csv,
    load_properties_json,
)
from wholesale_engine.lead_hunter import (  # noqa: E402
    run_from_csv as run_lead_hunter,
    with_overrides,
)
from wholesale_engine.hunt import HuntBudget, run_hunt  # noqa: E402
from wholesale_engine.outputs import CsvAdapter, JsonAdapter, publish_all  # noqa: E402
from wholesale_engine.models.results import AnalysisResult  # noqa: E402
from wholesale_engine.providers import (  # noqa: E402
    HuntCriteria,
    ProviderNotConfigured,
    describe_sources,
    get_provider,
)
from wholesale_engine.reports.hunt_report import (  # noqa: E402
    render_hunt_summary,
    write_hunt_outputs,
)
from wholesale_engine.settings import (  # noqa: E402
    NO_PROVIDER_MESSAGE,
    ProviderSettings,
)
from wholesale_engine.reports.deal_tables import (  # noqa: E402
    DEAL_COLUMNS,
    deal_rows,
    render_deal_table,
    render_watchlist,
)
from wholesale_engine.reports.dossier import render_dossier  # noqa: E402
from wholesale_engine.storage import (  # noqa: E402
    ACTIVE_STATUSES,
    DEFAULT_DB_PATH,
    LEAD_STATUSES,
    SORT_KEYS,
    LeadStore,
    SearchQuery,
)
from wholesale_engine.reports import (  # noqa: E402
    render_batch_summary,
    render_lead_summary,
    render_result,
    write_csv,
    write_hot_leads_csv,
    write_lead_pipeline_csv,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
SAMPLE_PROPERTIES = PACKAGE_ROOT / "data" / "sample_properties.csv"
SAMPLE_COMPS = PACKAGE_ROOT / "data" / "sample_comps.csv"
DEFAULT_OUTPUT = PACKAGE_ROOT / "reports" / "output" / "deal_analysis.csv"
SAMPLE_LEADS = PACKAGE_ROOT / "data" / "lead_sources" / "sample_leads.csv"
SAMPLE_LEAD_COMPS = PACKAGE_ROOT / "data" / "lead_sources" / "sample_lead_comps.csv"
DEFAULT_LEAD_OUTPUT = PACKAGE_ROOT / "reports" / "output" / "lead_pipeline.csv"
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "reports" / "output"
DEFAULT_HOT_OUTPUT = PACKAGE_ROOT / "reports" / "output" / "hot_leads.csv"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wholesale-engine",
        description=(
            "Screen real-estate wholesale leads from data you supply. "
            "This tool has no access to Zillow, the MLS, county records, or "
            "skip-tracing databases and never invents any of that data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_argument_group("input")
    source.add_argument("--csv", type=Path, help="properties CSV to analyze")
    source.add_argument("--comps", type=Path, help="comps CSV joined on property_id")
    source.add_argument("--json", type=Path, help="properties JSON file to analyze")
    source.add_argument(
        "--sample", action="store_true", help="run the bundled fictional sample data"
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--out", type=Path, default=None, help=f"CSV output path (default: {DEFAULT_OUTPUT})"
    )
    output.add_argument("--report", type=Path, help="write the full text reports to this file")
    output.add_argument(
        "--detail", action="store_true", help="add diagnostic columns to the CSV export"
    )
    output.add_argument(
        "--summary-only", action="store_true", help="print only the batch summary table"
    )
    output.add_argument("--quiet", action="store_true", help="suppress stdout reports entirely")

    hunter = parser.add_argument_group("lead hunting (Wave 2)")
    hunter.add_argument("--leads", type=Path, help="raw lead-list CSV to hunt through")
    hunter.add_argument(
        "--lead-comps", type=Path, help="optional comps CSV joined to the leads by id/address"
    )
    hunter.add_argument(
        "--sample-leads", action="store_true", help="run the bundled fictional lead list"
    )
    hunter.add_argument(
        "--states",
        type=str,
        default=None,
        help="comma-separated target states (default: %s)" % ",".join(DEFAULT_LEAD_CONFIG.target_states),
    )
    hunter.add_argument(
        "--property-types",
        type=str,
        default=None,
        help="comma-separated target property types (default: %s)"
        % ",".join(DEFAULT_LEAD_CONFIG.preferred_property_types),
    )
    hunter.add_argument("--min-lead-score", type=float, default=None, help="drop leads below this lead score")
    hunter.add_argument("--min-deal-score", type=float, default=None, help="drop leads below this deal score")
    hunter.add_argument("--max-asking-price", type=float, default=None, help="drop leads asking above this")
    hunter.add_argument("--min-equity", type=float, default=None, help="drop leads with less equity than this")
    hunter.add_argument(
        "--hot-only", action="store_true", help="report only 🔥 HOT and 🟠 STRONG leads"
    )
    hunter.add_argument("--lead-out", type=Path, default=None, help=f"pipeline CSV (default: {DEFAULT_LEAD_OUTPUT})")
    hunter.add_argument("--hot-out", type=Path, default=None, help=f"hot-lead CSV (default: {DEFAULT_HOT_OUTPUT})")

    hunt = parser.add_argument_group("hunt (Wave 4 — provider-backed)")
    hunt.add_argument(
        "--hunt",
        action="store_true",
        help="run the cost-controlled funnel: search -> filter -> score -> research -> comps -> deal",
    )
    hunt.add_argument(
        "--source",
        default="csv",
        help="property-data provider (default: csv). Use --list-sources to see what is available.",
    )
    hunt.add_argument(
        "--list-sources",
        action="store_true",
        help="show every provider and whether it is configured, then exit",
    )
    hunt.add_argument("--counties", help="comma-separated counties to search")
    hunt.add_argument("--cities", help="comma-separated cities to search")
    hunt.add_argument("--zip-codes", help="comma-separated ZIP codes to search")
    hunt.add_argument("--min-price", type=float, default=None, help="lowest asking price to consider")
    hunt.add_argument("--max-price", type=float, default=None, help="highest asking price to consider")
    for signal, helptext in (
        ("vacant", "reported vacant"),
        ("absentee", "absentee owner"),
        ("high-equity", "high equity"),
        ("pre-foreclosure", "in pre-foreclosure"),
        ("foreclosure", "in foreclosure"),
        ("tax-delinquent", "tax delinquent"),
        ("probate", "in probate"),
        ("inherited", "inherited"),
        ("code-violation", "carrying a code violation"),
        ("tired-landlord", "a tired landlord"),
    ):
        hunt.add_argument(
            f"--{signal}",
            action="store_true",
            help=f"require leads {helptext} (any-of when several are given; unknown never rejects)",
        )
    hunt.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"SQLite lead database (default: {DEFAULT_DB_PATH}); use :memory: to disable persistence",
    )
    hunt.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=f"directory for hunt outputs (default: {DEFAULT_OUTPUT_DIR})",
    )
    hunt.add_argument(
        "--research-limit", type=int, default=None,
        help="cap on billable property-detail calls (default: 100)",
    )
    hunt.add_argument(
        "--comps-limit", type=int, default=None,
        help="cap on billable comp calls — the most expensive stage (default: 30)",
    )
    hunt.add_argument(
        "--no-json", action="store_true", help="skip the JSON output"
    )

    database = parser.add_argument_group("lead database (Wave 4)")
    database.add_argument(
        "--search", action="store_true",
        help="search the stored leads; combine with any filter flag below",
    )
    database.add_argument(
        "--top-deals", action="store_true",
        help="the best analyzed deals, ranked by priority score",
    )
    database.add_argument(
        "--hot-leads", action="store_true",
        help="leads worth calling today, by priority, deal score, lead score, then fee",
    )
    database.add_argument(
        "--watchlist", action="store_true",
        help="everything you have actively moved into the pipeline",
    )
    database.add_argument(
        "--property", metavar="PROPERTY_ID",
        help="full research dossier for one property (lead id, or part of an address)",
    )
    database.add_argument("--limit", type=int, default=None, help="cap the rows returned")
    database.add_argument(
        "--sort-by", default=None,
        help=f"ranking column: {', '.join(SORT_KEYS)}",
    )
    database.add_argument("--status", action="append", help="filter by watchlist status (repeatable)")
    database.add_argument(
        "--open-only", action="store_true", help="exclude PASSED, DEAD and CLOSED leads",
    )
    database.add_argument("--min-arv", type=float, default=None, help="minimum ARV")
    database.add_argument("--max-arv", type=float, default=None, help="maximum ARV")
    database.add_argument(
        "--min-priority-score", type=float, default=None, help="minimum priority score",
    )
    database.add_argument("--min-fee", type=float, default=None, help="minimum potential wholesale fee")
    database.add_argument("--min-dom", type=int, default=None, help="minimum days on market")
    database.add_argument("--max-dom", type=int, default=None, help="maximum days on market")
    database.add_argument("--text", default=None, help="free-text match on address, city, county or notes")

    workflow = parser.add_argument_group("watchlist actions (Wave 4)")
    workflow.add_argument(
        "--set-status", metavar="STATUS",
        help=f"move --property to a status: {', '.join(LEAD_STATUSES)}",
    )
    workflow.add_argument("--reason", default="", help="why the status changed")
    workflow.add_argument("--note", metavar="TEXT", help="attach a note to --property")
    workflow.add_argument("--author", default="", help="who wrote the note")
    workflow.add_argument(
        "--activity", action="store_true", help="show the full activity log and exit",
    )

    export = parser.add_argument_group("export (Wave 4)")
    export.add_argument("--export-hot", action="store_true", help="export hot leads")
    export.add_argument("--export-top-deals", action="store_true", help="export top deals")
    export.add_argument("--export-watchlist", action="store_true", help="export the watchlist")
    export.add_argument(
        "--format", default="both", choices=("csv", "json", "both"),
        help="export format (default: both)",
    )

    assumptions = parser.add_argument_group("underwriting assumptions")
    assumptions.add_argument(
        "--arv-pct",
        type=float,
        default=DEFAULT_CONFIG.arv_percentage * 100,
        help="percent of ARV used in the MAO formula (default: 70)",
    )
    assumptions.add_argument(
        "--fee",
        type=float,
        default=DEFAULT_CONFIG.wholesale_fee,
        help="target wholesale fee in dollars (default: 18000)",
    )
    assumptions.add_argument(
        "--viable-fee",
        type=float,
        default=DEFAULT_CONFIG.min_viable_wholesale_fee,
        help=(
            "fee below which a deal is not called a GO (default: 10000). This is a "
            "viability floor, NOT the target — pass 0 to let the deal score decide alone."
        ),
    )
    assumptions.add_argument(
        "--strict",
        action="store_true",
        help="abort on any unparseable input row instead of skipping it",
    )
    return parser


def load_leads(args: argparse.Namespace) -> LoadReport:
    if args.sample:
        return load_properties_csv(SAMPLE_PROPERTIES, SAMPLE_COMPS, strict=args.strict)
    if args.json:
        return load_properties_json(args.json)
    return load_properties_csv(args.csv, args.comps, strict=args.strict)


def _split(value: Optional[str]) -> Optional[tuple]:
    if not value:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def lead_config_from_args(args: argparse.Namespace) -> LeadHunterConfig:
    """Build the lead-hunter config from CLI overrides (nothing hard-coded)."""
    states = _split(args.states)
    return with_overrides(
        DEFAULT_LEAD_CONFIG,
        target_states=tuple(s.upper() for s in states) if states else None,
        preferred_property_types=_split(args.property_types),
        min_lead_score=args.min_lead_score,
        min_deal_score=args.min_deal_score,
        max_asking_price=args.max_asking_price,
        min_equity=args.min_equity,
    )


def run_lead_pipeline_cli(args: argparse.Namespace, engine_config: EngineConfig) -> int:
    """Wave 2: raw lead list -> normalize -> dedupe -> score -> filter -> Wave 1."""
    leads_path = SAMPLE_LEADS if args.sample_leads else args.leads
    comps_path = SAMPLE_LEAD_COMPS if args.sample_leads else args.lead_comps
    lead_config = lead_config_from_args(args)

    report = run_lead_hunter(
        leads_path,
        engine_config=engine_config,
        lead_config=lead_config,
        comps_path=comps_path,
    )
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if not report.results:
        print("No usable leads were loaded.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(render_lead_summary(report, lead_config, hot_only=args.hot_only))

    pipeline_csv = write_lead_pipeline_csv(
        report,
        args.lead_out or DEFAULT_LEAD_OUTPUT,
        include_detail=True,
        hot_only=args.hot_only,
        lead_config=lead_config,
    )
    hot_csv = write_hot_leads_csv(
        report, args.hot_out or DEFAULT_HOT_OUTPUT, lead_config=lead_config
    )
    if not args.quiet:
        print(f"\nPipeline CSV written to: {pipeline_csv}")
        print(f"Hot leads CSV written to: {hot_csv}")
    return 0


# ---------------------------------------------------------------------------
# Wave 4 — hunt
# ---------------------------------------------------------------------------

#: CLI flag -> signal field name.
SIGNAL_FLAGS = {
    "vacant": "vacant",
    "absentee": "absentee_owner",
    "high_equity": "high_equity",
    "pre_foreclosure": "pre_foreclosure",
    "foreclosure": "foreclosure",
    "tax_delinquent": "tax_delinquent",
    "probate": "probate",
    "inherited": "inherited",
    "code_violation": "code_violation",
    "tired_landlord": "tired_landlord",
}


def criteria_from_args(
    args: argparse.Namespace, lead_config: LeadHunterConfig
) -> HuntCriteria:
    """Build the search criteria from the command line."""
    signals = tuple(
        field for flag, field in SIGNAL_FLAGS.items() if getattr(args, flag, False)
    )
    return HuntCriteria(
        states=_split(args.states) or lead_config.target_states,
        counties=_split(args.counties) or (),
        cities=_split(args.cities) or (),
        zip_codes=_split(args.zip_codes) or (),
        property_types=_split(args.property_types) or lead_config.preferred_property_types,
        min_price=args.min_price,
        max_price=args.max_price if args.max_price is not None else args.max_asking_price,
        min_equity=args.min_equity,
        required_signals=signals,
        min_lead_score=args.min_lead_score or 0.0,
        min_deal_score=args.min_deal_score or 0.0,
    )


def run_hunt_cli(args: argparse.Namespace, engine_config: EngineConfig) -> int:
    """``--hunt``: provider search through to the four output files."""
    lead_config = lead_config_from_args(args)
    criteria = criteria_from_args(args, lead_config)

    settings = ProviderSettings.from_env()
    requested = (args.source or "csv").strip().lower()
    if requested != "csv" and not settings.has_property_data:
        print(NO_PROVIDER_MESSAGE, file=sys.stderr)
        missing = ", ".join(settings.missing_for_property_data())
        print(
            f"  '{requested}' needs {missing}. Copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        requested = "csv"

    # Resolved after the fallback, so an unconfigured live source lands on a
    # working CSV run rather than on an error about a missing file.
    csv_path = args.leads or (SAMPLE_LEADS if requested == "csv" else None)
    comps_path = args.lead_comps or (
        SAMPLE_LEAD_COMPS if csv_path == SAMPLE_LEADS else None
    )
    if requested == "csv" and csv_path == SAMPLE_LEADS and not args.leads:
        print(
            "No --leads file given; hunting the bundled FICTIONAL sample lead list.",
            file=sys.stderr,
        )

    try:
        provider = get_provider(
            requested, settings=settings, csv_path=csv_path, comps_path=comps_path
        )
    except ProviderNotConfigured as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    budget = HuntBudget()
    if args.research_limit is not None:
        budget.research_limit = args.research_limit
    if args.comps_limit is not None:
        budget.comps_limit = args.comps_limit

    db_path = args.db or DEFAULT_DB_PATH
    store = LeadStore(db_path)
    try:
        result = run_hunt(
            provider,
            criteria,
            engine_config=engine_config,
            lead_config=lead_config,
            budget=budget,
            store=store,
        )
    finally:
        store.close()

    if not args.quiet:
        print(render_hunt_summary(result))

    written = write_hunt_outputs(
        result, args.out_dir or DEFAULT_OUTPUT_DIR, write_json=not args.no_json
    )
    if not args.quiet:
        print()
        for label in sorted(written):
            print(f"{label:<20} -> {written[label]}")
        print(f"{'lead database':<20} -> {db_path}")
    return 0


# ---------------------------------------------------------------------------
# Wave 4 — database commands
# ---------------------------------------------------------------------------


def query_from_args(args: argparse.Namespace) -> SearchQuery:
    """Turn the CLI filters into a :class:`SearchQuery`."""
    signals = {
        field: True for flag, field in SIGNAL_FLAGS.items() if getattr(args, flag, False)
    }
    return SearchQuery(
        states=_split(args.states) or (),
        counties=_split(args.counties) or (),
        cities=_split(args.cities) or (),
        zip_codes=_split(args.zip_codes) or (),
        property_types=_split(args.property_types) or (),
        min_price=args.min_price,
        max_price=args.max_price if args.max_price is not None else args.max_asking_price,
        min_arv=args.min_arv,
        max_arv=args.max_arv,
        min_equity=args.min_equity,
        min_fee=args.min_fee,
        min_lead_score=args.min_lead_score,
        min_deal_score=args.min_deal_score,
        min_priority_score=args.min_priority_score,
        min_days_on_market=args.min_dom,
        max_days_on_market=args.max_dom,
        statuses=tuple(args.status or ()),
        exclude_closed=args.open_only,
        text=args.text or "",
        limit=args.limit,
        sort_by=args.sort_by or "priority_score",
        **signals,
    )


def open_store(args: argparse.Namespace) -> LeadStore:
    return LeadStore(args.db or DEFAULT_DB_PATH)


def _export(
    args: argparse.Namespace, rows, label: str, config: EngineConfig
) -> List[Path]:
    """Write one dataset through the CSV and/or JSON adapters."""
    directory = args.out_dir or DEFAULT_OUTPUT_DIR
    payload = deal_rows(rows, config.target_wholesale_fee)
    adapters: List[Any] = []
    if args.format in ("csv", "both"):
        adapters.append(CsvAdapter(directory))
    if args.format in ("json", "both"):
        adapters.append(JsonAdapter(directory, meta={"export": label, "count": len(payload)}))
    return publish_all(adapters, payload, DEAL_COLUMNS, label)


def run_database_cli(args: argparse.Namespace, config: EngineConfig) -> int:
    """``--search`` / ``--top-deals`` / ``--hot-leads`` / ``--watchlist`` /
    ``--property`` / ``--export-*`` / ``--activity``, all off the local store."""
    store = open_store(args)
    try:
        if args.property:
            return _property_dossier(args, store, config)

        if args.activity:
            entries = store.recent_activity(args.limit or 50)
            if not entries:
                print("No activity recorded yet. Run --hunt first.")
            for entry in entries:
                print(
                    f"{entry['created_at'][:16]}  {entry['activity_type']:<22}"
                    f"{entry['description']}"
                )
            return 0

        exports: List[Path] = []
        printed = False

        if args.top_deals or args.export_top_deals:
            rows = store.top_deals(args.limit or 20, exclude_closed=True)
            if args.top_deals and not args.quiet:
                print(
                    render_deal_table(
                        rows, "TOP DEALS",
                        "Ranked by PRIORITY SCORE. Analyzed properties only.",
                        config.target_wholesale_fee,
                    )
                )
                printed = True
            if args.export_top_deals:
                exports += _export(args, rows, "top_deals", config)

        if args.hot_leads or args.export_hot:
            rows = store.hot_leads(args.limit)
            if args.hot_leads and not args.quiet:
                print(
                    render_deal_table(
                        rows, "HOT LEADS",
                        "By priority score, then deal score, lead score, potential fee.",
                        config.target_wholesale_fee,
                    )
                )
                printed = True
            if args.export_hot:
                exports += _export(args, rows, "hot_leads_export", config)

        if args.watchlist or args.export_watchlist:
            rows = store.watchlist(args.limit)
            if args.watchlist and not args.quiet:
                print(render_watchlist(rows, store.status_counts()))
                printed = True
            if args.export_watchlist:
                exports += _export(args, rows, "watchlist", config)

        if args.search:
            query = query_from_args(args)
            rows = store.search(query)
            if not args.quiet:
                print(
                    render_deal_table(
                        rows, "SEARCH RESULTS", query.describe(), config.target_wholesale_fee
                    )
                )
                printed = True

        if exports and not args.quiet:
            print()
            for path in exports:
                print(f"exported -> {path}")
        if not printed and not exports and not args.quiet:
            print("Nothing to show. Run --hunt first to populate the lead database.")
        return 0
    finally:
        store.close()


def _property_dossier(
    args: argparse.Namespace, store: LeadStore, config: EngineConfig
) -> int:
    """``--property``: the research screen, plus any watchlist action."""
    stored = store.find_one(args.property)
    if stored is None:
        print(
            f"No stored property matches '{args.property}'. Run --hunt first, or try "
            "part of the address.",
            file=sys.stderr,
        )
        return 1

    if args.set_status:
        status = args.set_status.strip().upper()
        if status not in LEAD_STATUSES:
            print(
                f"Unknown status '{args.set_status}'. Valid: {', '.join(LEAD_STATUSES)}",
                file=sys.stderr,
            )
            return 2
        store.set_status(stored.lead_row_id, status, args.reason)
        stored = store.find_one(args.property) or stored
        if not args.quiet:
            print(f"Status -> {status}" + (f" ({args.reason})" if args.reason else ""))

    if args.note:
        store.add_note(stored.lead_row_id, args.note, args.author)
        if not args.quiet:
            print("Note added.")

    if args.quiet:
        return 0

    # Re-run research and analysis for this one property so the dossier shows
    # live comps, ARV and economics rather than only the stored snapshot. On a
    # CSV source this costs nothing; on a live provider it is one property.
    result = research = priority = None
    live = _live_analysis(args, stored, config)
    if live is not None:
        result, research, priority = live

    print(
        render_dossier(
            result=result,
            research=research,
            priority=priority,
            stored=stored,
            notes=store.notes(stored.lead_row_id),
            activities=store.activities(stored.lead_row_id, limit=args.limit or 25),
            status_history=store.status_history(stored.lead_row_id),
            config=config,
        )
    )
    return 0


def _live_analysis(args: argparse.Namespace, stored, config: EngineConfig):
    """Re-research and re-analyze one stored property from its source.

    Returns ``(LeadResult, PropertyResearch, PriorityScore)`` or ``None`` when
    the source is unavailable — in which case the dossier falls back to the
    stored snapshot rather than showing nothing.
    """
    csv_path = args.leads or SAMPLE_LEADS
    comps_path = args.lead_comps or (SAMPLE_LEAD_COMPS if csv_path == SAMPLE_LEADS else None)
    if not Path(csv_path).exists():
        return None
    try:
        provider = get_provider(
            "csv", settings=ProviderSettings.from_env(),
            csv_path=Path(csv_path), comps_path=Path(comps_path) if comps_path else None,
        )
    except ProviderNotConfigured:
        return None

    from wholesale_engine.storage import dedupe_key

    lead_config = lead_config_from_args(args)
    hunt = run_hunt(
        provider,
        HuntCriteria(states=(), property_types=()),
        engine_config=config,
        lead_config=lead_config,
        budget=HuntBudget(research_min_lead_score=0.0, comps_min_lead_score=0.0),
        store=None,
    )
    for entry in hunt.prioritized:
        if dedupe_key(entry.lead) == stored.dedupe_key:
            key = stored.dedupe_key
            return entry, hunt.research.get(key), hunt.priorities.get(key)
    return None


def render_all(results: List[AnalysisResult], config: EngineConfig) -> str:
    return "\n\n".join(render_result(result, config) for result in results)


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_sources:
        print(describe_sources())
        return 0

    database_command = bool(
        args.search or args.top_deals or args.hot_leads or args.watchlist
        or args.property or args.activity or args.export_hot
        or args.export_top_deals or args.export_watchlist
    )

    hunting = bool(args.leads or args.sample_leads)
    if not (args.sample or args.csv or args.json or hunting or args.hunt or database_command):
        build_parser().print_help()
        print(
            "\nNothing to analyze. Start with:  --sample  (Wave 1),  "
            "--sample-leads  (Wave 2),  --hunt --source csv  (Wave 4),  "
            "then  --top-deals  /  --hot-leads  /  --property <id>",
            file=sys.stderr,
        )
        return 2

    for path in (args.csv, args.comps, args.json, args.leads, args.lead_comps):
        if path and not Path(path).exists():
            print(f"Input file not found: {path}", file=sys.stderr)
            return 2

    config = EngineConfig(
        arv_percentage=args.arv_pct / 100.0,
        target_wholesale_fee=args.fee,
        min_viable_wholesale_fee=args.viable_fee,
    )

    if database_command and not args.hunt:
        return run_database_cli(args, config)

    if args.hunt:
        code = run_hunt_cli(args, config)
        if code == 0 and database_command:
            return run_database_cli(args, config)
        return code

    if hunting:
        return run_lead_pipeline_cli(args, config)

    report = load_leads(args)
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if not report.leads:
        print("No usable properties were loaded.", file=sys.stderr)
        return 1

    results = analyze_properties(report.leads, config)

    if not args.quiet:
        if not args.summary_only:
            print(render_all(results, config))
            print()
        print(render_batch_summary(results))

    csv_path = write_csv(results, args.out or DEFAULT_OUTPUT, include_detail=args.detail)
    if not args.quiet:
        print(f"\nCSV written to: {csv_path}")

    if args.report:
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            render_all(results, config) + "\n\n" + render_batch_summary(results) + "\n",
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"Text report written to: {destination}")

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
