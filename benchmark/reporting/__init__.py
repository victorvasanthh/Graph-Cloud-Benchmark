"""Turning raw iteration timings into tables, charts and integrity checks."""

from .consistency import ConsistencyIssue, check_row_agreement, summarise_issues
from .summary import build_summary, write_summary
from .tables import (
    render_concurrency_table,
    render_footnotes,
    render_ingest_table,
    render_latency_table,
    render_status_table,
)

__all__ = [
    "ConsistencyIssue",
    "build_summary",
    "check_row_agreement",
    "render_concurrency_table",
    "render_footnotes",
    "render_ingest_table",
    "render_latency_table",
    "render_status_table",
    "summarise_issues",
    "write_summary",
]
