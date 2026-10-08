"""Complete CSV export for historical cases, shared by API and Gradio."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable

from app.case_queries import MAX_PAGE_SIZE, CaseRecord


CaseSearcher = Callable[..., list[CaseRecord]]

HEADERS = (
    "case_code", "merchant_name", "city", "industry", "platform", "case_type",
    "start_date", "end_date", "store_count_at_case", "current_store_count",
    "metric_code", "metric_name", "before_period", "before_value", "before_text",
    "after_period", "after_value", "after_text", "unit", "note",
)


def build_case_csv(searcher: CaseSearcher, *, city: str | None, industry: str | None) -> bytes:
    """Page through every matching case so export is not silently capped at 100."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    offset = 0
    while True:
        cases = searcher(city=city, industry=industry, limit=MAX_PAGE_SIZE, offset=offset)
        for case in cases:
            for metric in case.metrics.values() or [None]:
                writer.writerow((
                    case.case_code, case.merchant_name, case.city, case.industry,
                    case.platform, case.case_type, case.start_date.isoformat(),
                    case.end_date.isoformat() if case.end_date else "",
                    case.store_count_at_case, case.current_store_count,
                    metric.metric_code if metric else "",
                    metric.metric_name if metric else "",
                    metric.before_period if metric else "",
                    metric.before_value if metric and metric.before_value is not None else "",
                    metric.before_text if metric else "",
                    metric.after_period if metric else "",
                    metric.after_value if metric and metric.after_value is not None else "",
                    metric.after_text if metric else "",
                    metric.unit if metric else "",
                    metric.note if metric else "",
                ))
        if len(cases) < MAX_PAGE_SIZE:
            break
        offset += len(cases)
    return buffer.getvalue().encode("utf-8-sig")
