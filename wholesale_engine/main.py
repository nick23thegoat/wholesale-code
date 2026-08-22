"""Command-line entry point for the wholesale acquisition engine.

Examples::

    python -m wholesale_engine.main --sample
    python -m wholesale_engine.main --csv my_leads.csv --comps my_comps.csv
    python -m wholesale_engine.main --csv leads.csv --out out.csv --report deals.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):  # allow `python wholesale_engine/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wholesale_engine.analysis import analyze_properties  # noqa: E402
from wholesale_engine.config import DEFAULT_CONFIG, EngineConfig  # noqa: E402
from wholesale_engine.data.csv_loader import (  # noqa: E402
    LoadReport,
    load_properties_csv,
    load_properties_json,
)
from wholesale_engine.models.results import AnalysisResult  # noqa: E402
from wholesale_engine.reports import (  # noqa: E402
    render_batch_summary,
    render_result,
    write_csv,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
SAMPLE_PROPERTIES = PACKAGE_ROOT / "data" / "sample_properties.csv"
SAMPLE_COMPS = PACKAGE_ROOT / "data" / "sample_comps.csv"
DEFAULT_OUTPUT = PACKAGE_ROOT / "reports" / "output" / "deal_analysis.csv"


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


def render_all(results: List[AnalysisResult], config: EngineConfig) -> str:
    return "\n\n".join(render_result(result, config) for result in results)


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not (args.sample or args.csv or args.json):
        build_parser().print_help()
        print("\nNothing to analyze. Start with:  --sample", file=sys.stderr)
        return 2

    for path in (args.csv, args.comps, args.json):
        if path and not Path(path).exists():
            print(f"Input file not found: {path}", file=sys.stderr)
            return 2

    config = EngineConfig(
        arv_percentage=args.arv_pct / 100.0,
        wholesale_fee=args.fee,
        min_acceptable_spread=args.fee,
    )

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
