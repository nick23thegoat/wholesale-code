"""Where the engine's bundled files and default outputs live.

These were constants inside ``main.py``, which meant anything that was not the
CLI could not find them without importing argparse. They live here now so the
service layer, a scheduled job and a web request all resolve the same paths.
``main.py`` re-exports them, so existing references keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: Bundled FICTIONAL sample data. Never real property records.
SAMPLE_PROPERTIES = PACKAGE_ROOT / "data" / "sample_properties.csv"
SAMPLE_COMPS = PACKAGE_ROOT / "data" / "sample_comps.csv"
SAMPLE_LEADS = PACKAGE_ROOT / "data" / "lead_sources" / "sample_leads.csv"
SAMPLE_LEAD_COMPS = PACKAGE_ROOT / "data" / "lead_sources" / "sample_lead_comps.csv"

DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "reports" / "output"
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "deal_analysis.csv"
DEFAULT_LEAD_OUTPUT = DEFAULT_OUTPUT_DIR / "lead_pipeline.csv"
DEFAULT_HOT_OUTPUT = DEFAULT_OUTPUT_DIR / "hot_leads.csv"
