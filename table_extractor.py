"""
table_extractor.py
==================
Extracts tables from PDF pages using pdfplumber and converts them into
clean, embedding-friendly text representations.

For each table on a page it:
  1. Extracts raw cell data (pdfplumber)
  2. Cleans and normalises cell text (strip ¶, whitespace, line breaks)
  3. Detects header rows automatically
  4. Serialises to Markdown table  (default) or CSV fallback
  5. Appends a plain-prose summary via GPT-4o  (optional, for better RAG)
  6. Returns TableResult objects consumed by chunker.py

Handles:
  - Multi-line cells          (line breaks within a cell)
  - Merged/spanned cells      (None placeholders from pdfplumber)
  - Tables split across pages  (caller passes consecutive pages; we stitch)
  - Completely empty tables    (skipped, logged)
  - Nested tables              (outer table only; inner detected separately)
  - Corrupt / extraction fail  (returns empty, logs, pipeline continues)

Dependencies:
    pip install pdfplumber
"""

from __future__ import annotations

import re
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Optional

from config import TABLE_SETTINGS, TABLE_FORMAT, CHUNK_MIN_CHARS
from logger import get_logger

logger = get_logger("epod.table_extractor")

# Lazy import
def _pdfplumber():
    import pdfplumber
    return pdfplumber


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TableResult:
    """Extracted and serialised table from one page."""
    page_number:   int
    table_index:   int          # 0-based index within the page
    row_count:     int
    col_count:     int
    has_header:    bool
    markdown:      str          # Markdown table text
    plain_text:    str          # key=value prose fallback
    summary:       str          # GPT-4o prose summary (if enabled)
    raw_cells:     list[list[str]] = field(default_factory=list, repr=False)
    success:       bool         = True
    error:         str          = ""
    bbox:          tuple | None = None  # pdfplumber (x0,top,x1,bottom)
    source_table:  dict | None = None  # verbatim PDF table grid and cell geometry

    @property
    def best_text(self) -> str:
        """Return the richest available text representation."""
        parts = []
        if self.markdown:
            parts.append(self.markdown)
        if self.summary:
            parts.append(f"\n[TABLE SUMMARY]\n{self.summary}")
        return "\n".join(parts) if parts else self.plain_text


# ─────────────────────────────────────────────────────────────────────────────
# Cell cleaning
# ─────────────────────────────────────────────────────────────────────────────

def _json_bytes(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",",":"),
        allow_nan=False,
    ).encode("utf-8")


def build_source_table(raw_table, table, *, max_json_bytes: int):
    """Preserve pdfplumber's raw matrix and per-cell geometry, without inference.

    None, empty strings, whitespace, newlines and long cell text are retained.
    No fill-forward, empty-row deletion or truncation. Derived Markdown is
    independent. Oversized source tables fail explicitly rather than truncate.
    """
    if type(max_json_bytes) is not int or not 100_000 <= max_json_bytes <= 100_000_000:
        raise ValueError("Invalid table source JSON byte budget")
    if not isinstance(raw_table, list) or not raw_table:
        raise ValueError("Missing source cell matrix")
    cells = []
    for row in raw_table:
        if not isinstance(row, list):
            raise ValueError("Invalid source table row")
        if any(c is not None and not isinstance(c, str) for c in row):
            raise ValueError("Source cells must be text or null")
        cells.append(list(row))
    rows = list(table.rows)
    if len(rows) != len(cells):
        raise ValueError("Table geometry row count differs from source cells")
    geometry = []
    for raw_row, row in zip(cells, rows):
        if len(raw_row) != len(row.cells):
            raise ValueError("Table geometry column count differs from source cells")
        points = []
        for bbox in row.cells:
            if bbox is None:
                points.append(None)
            else:
                v = [float(x) for x in bbox]
                if (len(v) != 4 or not all(math.isfinite(x) for x in v)
                        or v[2] < v[0] or v[3] < v[1]):
                    raise ValueError("Invalid source cell bounding box")
                points.append(v)
        geometry.append(points)
    data = {
        "format": "pdfplumber_cells_v1",
        "source_cells": cells,
        "cell_bboxes": geometry,
        "row_count": len(cells),
        "max_columns": max(map(len,cells),default=0),
        "sha256": hashlib.sha256(_json_bytes(cells)).hexdigest(),
    }
    if len(_json_bytes(data)) > max_json_bytes:
        raise ValueError("TABLE_SOURCE_BUDGET_EXCEEDED: refuse to truncate original cells")
    return data


def validate_source_table(data, *, max_json_bytes=100_000_000):
    """Validate source grid at the persistence and read boundaries."""
    if not isinstance(data,dict) or set(data) != {
        "format","source_cells","cell_bboxes","row_count","max_columns","sha256"
    } or data["format"]!="pdfplumber_cells_v1":
        raise ValueError("Invalid source table representation")
    cells=data["source_cells"]
    boxes=data["cell_bboxes"]
    if (not isinstance(cells,list) or not cells or not isinstance(boxes,list)
            or len(cells)!=len(boxes)
            or type(data["row_count"]) is not int
            or data["row_count"]!=len(cells)
            or type(data["max_columns"]) is not int
            or data["max_columns"]!=max(map(len,cells),default=0)):
        raise ValueError("Invalid source table grid dimensions")
    for row,br in zip(cells,boxes):
        if (not isinstance(row,list) or not isinstance(br,list)
                or len(row)!=len(br)
                or any(c is not None and not isinstance(c,str) for c in row)):
            raise ValueError("Invalid source table cell row")
        for b in br:
            if b is not None and (
                not isinstance(b,list) or len(b)!=4 or
                any(type(x) not in (int,float) or not math.isfinite(x) for x in b) or
                b[0]>b[2] or b[1]>b[3]
            ):
                raise ValueError("Invalid source table cell geometry")
    if hashlib.sha256(_json_bytes(cells)).hexdigest()!=data["sha256"]:
        raise ValueError("Source table checksum mismatch")
    if len(_json_bytes(data))>max_json_bytes:
        raise ValueError("Source table exceeds permitted JSON budget")
    return data


def _clean_cell(raw, *, truncate: bool = True) -> str:
    """
    Normalise a single table cell value.
    - None / empty → empty string
    - Remove ¶ paragraph markers
    - Collapse internal whitespace and line breaks to single space
    - Strip leading/trailing whitespace
    - Truncate extremely long cells (>500 chars) to avoid bloating embeddings
    """
    if raw is None:
        return ""
    text = str(raw)
    text = text.replace("¶", " ")
    text = text.replace("\u00ad", "")          # soft hyphen
    text = re.sub(r"[\r\n\t]+", " ", text)    # internal newlines → space
    text = re.sub(r" {2,}", " ", text)         # multiple spaces → one
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)  # control chars
    text = text.strip()
    if truncate and len(text) > 500:
        text = text[:497] + "…"
    return text


def _clean_row(row: list, *, truncate: bool = True) -> list[str]:
    return [_clean_cell(cell, truncate=truncate) for cell in row]


def _is_empty_row(row: list[str]) -> bool:
    return all(c == "" for c in row)


def _fill_merged_cells(rows: list[list[str]]) -> list[list[str]]:
    """
    pdfplumber uses None for merged/spanned cells.
    Strategy: carry forward the last non-empty value in each column
    so the cell content is still meaningful.
    """
    if not rows:
        return rows
    col_count = max(len(r) for r in rows)
    last_seen = [""] * col_count
    filled = []
    for row in rows:
        # Pad short rows
        row = list(row) + [""] * (col_count - len(row))
        new_row = []
        for i, cell in enumerate(row):
            if cell == "" and i < len(last_seen):
                new_row.append(last_seen[i])   # carry forward
            else:
                new_row.append(cell)
                last_seen[i] = cell
        filled.append(new_row)
    return filled


# ─────────────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_header_row(rows: list[list[str]]) -> bool:
    """
    Heuristic: first row is a header if:
      - All cells are non-empty, AND
      - Cells are shorter than body rows (typically labels, not data), AND
      - OR first row has distinctly different content style from row 2+
    """
    if len(rows) < 2:
        return False
    first = rows[0]
    rest  = rows[1:]

    # All non-empty
    if any(c == "" for c in first):
        return False

    # First row average length vs body average
    first_avg = sum(len(c) for c in first) / max(len(first), 1)
    body_avg  = sum(
        sum(len(c) for c in row) / max(len(row), 1)
        for row in rest
    ) / max(len(rest), 1)

    # Headers are usually shorter than data cells
    if first_avg < body_avg * 1.5 and first_avg < 60:
        return True

    # Check if first row contains typical header-like words
    header_signals = re.compile(
        r"\b(id|no|name|date|type|code|status|value|total|count|"
        r"description|category|item|ref|result|unit|parameter|"
        r"subject|dose|group|visit|day|week|month|year)\b",
        re.I,
    )
    header_matches = sum(
        1 for c in first if header_signals.search(c)
    )
    if header_matches >= len(first) // 2:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Serialisers
# ─────────────────────────────────────────────────────────────────────────────

def _to_markdown(rows: list[list[str]], has_header: bool) -> str:
    """
    Convert cleaned rows to a Markdown table.

    | Col1 | Col2 | Col3 |
    |------|------|------|
    | val1 | val2 | val3 |
    """
    if not rows:
        return ""

    col_count = max(len(r) for r in rows)

    # Pad all rows to same width
    padded = [r + [""] * (col_count - len(r)) for r in rows]

    # Compute column widths
    widths = [
        max(len(padded[ri][ci]) for ri in range(len(padded)))
        for ci in range(col_count)
    ]
    widths = [max(w, 3) for w in widths]   # minimum width 3

    def _fmt_row(row: list[str]) -> str:
        cells = [row[i].ljust(widths[i]) for i in range(col_count)]
        return "| " + " | ".join(cells) + " |"

    lines = []
    if has_header:
        lines.append(_fmt_row(padded[0]))
        lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
        for row in padded[1:]:
            lines.append(_fmt_row(row))
    else:
        for row in padded:
            lines.append(_fmt_row(row))

    return "\n".join(lines)


def _to_plain_text(rows: list[list[str]], has_header: bool) -> str:
    """
    Convert table to key=value prose pairs.
    Used as fallback when Markdown is too wide / for single-column tables.

    Example:
        Parameter: Dose | Value: 100mg | Unit: mg/kg
    """
    if not rows:
        return ""

    if has_header and len(rows) >= 2:
        headers = rows[0]
        body    = rows[1:]
        parts   = []
        for row in body:
            pairs = []
            for i, cell in enumerate(row):
                if cell:
                    key = headers[i] if i < len(headers) and headers[i] else f"Col{i+1}"
                    pairs.append(f"{key}: {cell}")
            if pairs:
                parts.append(" | ".join(pairs))
        return "\n".join(parts)
    else:
        return "\n".join(
            " | ".join(c for c in row if c)
            for row in rows
        )


def _to_csv(rows: list[list[str]]) -> str:
    """Simple CSV fallback."""
    import csv
    import io as _io
    buf = _io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().strip()


# ─────────────────────────────────────────────────────────────────────────────
# Optional GPT-4o table summary
# ─────────────────────────────────────────────────────────────────────────────

def _summarise_table_with_gpt4o(
    markdown: str,
    context_text: str = "",
) -> str:
    """
    Ask GPT-4o to produce a 2-3 sentence natural language summary of the table.
    This dramatically improves RAG recall — queries like "what was the dose?"
    match the summary rather than needing to parse raw table syntax.

    Returns "" on failure (summary is optional, not critical).
    """
    if not markdown or len(markdown) < 50:
        return ""

    try:
        from azure_client import call_chat

        context_hint = (
            f"Document context near this table:\n{context_text[:400]}\n\n"
            if context_text else ""
        )

        prompt = (
            f"{context_hint}"
            f"Here is a table extracted from a regulatory document:\n\n"
            f"{markdown[:2000]}\n\n"
            "Write 2-3 sentences summarising what this table shows. "
            "Focus on what the data represents, key values, and any notable patterns. "
            "Plain text only — no markdown, no bullet points."
        )

        reply, _ = call_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        return reply.strip()

    except Exception as e:
        logger.debug(f"Table GPT summary failed: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Main extractor
# ─────────────────────────────────────────────────────────────────────────────

class TableExtractor:
    """
    Extracts and serialises all tables from a single PDF page.

    Usage:
        extractor = TableExtractor("path/to/doc.pdf", summarise=True)
        results   = extractor.extract_page(page_number=5, context_text="...")
        for tr in results:
            print(tr.best_text)
    """

    def __init__(
        self,
        file_path:  str,
        summarise:  bool = True,    # call GPT-4o for prose summary
        fmt:        str  = None,    # "markdown" | "csv" | "plain"
    ):
        self.file_path = file_path
        self.summarise = summarise
        self.fmt       = fmt or TABLE_FORMAT

    def extract_page(
        self,
        page_number:  int,           # 1-based
        context_text: str = "",      # surrounding text for GPT-4o context
    ) -> list[TableResult]:
        """
        Extract all tables from a single page.
        Returns list of TableResult (may be empty). Never raises.
        """
        pdfplumber = _pdfplumber()
        results = []

        try:
            with pdfplumber.open(self.file_path) as doc:
                if page_number < 1 or page_number > len(doc.pages):
                    logger.warning(
                        f"Page {page_number} out of range "
                        f"(doc has {len(doc.pages)} pages)"
                    )
                    return results

                plumb_page = doc.pages[page_number - 1]   # pdfplumber is 0-based
                tables     = plumb_page.extract_tables(TABLE_SETTINGS)

                if not tables:
                    return results

                for idx, raw_table in enumerate(tables):
                    result = self._process_table(
                        raw_table, page_number, idx, context_text
                    )
                    if result:
                        results.append(result)

        except Exception as e:
            logger.error(
                f"TableExtractor.extract_page failed "
                f"p{page_number} in {self.file_path}: {e}",
                exc_info=True,
            )

        return results

    def extract_all_pages(
        self,
        context_by_page: dict[int, str] = None,
    ) -> dict[int, list[TableResult]]:
        """
        Extract tables from every page in the document.
        Returns {page_number: [TableResult, ...]}
        Useful for pre-scanning a full document.
        """
        pdfplumber = _pdfplumber()
        all_results: dict[int, list[TableResult]] = {}
        context_by_page = context_by_page or {}

        try:
            with pdfplumber.open(self.file_path) as doc:
                total = len(doc.pages)
                for page_number in range(1, total + 1):
                    ctx = context_by_page.get(page_number, "")
                    results = self.extract_page(page_number, ctx)
                    if results:
                        all_results[page_number] = results
                        logger.debug(
                            f"  p{page_number}/{total}: "
                            f"{len(results)} table(s) extracted"
                        )
        except Exception as e:
            logger.error(f"extract_all_pages failed: {e}", exc_info=True)

        return all_results

    # ── Private ───────────────────────────────────────────────────────────────

    def _process_table(
        self,
        raw_table:    list[list],
        page_number:  int,
        table_index:  int,
        context_text: str,
        *,
        allow_short: bool = False,
        preserve_source: bool = False,
    ) -> Optional[TableResult]:
        """Process a single raw table from pdfplumber."""

        # Clean all cells
        cleaned = [_clean_row(row, truncate=not preserve_source)
                   for row in raw_table if row]

        # Remove completely empty rows
        cleaned = [r for r in cleaned if not _is_empty_row(r)]

        if not cleaned:
            logger.debug(f"p{page_number} table {table_index}: all rows empty — skipped")
            return None

        # Fill merged cells
        if not preserve_source:
            cleaned = _fill_merged_cells(cleaned)

        # Validate minimum useful size
        col_count = max(len(r) for r in cleaned)
        row_count = len(cleaned)

        if row_count < 1 or col_count < 1:
            return None

        # Detect header
        has_header = _detect_header_row(cleaned)

        # Serialise
        markdown   = _to_markdown(cleaned, has_header)
        plain_text = _to_plain_text(cleaned, has_header)

        # Validate minimum text length
        if not allow_short and len(markdown) < CHUNK_MIN_CHARS and len(plain_text) < CHUNK_MIN_CHARS:
            logger.debug(
                f"p{page_number} table {table_index}: "
                f"too short ({len(markdown)} chars) — skipped"
            )
            return None

        # Optional GPT-4o summary
        summary = ""
        if self.summarise:
            summary = _summarise_table_with_gpt4o(markdown, context_text)

        logger.debug(
            f"p{page_number} table {table_index}: "
            f"{row_count}r × {col_count}c  "
            f"header={has_header}  "
            f"md_len={len(markdown)}"
        )

        return TableResult(
            page_number=page_number,
            table_index=table_index,
            row_count=row_count,
            col_count=col_count,
            has_header=has_header,
            markdown=markdown,
            plain_text=plain_text,
            summary=summary,
            raw_cells=cleaned,
            success=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function called by main.py / chunker.py
# ─────────────────────────────────────────────────────────────────────────────

def extract_tables_for_page(
    file_path:    str,
    page_number:  int,
    context_text: str = "",
    summarise:    bool = True,
) -> list[TableResult]:
    """
    Top-level entry used by the pipeline per page.
    Returns list of TableResult — never raises.
    """
    try:
        extractor = TableExtractor(file_path, summarise=summarise)
        return extractor.extract_page(page_number, context_text)
    except Exception as e:
        logger.error(f"extract_tables_for_page failed p{page_number}: {e}")
        return []


def extract_tables_for_pages(
    file_path: str,
    page_numbers: list[int],
    context_by_page: dict[int, str] | None = None,
) -> tuple[dict[int, list[TableResult]], dict[int, str]]:
    """Batch many PDF pages using ONE pdfplumber document handle.

    Existing extraction/table serialization logic is reused. No GPT calls
    while a PDF handle is open. Errors are isolated per page and returned
    to the caller, so document completeness cannot silently pass.
    A caller should submit a bounded list (default 32 pages).
    """
    if not page_numbers:
        return {}, {}
    if len(page_numbers) > 256 or len(set(page_numbers)) != len(page_numbers):
        raise ValueError("Table page batch must contain 1..256 distinct pages")
    if any(type(page) is not int or page < 1 for page in page_numbers):
        raise ValueError("Invalid table page number")
    context_by_page = context_by_page or {}
    extractor = TableExtractor(file_path, summarise=False)
    pdfplumber = _pdfplumber()
    output = {}
    errors = {}
    try:
        with pdfplumber.open(file_path) as doc:
            for page_number in page_numbers:
                if page_number > len(doc.pages):
                    errors[page_number] = "Page outside PDF bounds"
                    continue
                page = None
                try:
                    page = doc.pages[page_number - 1]
                    tables = page.find_tables(TABLE_SETTINGS)
                    results = []
                    for idx, detected in enumerate(tables or []):
                        result = extractor._process_table(
                            detected.extract(), page_number, idx,
                            context_by_page.get(page_number, "")
                        )
                        if result:
                            result.bbox = tuple(detected.bbox)
                            results.append(result)
                    output[page_number] = results
                except Exception as exc:
                    errors[page_number] = f"{type(exc).__name__}: {str(exc)[:300]}"
                finally:
                    if page is not None and hasattr(page, "close"):
                        page.close()  # release pdfplumber per-page layout caches
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        for page_number in page_numbers:
            errors.setdefault(page_number, message)
    return output, errors


def inject_table_text_into_elements(
    elements:     list,
    table_results: list[TableResult],
    *,
    strict: bool = False,
) -> list:
    """
    Fill .text on TABLE PageElements using the closest matching TableResult
    (matched by page_number and proximity of table_index).
    Mutates elements in-place. Returns modified list.
    """
    from pdf_processor import ElementType

    table_els = [e for e in elements if e.element_type == ElementType.TABLE]

    if strict:
        # A table's text must belong to its source bounding box.
        # Blind index matching can mix up adjacent regulatory tables.
        if len(table_els) != len(table_results):
            raise ValueError(
                f"Table region/result count mismatch: {len(table_els)} "
                f"regions vs {len(table_results)} extracted tables"
            )
        remaining = list(table_results)
        for el in table_els:
            bbox = (el.bbox.x0,el.bbox.y0,el.bbox.x1,el.bbox.y1)
            matches = [
                (max(abs(a-b) for a,b in zip(bbox,t.bbox)),index,t)
                for index,t in enumerate(remaining) if t.bbox is not None
            ]
            if not matches:
                raise ValueError("Missing table geometry for source region")
            distance,index,table = min(matches,key=lambda item:item[0])
            if distance > 1.0:
                raise ValueError(
                    f"Table geometry mismatch: max axis offset={distance:.2f}"
                )
            el.text=table.best_text
            if getattr(table, "source_table", None) is not None:
                el.source_table=validate_source_table(table.source_table)
            del remaining[index]
    else:
        for i, el in enumerate(table_els):
            if i < len(table_results):
                el.text = table_results[i].best_text
            else:
                el.text = ""
    return elements
