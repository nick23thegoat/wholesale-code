"""Report generation: human-readable text and flat CSV export."""

from .csv_report import CSV_COLUMNS, DETAIL_COLUMNS, result_to_row, write_csv
from .lead_report import (
    LEAD_DETAIL_COLUMNS,
    LEAD_PIPELINE_COLUMNS,
    lead_result_to_row,
    render_lead_summary,
    write_hot_leads_csv,
    write_lead_pipeline_csv,
)
from .text_report import DISCLAIMER, render_batch_summary, render_result

__all__ = [
    "CSV_COLUMNS",
    "DETAIL_COLUMNS",
    "DISCLAIMER",
    "LEAD_DETAIL_COLUMNS",
    "LEAD_PIPELINE_COLUMNS",
    "lead_result_to_row",
    "render_lead_summary",
    "write_hot_leads_csv",
    "write_lead_pipeline_csv",
    "render_batch_summary",
    "render_result",
    "result_to_row",
    "write_csv",
]
