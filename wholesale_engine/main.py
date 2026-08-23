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
from typing import List, Optional

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
from wholesale_engine.models.results import AnalysisResult  # noqa: E402
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


def render_all(results: List[AnalysisResult], config: EngineConfig) -> str:
    return "\n\n".join(render_result(result, config) for result in results)


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    hunting = bool(args.leads or args.sample_leads)
    if not (args.sample or args.csv or args.json or hunting):
        build_parser().print_help()
        print(
            "\nNothing to analyze. Start with:  --sample  (Wave 1)  or  "
            "--sample-leads  (Wave 2)",
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
        min_acceptable_spread=args.fee,
    )

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
