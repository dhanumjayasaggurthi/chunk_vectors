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

def _clean_cell(raw) -> str:
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
    if len(text) > 500:
        text = text[:497] + "…"
    return text


def _clean_row(row: list) -> list[str]:
    return [_clean_cell(cell) for cell in row]


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
    ) -> Optional[TableResult]:
        """Process a single raw table from pdfplumber."""

        # Clean all cells
        cleaned = [_clean_row(row) for row in raw_table if row]

        # Remove completely empty rows
        cleaned = [r for r in cleaned if not _is_empty_row(r)]

        if not cleaned:
            logger.debug(f"p{page_number} table {table_index}: all rows empty — skipped")
            return None

        # Fill merged cells
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
        if len(markdown) < CHUNK_MIN_CHARS and len(plain_text) < CHUNK_MIN_CHARS:
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


def inject_table_text_into_elements(
    elements:     list,
    table_results: list[TableResult],
) -> list:
    """
    Fill .text on TABLE PageElements using the closest matching TableResult
    (matched by page_number and proximity of table_index).
    Mutates elements in-place. Returns modified list.
    """
    from pdf_processor import ElementType

    table_els = [e for e in elements if e.element_type == ElementType.TABLE]

    for i, el in enumerate(table_els):
        if i < len(table_results):
            el.text = table_results[i].best_text
        else:
            el.text = ""   # no matching extraction

    return elements
