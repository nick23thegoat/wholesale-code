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
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, Tuple

if __package__ in (None, ""):  # allow `python wholesale_engine/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wholesale_engine.analysis import analyze_properties  # noqa: E402
from wholesale_engine.config import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_LEAD_CONFIG,
    EngineConfig,
    LeadHunterConfig,
    MAX_PROPERTY_PRICE,
    MIN_PROPERTY_PRICE,
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
from wholesale_engine.acquisitions import (  # noqa: E402
    AcquisitionStore,
    AcquisitionWorkflow,
    Assignment,
    AssignmentStatus,
    Buyer,
    Channel,
    Contract,
    ContractStatus,
    Direction,
    OfferStatus,
    Outcome,
    SellerResponse,
    SkipTraceNotConfigured,
    get_skip_trace_provider,
    skip_trace_status,
)
from wholesale_engine.formatting import money  # noqa: E402
from wholesale_engine.automation import (  # noqa: E402
    DailyPriorityEngine,
    monitor,
    render_daily_report,
    render_monitor,
    render_priority,
    run_daily,
)
from wholesale_engine.backup import create_backup  # noqa: E402
from wholesale_engine.budget import ApiBudget  # noqa: E402
from wholesale_engine.hunt import HuntBudget, run_hunt  # noqa: E402
from wholesale_engine.importer import ImportError_, run_import  # noqa: E402
from wholesale_engine.integrations import (  # noqa: E402
    EventType,
    NotificationCenter,
    get_note_writer,
    get_sheets_adapter,
    integration_status,
)
from wholesale_engine.runtime import ModeError, RunMode, RuntimeConfig  # noqa: E402
from wholesale_engine.security import (  # noqa: E402
    ValidationError,
    audit_source,
    render_audit,
    safe_path,
)
from wholesale_engine.outputs import CsvAdapter, JsonAdapter, publish_all  # noqa: E402
from wholesale_engine.models.results import AnalysisResult  # noqa: E402
from wholesale_engine.providers import (  # noqa: E402
    HuntCriteria,
    ProviderNotConfigured,
    capability_matrix,
    describe_sources,
    get_provider,
    health_report,
    propertyreach_schema_status,
    registration,
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
from wholesale_engine.reports.acquisition_exports import (  # noqa: E402
    ASSIGNMENT_COLUMNS,
    BUYER_COLUMNS,
    CONTACT_COLUMNS,
    CONTRACT_COLUMNS,
    OFFER_COLUMNS,
    OUTREACH_COLUMNS,
    PIPELINE_COLUMNS,
    assignment_rows,
    buyer_rows,
    contact_rows,
    contract_rows,
    offer_rows,
    outreach_rows,
    pipeline_rows,
)
from wholesale_engine.reports.acquisitions import (  # noqa: E402
    FOLLOW_UP_COLUMNS,
    follow_up_rows,
    render_contact_queue,
    render_daily,
    render_dashboard,
    render_follow_ups,
)
from wholesale_engine.reports.deal_room import render_deal_room  # noqa: E402
from wholesale_engine.reports.dossier import render_dossier  # noqa: E402
from wholesale_engine.storage import (  # noqa: E402
    ACTIVE_STATUSES,
    DEFAULT_DB_PATH,
    LEAD_STATUSES,
    SORT_KEYS,
    STATUS_ASSIGNED,
    STATUS_BUYER_SEARCH,
    STATUS_CONTACT_READY,
    STATUS_UNDER_CONTRACT,
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
    hunt.add_argument(
        "--min-price", type=float, default=None,
        help=f"lowest asking price to search (default: ${MIN_PROPERTY_PRICE:,.0f})",
    )
    hunt.add_argument(
        "--max-price", type=float, default=None,
        help=f"highest asking price to search (default: ${MAX_PROPERTY_PRICE:,.0f} — "
             "what the buyer network can close, NOT a claim that everything "
             "under it is a deal)",
    )
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

    acquisition = parser.add_argument_group("acquisitions workflow (Wave 5)")
    acquisition.add_argument(
        "--contact-queue", action="store_true",
        help="who to work next, ranked by acquisition priority",
    )
    acquisition.add_argument(
        "--follow-ups", action="store_true",
        help="scheduled follow-ups: overdue, today, upcoming",
    )
    acquisition.add_argument(
        "--dashboard", action="store_true",
        help="pipeline counts and projected (not earned) economics",
    )
    acquisition.add_argument(
        "--deal-room", metavar="PROPERTY_ID",
        help="the complete deal summary for one property",
    )
    acquisition.add_argument(
        "--skip-trace", action="store_true",
        help="run the configured skip-trace provider against --property (or the queue)",
    )
    acquisition.add_argument(
        "--skip-trace-provider", default="none",
        help="skip-trace provider: none (default) or mock (FICTIONAL TEST DATA)",
    )
    for flag, channel in (
        ("--log-call", "CALL"),
        ("--log-text", "TEXT"),
        ("--log-email", "EMAIL"),
        ("--log-voicemail", "VOICEMAIL"),
        ("--log-mail", "MAIL"),
    ):
        acquisition.add_argument(
            flag, action="store_true",
            help=f"log a {channel.lower()} against --property (nothing is sent)",
        )
    acquisition.add_argument(
        "--log-note", action="store_true",
        help="log a contact note against --property without a channel",
    )
    acquisition.add_argument(
        "--outcome",
        help="outcome of the logged attempt: NO_ANSWER, LEFT_VOICEMAIL, CONNECTED, "
             "INTERESTED, NOT_INTERESTED, CALL_BACK, WANTS_OFFER, OFFER_SENT, "
             "NEGOTIATING, DEAD",
    )
    acquisition.add_argument(
        "--follow-up", metavar="YYYY-MM-DD", help="schedule the next follow-up",
    )
    acquisition.add_argument(
        "--follow-up-reason", default="", help="why the follow-up is scheduled",
    )
    acquisition.add_argument(
        "--inbound", action="store_true", help="the seller contacted you, not the other way",
    )
    acquisition.add_argument(
        "--make-offer", type=float, metavar="AMOUNT",
        help="record an offer on --property (warns above MAO, never blocks)",
    )
    acquisition.add_argument(
        "--offer-status", default="SENT",
        help="offer status: DRAFT, SENT, COUNTERED, ACCEPTED, REJECTED, EXPIRED, WITHDRAWN",
    )
    acquisition.add_argument(
        "--counter", type=float, metavar="AMOUNT",
        help="record the seller's counter on --property",
    )
    acquisition.add_argument(
        "--contract", action="store_true", help="record or update a contract on --property",
    )
    acquisition.add_argument("--purchase-price", type=float, help="contract purchase price")
    acquisition.add_argument("--closing-date", metavar="YYYY-MM-DD", help="contract closing date")
    acquisition.add_argument(
        "--inspection-deadline", metavar="YYYY-MM-DD", help="inspection deadline",
    )
    acquisition.add_argument("--earnest-money", type=float, help="earnest money deposit")
    acquisition.add_argument(
        "--assignment-allowed", choices=("yes", "no"),
        help="whether the contract as signed permits assignment",
    )
    acquisition.add_argument(
        "--contract-status", default=None,
        help="PENDING, INSPECTION, CLEAR_TO_CLOSE, CLOSED, CANCELLED",
    )
    acquisition.add_argument("--add-buyer", metavar="NAME", help="add or update an end buyer")
    acquisition.add_argument("--buyer-company", default="", help="buyer company")
    acquisition.add_argument("--buyer-email", default=None, help="buyer email")
    acquisition.add_argument("--buyer-phone", default=None, help="buyer phone")
    acquisition.add_argument("--buyer-states", default=None, help="buyer's states, comma separated")
    acquisition.add_argument("--buyer-types", default=None, help="buyer's property types")
    acquisition.add_argument("--buyer-min", type=float, default=None, help="buyer minimum price")
    acquisition.add_argument("--buyer-max", type=float, default=None, help="buyer maximum price")
    acquisition.add_argument("--buyers", action="store_true", help="list the buyer database")
    acquisition.add_argument(
        "--assign", metavar="BUYER", help="record an assignment of --property to a buyer",
    )
    acquisition.add_argument(
        "--assignment-price", type=float, help="what the end buyer pays",
    )
    acquisition.add_argument(
        "--assignment-status", default=None,
        help="BUYER_SEARCH, BUYER_INTERESTED, BUYER_OFFER, ASSIGNMENT_SIGNED, CLOSED, FAILED",
    )

    production = parser.add_argument_group("production (Wave 6)")
    production.add_argument(
        "--daily", action="store_true",
        help="the full daily acquisitions run: ingest, dedupe, detect changes, "
             "score, research, rank, and report",
    )
    production.add_argument(
        "--mode", choices=("TEST", "LIVE", "test", "live"), default=None,
        help="TEST (default) uses local files and fictional data; LIVE uses real "
             "provider APIs and refuses to start without the credentials",
    )
    production.add_argument(
        "--provider-status", action="store_true",
        help="which provider supports which capability, and what is connected",
    )
    production.add_argument(
        "--integrations", action="store_true",
        help="BUILT / CONFIGURED / CONNECTED status for every outbound adapter",
    )
    production.add_argument(
        "--health", action="store_true",
        help="try every configured provider and report whether it is usable",
    )
    production.add_argument(
        "--security-audit", action="store_true",
        help="scan the package for hard-coded secrets, shell calls and unsafe SQL",
    )
    production.add_argument(
        "--budget-status", action="store_true", help="show the API caps for this run",
    )
    production.add_argument(
        "--monitor", action="store_true",
        help="what changed on stored leads since their previous sighting",
    )
    production.add_argument(
        "--max-raw-leads", type=int, default=None, help="cap raw leads pulled per run",
    )
    production.add_argument(
        "--max-research", type=int, default=None, help="cap billable research calls",
    )
    production.add_argument(
        "--max-comps", type=int, default=None, help="cap billable comp calls",
    )
    production.add_argument(
        "--max-skip-traces", type=int, default=None, help="cap billable skip traces",
    )
    production.add_argument(
        "--auto-skip-trace", action="store_true",
        help="skip trace qualifying leads without asking (off by default)",
    )
    production.add_argument(
        "--yes", action="store_true", help="answer yes to confirmation prompts",
    )
    production.add_argument(
        "--no-ingest", action="store_true",
        help="--daily without pulling new leads: report on what is already stored",
    )
    production.add_argument(
        "--backup", action="store_true", help="write a timestamped backup archive",
    )
    production.add_argument(
        "--backup-dir", type=Path, default=None, help="where backups are written",
    )
    production.add_argument(
        "--include-secrets", action="store_true",
        help="include .env in the backup (excluded by default)",
    )
    production.add_argument(
        "--import", dest="import_kind", choices=("leads", "contacts", "buyers"),
        help="import records from --file without creating duplicates",
    )
    production.add_argument("--file", type=Path, default=None, help="file to import")
    production.add_argument(
        "--sheets", action="store_true", help="publish the sheet tabs through SHEETS_PROVIDER",
    )
    production.add_argument(
        "--response", metavar="RESPONSE",
        help="record a seller response on --property: INTERESTED, NOT_INTERESTED, "
             "CALL_BACK, WANTS_PRICE, WANTS_OFFER, COUNTER, ACCEPTED, REJECTED, "
             "NO_RESPONSE, WRONG_NUMBER, DO_NOT_CONTACT",
    )
    production.add_argument(
        "--suggest", action="store_true",
        help="advisory summaries for --property (rule-based; no AI needed)",
    )

    export = parser.add_argument_group("export (Wave 4)")
    export.add_argument("--export-hot", action="store_true", help="export hot leads")
    export.add_argument("--export-top-deals", action="store_true", help="export top deals")
    export.add_argument("--export-watchlist", action="store_true", help="export the watchlist")
    export.add_argument("--export-contacts", action="store_true", help="export contacts")
    export.add_argument("--export-outreach", action="store_true", help="export outreach history")
    export.add_argument("--export-follow-ups", action="store_true", help="export follow-ups")
    export.add_argument("--export-offers", action="store_true", help="export offers")
    export.add_argument("--export-contracts", action="store_true", help="export contracts")
    export.add_argument("--export-buyers", action="store_true", help="export buyers")
    export.add_argument("--export-assignments", action="store_true", help="export assignments")
    export.add_argument(
        "--export-pipeline", action="store_true",
        help="export the full acquisition pipeline, one row per property",
    )
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


def _first_set(*values: Optional[float]) -> Optional[float]:
    """The first value that was actually given. Used to resolve the price band.

    ``--min-price``/``--max-price`` win, then the older ``--max-asking-price``,
    then the configured search range. The range is a BUYER-CAPACITY ceiling,
    not a deal rule — everything inside it is still underwritten normally.
    """
    for value in values:
        if value is not None:
            return value
    return None


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
        min_price=_first_set(args.min_price, MIN_PROPERTY_PRICE),
        max_price=_first_set(
            args.max_price, getattr(args, "max_asking_price", None), MAX_PROPERTY_PRICE
        ),
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

    # Each adapter declares the variables it needs, so the fallback message
    # names the right ones instead of a generic pair. An unknown name falls
    # through to get_provider, which lists what is registered.
    entry = registration(requested)
    missing = entry.missing_settings(settings) if entry else []
    if entry is not None and missing:
        print(NO_PROVIDER_MESSAGE, file=sys.stderr)
        print(
            f"  '{requested}' needs {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        print("  Falling back to the local CSV source for this run.", file=sys.stderr)
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

    budget = HuntBudget.from_api_budget(ApiBudget.from_env())
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


# ---------------------------------------------------------------------------
# Wave 5 — acquisitions
# ---------------------------------------------------------------------------

#: CLI flag -> outreach channel.
LOG_CHANNELS = {
    "log_call": "CALL",
    "log_text": "TEXT",
    "log_email": "EMAIL",
    "log_voicemail": "VOICEMAIL",
    "log_mail": "MAIL",
    "log_note": "OTHER",
}


def _parse_date(raw: Optional[str], label: str) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"{label} must be YYYY-MM-DD, got '{raw}'")


def _acquisition_export(
    args: argparse.Namespace, rows, columns, label: str
) -> List[Path]:
    directory = args.out_dir or DEFAULT_OUTPUT_DIR
    adapters: List[Any] = []
    if args.format in ("csv", "both"):
        adapters.append(CsvAdapter(directory))
    if args.format in ("json", "both"):
        adapters.append(JsonAdapter(directory, meta={"export": label, "count": len(rows)}))
    return publish_all(adapters, rows, columns, label)


def run_acquisitions_cli(args: argparse.Namespace, config: EngineConfig) -> int:
    """Every Wave 5 command: queue, follow-ups, dashboard, daily, deal room,
    outreach logging, offers, contracts, buyers, assignments and exports."""
    store = open_store(args)
    workflow = AcquisitionWorkflow(store, config=config)
    acquisitions = workflow.store
    exit_code = 0
    printed = False
    exports: List[Path] = []

    try:
        today = date.today()

        # --- actions on one property ---------------------------------
        target = args.property or args.deal_room
        if target:
            code, acted = _acquisition_actions(args, workflow, target, config)
            if code:
                return code
            printed = printed or acted

        # --- skip trace ------------------------------------------------
        if args.skip_trace:
            printed = _run_skip_trace(args, workflow) or printed

        # --- buyers ----------------------------------------------------
        if args.add_buyer:
            buyer = acquisitions.save_buyer(
                Buyer(
                    name=args.add_buyer,
                    company=args.buyer_company,
                    email=args.buyer_email,
                    phone=args.buyer_phone,
                    preferred_states=_split(args.buyer_states) or [],
                    property_types=_split(args.buyer_types) or [],
                    min_price=args.buyer_min,
                    max_price=args.buyer_max,
                )
            )
            if not args.quiet:
                print(f"Buyer saved: {buyer.name} ({buyer.price_range()}, "
                      f"{', '.join(buyer.preferred_states) or 'any state'})")
                printed = True

        if args.buyers and not args.quiet:
            print(render_buyers(acquisitions.all_buyers()))
            printed = True

        # --- screens ---------------------------------------------------
        if args.deal_room:
            printed = _render_deal_room(args, workflow, config) or printed

        if args.contact_queue or args.export_contacts:
            entries = workflow.queue_entries(limit=args.limit, today=today)
            if args.contact_queue and not args.quiet:
                print(render_contact_queue(entries, config.target_wholesale_fee))
                printed = True
            if args.export_contacts:
                exports += _acquisition_export(
                    args, contact_rows(acquisitions.all_contacts()),
                    CONTACT_COLUMNS, "contacts",
                )

        if args.follow_ups or args.export_follow_ups:
            buckets = workflow.follow_ups_by_bucket(today)
            if args.follow_ups and not args.quiet:
                print(render_follow_ups(buckets))
                printed = True
            if args.export_follow_ups:
                ordered = buckets["OVERDUE"] + buckets["TODAY"] + buckets["UPCOMING"]
                exports += _acquisition_export(
                    args, follow_up_rows(ordered), FOLLOW_UP_COLUMNS, "follow_ups",
                )

        if args.dashboard and not args.quiet:
            print(render_dashboard(workflow.dashboard(today)))
            printed = True

        if args.daily and not args.quiet:
            print(render_daily(workflow.daily_plan(today), today))
            printed = True

        # --- remaining exports -----------------------------------------
        for flag, rows_fn, columns, label in (
            ("export_outreach", lambda: outreach_rows(acquisitions.all_outreach()),
             OUTREACH_COLUMNS, "outreach"),
            ("export_offers", lambda: offer_rows(acquisitions.all_offers()),
             OFFER_COLUMNS, "offers"),
            ("export_contracts", lambda: contract_rows(acquisitions.all_contracts()),
             CONTRACT_COLUMNS, "contracts"),
            ("export_buyers", lambda: buyer_rows(acquisitions.all_buyers()),
             BUYER_COLUMNS, "buyers"),
            ("export_assignments", lambda: assignment_rows(acquisitions.all_assignments()),
             ASSIGNMENT_COLUMNS, "assignments"),
        ):
            if getattr(args, flag, False):
                exports += _acquisition_export(args, rows_fn(), columns, label)

        if args.export_pipeline:
            entries = workflow.queue_entries(include_closed=True, today=today)
            exports += _acquisition_export(
                args,
                pipeline_rows(entries, acquisitions, config.target_wholesale_fee),
                PIPELINE_COLUMNS, "acquisition_pipeline",
            )

        if exports and not args.quiet:
            print()
            for path in exports:
                print(f"exported -> {path}")
            printed = True

        if not printed and not args.quiet:
            print("Nothing to show. Run --hunt first to populate the lead database.")
        return exit_code
    finally:
        store.close()


def _acquisition_actions(
    args: argparse.Namespace,
    workflow: "AcquisitionWorkflow",
    property_id: str,
    config: EngineConfig,
) -> Tuple[int, bool]:
    """Status moves, outreach logging, offers, contracts and assignments.

    Returns ``(exit_code, acted)``. ``acted`` says whether anything was written
    or printed, so the caller does not follow a completed action with
    "nothing to show".
    """
    acted = False
    row = workflow.leads.find_one(property_id)
    if row is None:
        print(
            f"No stored property matches '{property_id}'. Run --hunt first, or try "
            "part of the address.",
            file=sys.stderr,
        )
        return 1, False

    try:
        follow_up = _parse_date(args.follow_up, "--follow-up")
        closing = _parse_date(args.closing_date, "--closing-date")
        inspection = _parse_date(args.inspection_deadline, "--inspection-deadline")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2, acted

    # --- outreach ------------------------------------------------------
    channel_flag = next((f for f in LOG_CHANNELS if getattr(args, f, False)), None)
    if channel_flag:
        try:
            outcome = Outcome.parse(args.outcome) if args.outcome else None
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2, acted
        activity, messages = workflow.log_outreach(
            row.dedupe_key,
            channel=Channel.parse(LOG_CHANNELS[channel_flag]),
            outcome=outcome,
            notes=args.note or "",
            follow_up=follow_up,
            direction=Direction.INBOUND if args.inbound else Direction.OUTBOUND,
        )
        if not args.quiet and activity is not None:
            print(
                f"Logged {activity.channel}"
                + (f" — {activity.outcome}" if activity.outcome else "")
                + f" on {row.address or row.dedupe_key}. Nothing was sent."
            )
            for message in messages:
                print(f"  {message}")
        acted = True
    elif follow_up and not args.make_offer:
        workflow.store.set_follow_up(
            row.dedupe_key, follow_up, args.follow_up_reason or "scheduled manually"
        )
        workflow.leads.log_activity(
            row.lead_row_id, "follow_up_scheduled",
            f"follow-up {follow_up.isoformat()}"
            + (f": {args.follow_up_reason}" if args.follow_up_reason else ""),
            row.dedupe_key,
        )
        if not args.quiet:
            print(f"Follow-up set for {follow_up.isoformat()}.")
        acted = True

    # --- offers --------------------------------------------------------
    if args.make_offer is not None:
        try:
            status = OfferStatus.parse(args.offer_status)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2, acted
        offer, warnings = workflow.build_offer(
            row.dedupe_key, args.make_offer, notes=args.note or "", status=status
        )
        if not args.quiet and offer is not None:
            print(render_offer(offer, config))
            for warning in warnings:
                print(f"  !! {warning}")
        acted = True

    if args.counter is not None:
        offer, messages = workflow.record_counter(
            row.dedupe_key, args.counter, notes=args.note or ""
        )
        if not args.quiet:
            for message in messages:
                print(message)
            if offer is not None:
                print(render_negotiation(offer, config))
        acted = True

    # --- contract ------------------------------------------------------
    if args.contract or args.contract_status:
        existing = workflow.store.contract_for(row.dedupe_key) or Contract(
            property_id=row.dedupe_key
        )
        if args.purchase_price is not None:
            existing.purchase_price = args.purchase_price
        if existing.contract_date is None:
            existing.contract_date = date.today()
        if inspection:
            existing.inspection_deadline = inspection
        if closing:
            existing.closing_date = closing
        if args.earnest_money is not None:
            existing.earnest_money = args.earnest_money
        if args.assignment_allowed:
            existing.assignment_allowed = args.assignment_allowed == "yes"
        if args.contract_status:
            try:
                existing.status = ContractStatus.parse(args.contract_status)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2, acted
        if args.note:
            existing.notes = f"{existing.notes}\n{args.note}".strip()
        workflow.store.save_contract(existing)
        workflow.set_status(row.dedupe_key, STATUS_UNDER_CONTRACT, "contract recorded")
        if not args.quiet:
            print(
                f"Contract recorded: {existing.status} at "
                f"{money(existing.purchase_price)}. Tracking only — this engine "
                "drafts no documents and gives no legal advice."
            )
        acted = True

    # --- assignment ----------------------------------------------------
    if args.assign or args.assignment_price is not None or args.assignment_status:
        existing = workflow.store.assignment_for(row.dedupe_key) or Assignment(
            property_id=row.dedupe_key
        )
        if args.assign:
            existing.buyer_name = args.assign
            match = next(
                (b for b in workflow.store.all_buyers() if b.name == args.assign), None
            )
            if match:
                existing.buyer_id = match.buyer_id
        if args.assignment_price is not None:
            existing.assignment_price = args.assignment_price
        contract = workflow.store.contract_for(row.dedupe_key)
        if existing.purchase_price is None:
            existing.purchase_price = (
                contract.purchase_price if contract else row.recommended_offer
            )
        if args.assignment_status:
            try:
                existing.status = AssignmentStatus.parse(args.assignment_status)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2, acted
        elif existing.assignment_price is not None and existing.buyer_name:
            existing.status = AssignmentStatus.ASSIGNMENT_SIGNED
            existing.assignment_date = existing.assignment_date or date.today()
        workflow.store.save_assignment(existing)
        workflow.set_status(
            row.dedupe_key,
            STATUS_ASSIGNED if existing.status is AssignmentStatus.ASSIGNMENT_SIGNED
            else STATUS_BUYER_SEARCH,
            "assignment updated",
        )
        if not args.quiet:
            fee = existing.gross_assignment_fee
            print(
                f"Assignment {existing.status}"
                + (f" to {existing.buyer_name}" if existing.buyer_name else "")
                + (f", gross fee {money(fee)}" if fee is not None else "")
            )
        acted = True
    return 0, acted


def _run_skip_trace(args: argparse.Namespace, workflow: "AcquisitionWorkflow") -> bool:
    """Run the configured skip-trace provider, within budget and gates.

    Skip tracing is the most expensive call in the pipeline, so it is never
    automatic for everything: a lead has to clear a quality bar, the batch is
    capped, and a bulk run asks before it spends.
    """
    try:
        provider = get_skip_trace_provider(args.skip_trace_provider)
    except SkipTraceNotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return False

    budget = budget_from_args(args)

    if args.property:
        row = workflow.leads.find_one(args.property)
        targets = [row] if row else []
    else:
        # Only leads that earned it: the score gates, a live status, and no
        # contact route already on file.
        targets = []
        for entry in workflow.skip_trace_candidates():
            row = entry.row
            if budget.qualifies_for_skip_trace(
                lead_score=row.lead_score,
                deal_score=row.deal_score,
                priority_score=row.priority_score,
                status=row.status,
                already_reachable=bool(entry.contact and entry.contact.is_reachable),
            ):
                targets.append(row)

    cap = args.limit or budget.max_skip_traces
    over_budget = max(len(targets) - cap, 0)
    targets = targets[:cap]

    if not targets:
        if not args.quiet:
            print(
                "Nothing qualifies for a skip trace. Gates: "
                + budget.describe_gates()
            )
        return True

    # Say what it will cost before spending it.
    if not args.quiet:
        estimate = budget.estimate("skip_trace", len(targets))
        print(
            f"{len(targets)} lead(s) qualify for a skip trace"
            + (f" ({over_budget} more capped by MAX_SKIP_TRACES={cap})" if over_budget else "")
            + (f", estimated cost ${estimate:,.2f}." if estimate else
               ", cost unknown — set cost_per_skip_trace from your vendor's pricing.")
        )

    # Bulk runs ask first, unless automatic mode was turned on deliberately.
    if len(targets) > 1 and not budget.auto_skip_trace and not args.property:
        if not confirm(f"Skip trace {len(targets)} lead(s)?", args.yes):
            print(
                "Cancelled — nothing was traced. Pass --yes, or --auto-skip-trace "
                "to stop asking.",
                file=sys.stderr,
            )
            return False

    if provider.is_test_provider and not args.quiet:
        print(
            "WARNING: the mock skip-trace provider returns FICTIONAL TEST DATA. "
            "Reserved 555-01xx numbers and .invalid addresses. Do not dial or "
            "email anything it produces."
        )

    found = 0
    for row in targets:
        try:
            result = provider.skip_trace(
                property_id=row.dedupe_key,
                owner_name=None,
                address=row.address,
                city=row.city,
                state=row.state,
                zip_code=row.zip_code,
            )
        except SkipTraceNotConfigured as exc:
            print(str(exc), file=sys.stderr)
            return False
        contact = workflow.store.save_contact(result.to_contact(row.dedupe_key))
        workflow.leads.log_activity(
            row.lead_row_id, "skip_trace_run",
            f"{provider.name}: "
            + ("contact found" if contact.is_reachable else "no contact found")
            + (" (TEST DATA)" if contact.is_test_data else ""),
            row.dedupe_key,
        )
        if contact.is_reachable:
            found += 1
            workflow.set_status(row.dedupe_key, STATUS_CONTACT_READY, "skip trace returned contact")
        if not args.quiet:
            print(
                f"  {row.address or row.dedupe_key:<34}"
                f"{contact.display_phone():<18}{contact.display_email():<28}"
                f"{contact.provenance}"
            )
    if not args.quiet:
        print(
            f"{found} of {len(targets)} lookup(s) returned a contact "
            f"({provider.lookups} lookup(s) billed at ${provider.cost_per_lookup:.2f} each)."
        )
    return True


def _render_deal_room(
    args: argparse.Namespace, workflow: "AcquisitionWorkflow", config: EngineConfig
) -> bool:
    row = workflow.leads.find_one(args.deal_room)
    if row is None:
        print(f"No stored property matches '{args.deal_room}'.", file=sys.stderr)
        return False
    if args.quiet:
        return True

    acquisitions = workflow.store
    contact = acquisitions.best_contact(row.dedupe_key)
    entries = [e for e in workflow.queue_entries(include_closed=True)
               if e.row.dedupe_key == row.dedupe_key]
    acquisition_priority = entries[0].priority if entries else None

    live = _live_analysis(args, row, config)
    analysis = research = priority = None
    if live is not None:
        result, research, priority = live
        analysis = result.analysis

    print(
        render_deal_room(
            row=row,
            contact=contact,
            outreach=acquisitions.outreach_for(row.dedupe_key),
            offers=acquisitions.offers_for(row.dedupe_key),
            contract=acquisitions.contract_for(row.dedupe_key),
            assignment=acquisitions.assignment_for(row.dedupe_key),
            research=research,
            priority=priority,
            acquisition_priority=acquisition_priority,
            analysis=analysis,
            notes=workflow.leads.notes(row.lead_row_id),
            config=config,
        )
    )
    return True


def render_offer(offer, config: EngineConfig) -> str:
    """The offer as recorded, against the underwriting behind it."""
    lines = [
        "-" * 78,
        f"OFFER RECORDED — {money(offer.offer_amount)}  [{offer.offer_status}]",
        "-" * 78,
        f"  {'Asking price:':<28}{money(offer.current_price)}",
        f"  {'ARV:':<28}{money(offer.arv)}",
        f"  {'Repairs:':<28}{money(offer.repairs)}",
        f"  {'End-buyer ceiling:':<28}{money(offer.end_buyer_ceiling)}",
        f"  {'MAO:':<28}{money(offer.mao)}",
        f"  {'Your offer:':<28}{money(offer.offer_amount)}",
        f"  {'Distance to MAO:':<28}{money(offer.distance_to_mao)}",
        f"  {'Target wholesale fee:':<28}{money(offer.target_wholesale_fee)}",
        f"  {'Potential wholesale fee:':<28}{money(offer.potential_wholesale_fee)}",
    ]
    gap = offer.distance_to_target_fee
    if gap is not None:
        lines.append(
            f"  {'Distance to target fee:':<28}{money(gap)}"
            + ("  (below target — a label, not a rejection)" if gap < 0 else "")
        )
    return "\n".join(lines)


def render_negotiation(offer, config: EngineConfig) -> str:
    """Where the negotiation stands after a counter."""
    return "\n".join([
        "-" * 78,
        "NEGOTIATION",
        "-" * 78,
        f"  {'Your offer:':<28}{money(offer.offer_amount)}",
        f"  {'Seller counter:':<28}{money(offer.seller_counter)}",
        f"  {'Current price:':<28}{money(offer.current_proposed_price)}",
        f"  {'MAO:':<28}{money(offer.mao)}",
        f"  {'Distance to MAO:':<28}{money(offer.distance_to_mao)}",
        f"  {'Potential wholesale fee:':<28}{money(offer.fee_at_current_price)}",
        f"  {'Target wholesale fee:':<28}{money(offer.target_wholesale_fee)}",
        f"  {'Distance to target fee:':<28}{money(offer.distance_to_target_fee)}",
    ])


def render_buyers(buyers) -> str:
    lines = ["=" * 110, "BUYER LIST", "=" * 110,
             f"{'NAME':<24}{'COMPANY':<24}{'STATES':<14}{'TYPES':<24}{'PRICE RANGE':<20}",
             "-" * 110]
    if not buyers:
        lines.append("  No buyers yet. Add one with --add-buyer NAME.")
    for buyer in buyers:
        lines.append(
            f"{buyer.name[:23]:<24}{buyer.company[:23]:<24}"
            f"{', '.join(buyer.preferred_states)[:13] or 'any':<14}"
            f"{', '.join(buyer.property_types)[:23] or 'any':<24}"
            f"{buyer.price_range():<20}"
        )
    lines.append("=" * 110)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wave 6 — production
# ---------------------------------------------------------------------------


def budget_from_args(args: argparse.Namespace) -> ApiBudget:
    """Defaults, then environment, then explicit --max-* flags."""
    return ApiBudget.from_env(
        max_raw_leads=args.max_raw_leads,
        max_research=args.max_research,
        max_comps=args.max_comps,
        max_skip_traces=args.max_skip_traces,
        auto_skip_trace=args.auto_skip_trace or None,
    )


def runtime_from_args(args: argparse.Namespace) -> RuntimeConfig:
    """Resolve the run mode and every provider slot."""
    overrides = {}
    if getattr(args, "source", None) and args.source != "csv":
        overrides["DATA_PROVIDER"] = args.source
    if getattr(args, "skip_trace_provider", None) and args.skip_trace_provider != "none":
        overrides["SKIP_TRACE_PROVIDER"] = args.skip_trace_provider
    return RuntimeConfig.from_env(
        mode=args.mode, overrides=overrides, auto_confirm=args.yes
    )


def confirm(prompt: str, auto_yes: bool) -> bool:
    """Ask before spending money. ``--yes`` answers for you."""
    if auto_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        # Non-interactive (cron, CI): never assume yes for a billable action.
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run_status_cli(args: argparse.Namespace, runtime: RuntimeConfig) -> int:
    """``--provider-status`` / ``--integrations`` / ``--health`` /
    ``--security-audit`` / ``--budget-status``."""
    printed = False
    if args.provider_status:
        print(capability_matrix(runtime.settings))
        print()
        # What is confirmed about the one real vendor wired in, and what still
        # needs its documentation read. Printed so the gap is never a surprise.
        print(propertyreach_schema_status())
        print()
        print(skip_trace_status())
        printed = True
    if args.integrations:
        if printed:
            print()
        print(integration_status())
        printed = True
    if args.health:
        if printed:
            print()
        csv_path = args.leads or SAMPLE_LEADS
        print(health_report(runtime.settings, csv_path, args.lead_comps))
        printed = True
    if args.budget_status:
        if printed:
            print()
        print(budget_from_args(args).render())
        printed = True
    if args.security_audit:
        if printed:
            print()
        findings = audit_source()
        print(render_audit(findings))
        return 1 if any(f.severity == "HIGH" for f in findings) else 0
    return 0 if printed else 0


def run_production_cli(
    args: argparse.Namespace, config: EngineConfig, runtime: RuntimeConfig
) -> int:
    """``--daily``, ``--monitor``, ``--backup``, ``--import``, ``--sheets``."""
    budget = budget_from_args(args)
    store = open_store(args)
    exit_code = 0
    printed = False

    try:
        acquisitions = AcquisitionStore(store)
        workflow = AcquisitionWorkflow(store, acquisitions, config=config)

        # --- per-property actions --------------------------------------
        if args.property and (args.response or args.suggest):
            row = store.find_one(args.property)
            if row is None:
                print(
                    f"No stored property matches '{args.property}'.", file=sys.stderr
                )
                return 1
            if args.response:
                try:
                    recorded = acquisitions.record_seller_response(
                        row.dedupe_key, args.response, args.note or ""
                    )
                except ValueError as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
                from wholesale_engine.acquisitions.models import RESPONSE_STATUS

                suggested = RESPONSE_STATUS.get(recorded)
                if suggested:
                    workflow.set_status(
                        row.dedupe_key, suggested, f"seller response {recorded}"
                    )
                if not args.quiet:
                    print(f"Seller response recorded: {recorded}")
                    if suggested:
                        print(f"  Status -> {suggested}")
                    if recorded == "DO_NOT_CONTACT":
                        print(
                            "  Every contact method for this property is now "
                            "suppressed and will never be contacted again."
                        )
                printed = True
            if args.suggest and not args.quiet:
                print(_render_suggestions(workflow, row, config, runtime))
                printed = True

        # --- backup -----------------------------------------------------
        if args.backup:
            result = create_backup(
                database=args.db or DEFAULT_DB_PATH,
                destination_dir=args.backup_dir or (DEFAULT_OUTPUT_DIR / "backups"),
                reports_dir=args.out_dir or DEFAULT_OUTPUT_DIR,
                include_secrets=args.include_secrets,
            )
            if not args.quiet:
                print(result.render())
            printed = True

        # --- import -----------------------------------------------------
        if args.import_kind:
            try:
                path = safe_path(args.file, label="--file")
                result = run_import(args.import_kind, path, store, acquisitions)
            except (ValidationError, ImportError_) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if not args.quiet:
                print(result.render())
            printed = True

        # --- monitor ----------------------------------------------------
        if args.monitor and not args.quiet:
            print(render_monitor(monitor(store, args.limit)))
            printed = True

        # --- the daily run ----------------------------------------------
        if args.daily:
            provider = None
            if not args.no_ingest:
                try:
                    provider = get_provider(
                        runtime.data_provider,
                        settings=runtime.settings,
                        csv_path=args.leads or SAMPLE_LEADS,
                        comps_path=args.lead_comps or (
                            SAMPLE_LEAD_COMPS if not args.leads else None
                        ),
                    )
                except ProviderNotConfigured as exc:
                    print(f"WARNING: {exc}", file=sys.stderr)
                    print(
                        "Continuing without ingestion — reporting on stored leads only.",
                        file=sys.stderr,
                    )

            notifications = NotificationCenter.build(
                runtime.slot("NOTIFICATION_PROVIDER")
            )
            report = run_daily(
                store,
                provider=provider,
                criteria=criteria_from_args(args, lead_config_from_args(args)),
                engine_config=config,
                lead_config=lead_config_from_args(args),
                budget=budget,
                runtime=runtime,
                notifications=notifications,
                ingest=provider is not None,
            )
            if not args.quiet:
                print(render_daily_report(report))
                print()
                print(render_priority(report.priorities))
            paths = _export_daily(args, report, config)
            report.exports = paths
            if paths and not args.quiet:
                print()
                for path in paths:
                    print(f"exported -> {path}")
            printed = True

        # --- sheets -----------------------------------------------------
        if args.sheets:
            printed = _publish_sheets(args, workflow, config, runtime) or printed

        if not printed and not args.quiet:
            print("Nothing to do. Try --daily, --dashboard or --provider-status.")
        return exit_code
    finally:
        store.close()


def _render_suggestions(
    workflow, row, config: EngineConfig, runtime: RuntimeConfig
) -> str:
    """Advisory summaries for one property. Never acts on anything."""
    writer = get_note_writer(runtime.slot("AI_PROVIDER"))
    contact = workflow.store.best_contact(row.dedupe_key)
    entries = [
        e for e in workflow.queue_entries(include_closed=True)
        if e.row.dedupe_key == row.dedupe_key
    ]
    priority = entries[0].priority if entries else None
    context = {
        "property_id": row.dedupe_key,
        "owner_name": contact.owner_name if contact else None,
        "contact_attempts": contact.contact_attempts if contact else 0,
        "last_outcome": contact.last_outcome if contact else None,
        "next_follow_up": (
            contact.next_follow_up.isoformat()
            if contact and contact.next_follow_up else None
        ),
        "is_test_data": contact.is_test_data if contact else False,
        "seller_response": workflow.store.latest_seller_response(row.dedupe_key),
        "arv": row.arv,
        "repairs": row.repair_estimate,
        "mao": row.mao,
        "asking_price": row.asking_price,
        "potential_fee": row.potential_fee,
        "target_fee": config.target_wholesale_fee,
        "next_action": str(priority.action) if priority else None,
        "action_reason": priority.reason if priority else None,
    }
    blocks = [f"ADVISORY SUMMARIES — {row.address or row.dedupe_key}", "=" * 78]
    for suggestion in writer.all_suggestions(context):
        blocks.append("")
        blocks.append(suggestion.render())
    return "\n".join(blocks)


def _export_daily(args: argparse.Namespace, report, config: EngineConfig) -> List[Path]:
    """Write the daily report as CSV and JSON."""
    directory = args.out_dir or DEFAULT_OUTPUT_DIR
    rows = [item.as_dict() for item in report.priorities]
    columns = [
        "band", "action", "property_id", "address", "reason",
        "deal_score", "lead_score", "priority_score",
        "next_deadline", "days_to_deadline",
    ]
    adapters: List[Any] = []
    if args.format in ("csv", "both"):
        adapters.append(CsvAdapter(directory))
    if args.format in ("json", "both"):
        adapters.append(JsonAdapter(directory, meta=report.as_dict()))
    return publish_all(adapters, rows, columns, "daily_report")


def _publish_sheets(
    args: argparse.Namespace, workflow, config: EngineConfig, runtime: RuntimeConfig
) -> bool:
    """Publish the sheet tabs through whatever SHEETS_PROVIDER names."""
    from wholesale_engine.integrations import IntegrationNotConfigured
    from wholesale_engine.reports.acquisition_exports import (
        PIPELINE_COLUMNS, offer_rows, pipeline_rows, OFFER_COLUMNS,
    )
    from wholesale_engine.reports.acquisitions import (
        CONTACT_QUEUE_COLUMNS, FOLLOW_UP_COLUMNS, contact_queue_rows, follow_up_rows,
    )
    from wholesale_engine.reports.deal_tables import DEAL_COLUMNS, deal_rows

    name = runtime.slot("SHEETS_PROVIDER")
    adapter = get_sheets_adapter(name, args.out_dir or DEFAULT_OUTPUT_DIR)
    if adapter is None:
        print(
            "SHEETS_PROVIDER is not set. Use 'local' to write the same tabs as "
            "CSV files, or 'google' once you have a service account.",
            file=sys.stderr,
        )
        return False

    entries = workflow.queue_entries()
    buckets = workflow.follow_ups_by_bucket()
    datasets = (
        ("hot_leads", deal_rows(workflow.leads.hot_leads()), DEAL_COLUMNS),
        ("contact_queue", contact_queue_rows(entries), CONTACT_QUEUE_COLUMNS),
        (
            "follow_ups",
            follow_up_rows(buckets["OVERDUE"] + buckets["TODAY"] + buckets["UPCOMING"]),
            FOLLOW_UP_COLUMNS,
        ),
        ("offers", offer_rows(workflow.store.all_offers()), OFFER_COLUMNS),
        (
            "pipeline",
            pipeline_rows(entries, workflow.store, config.target_wholesale_fee),
            PIPELINE_COLUMNS,
        ),
    )
    for tab, rows, columns in datasets:
        try:
            result = adapter.publish(tab, rows, columns)
        except (IntegrationNotConfigured, NotImplementedError) as exc:
            print(f"{tab}: {exc}", file=sys.stderr)
            return False
        if not args.quiet:
            print(result.render())
    return True


def render_all(results: List[AnalysisResult], config: EngineConfig) -> str:
    return "\n\n".join(render_result(result, config) for result in results)


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # --- resolve the mode before anything reads data ---------------------
    try:
        runtime = runtime_from_args(args)
        runtime.assert_live_ready()
    except (ValueError, ModeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    status_command = bool(
        args.provider_status or args.integrations or args.health
        or args.security_audit or args.budget_status
    )
    if status_command:
        return run_status_cli(args, runtime)

    if args.list_sources:
        print(describe_sources(runtime.settings))
        return 0

    production_command = bool(
        args.daily or args.monitor or args.backup or args.import_kind
        or args.sheets or args.response or args.suggest
    )

    acquisition_command = bool(
        args.contact_queue or args.follow_ups or args.dashboard or args.daily
        or args.deal_room or args.skip_trace or args.buyers or args.add_buyer
        or args.make_offer is not None or args.counter is not None
        or args.contract or args.contract_status or args.assign
        or args.assignment_price is not None or args.assignment_status
        or any(getattr(args, flag, False) for flag in LOG_CHANNELS)
        or args.export_contacts or args.export_outreach or args.export_follow_ups
        or args.export_offers or args.export_contracts or args.export_buyers
        or args.export_assignments or args.export_pipeline
        or (args.property and args.follow_up)
    )
    # --daily is a production command now, not the Wave 5 plan renderer.
    acquisition_command = acquisition_command and not args.daily

    database_command = bool(
        args.search or args.top_deals or args.hot_leads or args.watchlist
        or args.property or args.activity or args.export_hot
        or args.export_top_deals or args.export_watchlist
    )

    hunting = bool(args.leads or args.sample_leads)
    if not (
        args.sample or args.csv or args.json or hunting or args.hunt
        or database_command or acquisition_command or production_command
    ):
        build_parser().print_help()
        print(
            "\nNothing to analyze. Start with:  --sample  (Wave 1),  "
            "--sample-leads  (Wave 2),  --hunt --source csv  (Wave 4),  "
            "then  --dashboard  /  --daily  /  --contact-queue  /  --deal-room <id>",
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

    if not args.quiet and (production_command or args.hunt):
        print(runtime.banner(), file=sys.stderr)

    if production_command and not args.hunt:
        return run_production_cli(args, config, runtime)

    if acquisition_command and not args.hunt:
        return run_acquisitions_cli(args, config)

    if database_command and not args.hunt:
        return run_database_cli(args, config)

    if args.hunt:
        code = run_hunt_cli(args, config)
        if code == 0 and production_command:
            return run_production_cli(args, config, runtime)
        if code == 0 and acquisition_command:
            return run_acquisitions_cli(args, config)
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
