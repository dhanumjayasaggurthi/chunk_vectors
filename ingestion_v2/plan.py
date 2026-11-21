"""Deterministic bounded page-range planning and explicit policy validation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractionPolicy:
    range_pages: int = 32
    native_text_min_chars: int = 100
    cpu_processes: int = 2
    gcp_workers: int = 2
    ocr_dpi: int = 150
    max_attempts: int = 5
    lease_seconds: int = 600

    def __post_init__(self):
        if not 1 <= self.range_pages <= 500:
            raise ValueError("range_pages must be 1..500")
        if not 0 <= self.native_text_min_chars <= 5000:
            raise ValueError("native_text_min_chars must be 0..5000")
        if not 1 <= self.cpu_processes <= 128:
            raise ValueError("cpu_processes must be 1..128")
        if not 1 <= self.gcp_workers <= 128:
            raise ValueError("gcp_workers must be 1..128")
        if not 72 <= self.ocr_dpi <= 300:
            raise ValueError("ocr_dpi must be 72..300")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be 1..20")
        if not 60 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be 60..3600")


def page_ranges(page_count: int, range_pages: int = 32):
    """Return zero-based half-open ranges, never loading the PDF into RAM."""
    if not isinstance(page_count, int) or page_count < 0:
        raise ValueError("page_count must be a nonnegative integer")
    if not isinstance(range_pages, int) or not 1 <= range_pages <= 500:
        raise ValueError("range_pages must be 1..500")
    return [(start, min(page_count, start + range_pages))
            for start in range(0, page_count, range_pages)]
