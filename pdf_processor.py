"""
pdf_processor.py
================
Core PDF analysis engine for the EPOD pipeline.

For every page of a PDF it:
  1. Extracts native text blocks with bounding boxes (PyMuPDF)
  2. Detects image regions  → flags for Google Vision OCR
  3. Detects table regions  → flags for pdfplumber table extraction
  4. Classifies each region : text | image | table | chart | mixed
  5. Reconstructs reading order  (top-to-bottom, left-to-right)
  6. Returns a PageContent object per page, consumed by chunker.py

Handles:
  - PDFs with thousands of pages (streams page-by-page, low memory)
  - Scanned pages  (no native text → OCR trigger)
  - Mixed pages    (text + embedded images + tables on same page)
  - Corrupt/unreadable pages  (logged, skipped, pipeline continues)
  - Password-protected PDFs   (logged as ERROR, skipped)
  - TOC extraction            (drives section-level chunking)

Dependencies:
    pip install pymupdf pdfplumber pillow
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# ── lazy imports (so syntax check works without packages installed) ──────────
def _fitz():
    import fitz  # PyMuPDF
    return fitz

def _pdfplumber():
    import pdfplumber
    return pdfplumber

def _Image():
    from PIL import Image
    return Image
# ─────────────────────────────────────────────────────────────────────────────

from config import (
    OCR_RENDER_DPI,
    SCANNED_PAGE_TEXT_THRESHOLD,
    TABLE_SETTINGS,
    CHUNK_MIN_CHARS,
)
from logger import get_logger, DocLogger

logger = get_logger("epod.pdf_processor")


# ─────────────────────────────────────────────────────────────────────────────
# Enums & data classes
# ─────────────────────────────────────────────────────────────────────────────

class ElementType(str, Enum):
    TEXT  = "text"
    IMAGE = "image"
    TABLE = "table"
    CHART = "chart"
    MIXED = "mixed"


@dataclass
class BBox:
    """Bounding box in PDF points (origin top-left)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def area(self) -> float:
        return max(0, self.x1 - self.x0) * max(0, self.y1 - self.y0)

    def overlaps(self, other: "BBox", threshold: float = 0.01) -> bool:
        """True if intersection area > threshold fraction of the smaller box."""
        ix0 = max(self.x0, other.x0)
        iy0 = max(self.y0, other.y0)
        ix1 = min(self.x1, other.x1)
        iy1 = min(self.y1, other.y1)
        inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        if inter <= 0:
            return False
        min_area = min(self.area, other.area)
        return (inter / min_area) > threshold if min_area > 0 else False


@dataclass
class PageElement:
    """
    A single classified region on a page.
    text is filled immediately for TEXT elements.
    For IMAGE/TABLE/CHART, text is filled after OCR/table extraction.
    """
    element_type: ElementType
    bbox:         BBox
    text:         str        = ""
    image_bytes:  bytes      = field(default=b"", repr=False)  # PNG bytes for OCR
    confidence:   float      = 1.0   # OCR confidence  (0-1)
    reading_order: int       = 0


@dataclass
class TocEntry:
    """One entry from the PDF Table of Contents."""
    level:      int     # 1 = top, 2 = sub-section, etc.
    title:      str
    page_number: int    # 1-based


@dataclass
class PageContent:
    """
    All extracted content for a single page.
    Produced by PDFProcessor and consumed by chunker.py.
    """
    page_number:   int          # 1-based
    width:         float        # page dimensions in points
    height:        float
    elements:      list[PageElement] = field(default_factory=list)
    needs_ocr:     bool          = False   # True → scanned / image-heavy
    is_blank:      bool          = False
    raw_text:      str           = ""      # concatenated text (filled after OCR)
    has_tables:    bool          = False
    has_images:    bool          = False
    error:         str           = ""      # non-empty if page processing failed


@dataclass
class DocumentStructure:
    """
    Full structural analysis of one PDF.
    Passed to chunker.py which uses toc + pages to build chunks.
    """
    file_path:   str
    file_name:   str
    doc_id:      str
    page_count:  int
    toc:         list[TocEntry]       = field(default_factory=list)
    pages:       list[PageContent]    = field(default_factory=list)
    has_toc:     bool                 = False
    error:       str                  = ""   # set if entire PDF failed to open


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Regex patterns that suggest a text block is a section heading
_HEADING_PATTERNS = [
    re.compile(r"^\d+(\.\d+)*\s+[A-Z]"),          # "1.2.3 HEADING"
    re.compile(r"^(SECTION|CHAPTER|ANNEX|APPENDIX|PART)\s+\w", re.I),
    re.compile(r"^[A-Z][A-Z\s]{4,40}$"),           # ALL CAPS SHORT LINE
    re.compile(r"^(Executive Summary|Introduction|Conclusion|References)", re.I),
]

def _looks_like_heading(text: str, font_size: float, page_median_size: float) -> bool:
    """Heuristic: is this text block a section heading?"""
    t = text.strip()
    if not t or len(t) > 120:
        return False
    if font_size > page_median_size * 1.15:   # notably larger than body text
        return True
    for pat in _HEADING_PATTERNS:
        if pat.match(t):
            return True
    return False


def _median_font_size(blocks: list) -> float:
    """Compute median font size across all text spans on a page."""
    sizes = []
    for b in blocks:
        if b.get("type") != 0:   # 0 = text block
            continue
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                s = span.get("size", 0)
                if s > 0:
                    sizes.append(s)
    if not sizes:
        return 12.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _clean_text(raw: str) -> str:
    """
    Remove ¶ markers, excessive whitespace, soft hyphens,
    control characters. Keep newlines between paragraphs.
    """
    if not raw:
        return ""
    # Remove paragraph/pilcrow markers
    text = raw.replace("¶", " ")
    # Remove soft hyphen (U+00AD)
    text = text.replace("\u00ad", "")
    # Collapse 3+ consecutive newlines → 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove control chars except \n \t
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    # Collapse multiple spaces
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _is_chart(img_bbox: BBox, text_elements: list[PageElement]) -> bool:
    """
    Heuristic: an image is likely a chart/graph if there are numeric
    text elements nearby (axis labels, legends).
    Charts often have very little standalone text compared to their area.
    """
    nearby_text = ""
    for el in text_elements:
        if el.bbox.overlaps(BBox(
            img_bbox.x0 - 20, img_bbox.y0 - 20,
            img_bbox.x1 + 20, img_bbox.y1 + 20,
        ), threshold=0.01):
            nearby_text += el.text

    # If there are numbers/% in nearby text → likely chart
    num_matches = len(re.findall(r"\d+[.,]?\d*\s*%?", nearby_text))
    return num_matches >= 3


# ─────────────────────────────────────────────────────────────────────────────
# Main processor
# ─────────────────────────────────────────────────────────────────────────────

class PDFProcessor:
    """
    Analyses a single PDF file and returns a DocumentStructure.

    Usage:
        proc   = PDFProcessor("path/to/doc.pdf", doc_id="abc123", file_name="doc.pdf")
        struct = proc.process()
        # struct.toc  → list of TocEntry
        # struct.pages → list of PageContent
    """

    def __init__(
        self,
        file_path: str,
        doc_id:    str,
        file_name: str,
    ):
        self.file_path = file_path
        self.doc_id    = doc_id
        self.file_name = file_name
        self.dlog      = DocLogger(file_name, doc_id, "epod.pdf_processor")

    # ── Public ───────────────────────────────────────────────────────────────

    def process(self) -> DocumentStructure:
        """
        Full analysis of the PDF.
        Returns DocumentStructure even on failure (with .error set).
        """
        fitz = _fitz()
        struct = DocumentStructure(
            file_path=self.file_path,
            file_name=self.file_name,
            doc_id=self.doc_id,
            page_count=0,
        )

        try:
            doc = fitz.open(self.file_path)
        except fitz.FileDataError as e:
            struct.error = f"Cannot open PDF (corrupt?): {e}"
            self.dlog.error(struct.error)
            return struct
        except Exception as e:
            struct.error = f"Unexpected open error: {e}"
            self.dlog.error(struct.error, exc_info=True)
            return struct

        # Password-protected check
        if doc.is_encrypted:
            struct.error = "PDF is password-protected — cannot process"
            self.dlog.error(struct.error)
            doc.close()
            return struct

        struct.page_count = doc.page_count
        self.dlog.info(f"Opened — {doc.page_count} pages")

        # ── Table of Contents ────────────────────────────────────────────────
        struct.toc    = self._extract_toc(doc)
        struct.has_toc = len(struct.toc) > 0
        if struct.has_toc:
            self.dlog.info(f"TOC found — {len(struct.toc)} entries (section-level chunking)")
        else:
            self.dlog.info("No TOC — will use page-level chunking fallback")


        # For large docs (6500+ pages), reload page object every 500 pages to
        # flush PyMuPDF C-heap page cache and prevent OOM accumulation.# ── Page-by-page analysis ────────────────────────────────────────────
        for page_num in range(doc.page_count):
            page_content = self._process_page(doc, page_num)
            struct.pages.append(page_content)

            if (page_num + 1) % 100 == 0:
                self.dlog.info(
                    f"  Pages analysed: {page_num + 1}/{doc.page_count}",
                    extra={"page": page_num + 1}
                )
        # Memory flush for very large docs
            if (page_num + 1) % 500 == 0 and doc.page_count > 1000:
                try:
                    doc.reload_page(doc[page_num])
                except Exception:
                    pass   # non-fatal — just a memory hint

        doc.close()
        ocr_count   = sum(1 for p in struct.pages if p.needs_ocr)
        table_count = sum(1 for p in struct.pages if p.has_tables)
        blank_count = sum(1 for p in struct.pages if p.is_blank)
        error_count = sum(1 for p in struct.pages if p.error)

        self.dlog.info(
            f"Analysis complete — "
            f"OCR needed: {ocr_count}  "
            f"Tables: {table_count}  "
            f"Blank: {blank_count}  "
            f"Page errors: {error_count}"
        )
        return struct

    # ── TOC extraction ────────────────────────────────────────────────────────

    def _extract_toc(self, doc) -> list[TocEntry]:
        """
        Extract PDF bookmark/outline tree as TocEntry list.
        Falls back to heuristic heading detection if no bookmarks found.
        """
        entries = []
        try:
            raw_toc = doc.get_toc(simple=False)   # [[level, title, page, dest], ...]
            for item in raw_toc:
                level = item[0]
                title = _clean_text(str(item[1]))
                page  = int(item[2])
                if title and page > 0:
                    entries.append(TocEntry(level=level, title=title, page_number=page))
        except Exception as e:
            self.dlog.warning(f"TOC extraction failed: {e}")

        return entries

    # ── Single page ───────────────────────────────────────────────────────────

    def _process_page(self, doc, page_num: int) -> PageContent:
        """
        Analyse one page. Returns PageContent.
        Never raises — all errors are caught and stored in PageContent.error.
        """
        page_1based = page_num + 1
        try:
            page = doc[page_num]
            return self._analyse_page(page, page_1based)
        except Exception as e:
            self.dlog.error(
                f"Page {page_1based} failed: {e}",
                exc_info=True,
                extra={"page": page_1based}
            )
            return PageContent(
                page_number=page_1based,
                width=0, height=0,
                error=str(e),
            )

    def _analyse_page(self, page, page_number: int) -> PageContent:
        """Core per-page analysis — text, images, tables, reading order."""
        fitz = _fitz()
        rect   = page.rect
        width, height = rect.width, rect.height

        pc = PageContent(
            page_number=page_number,
            width=width,
            height=height,
        )

        # ── 1. Extract raw text dict (blocks with bboxes + font info) ────────
        try:
            text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            blocks    = text_dict.get("blocks", [])
        except Exception as e:
            pc.error = f"get_text failed: {e}"
            return pc

        median_size = _median_font_size(blocks)

        # ── 2. Text elements ─────────────────────────────────────────────────
        text_elements: list[PageElement] = []
        all_text_parts = []

        for block in blocks:
            if block.get("type") != 0:   # skip image blocks here (handled below)
                continue
            b = block.get("bbox", (0, 0, 0, 0))
            bbox = BBox(b[0], b[1], b[2], b[3])

            # Gather all span text within block
            block_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    block_text += span.get("text", "")
                block_text += "\n"

            block_text = _clean_text(block_text)
            if not block_text:
                continue

            all_text_parts.append(block_text)

            el = PageElement(
                element_type=ElementType.TEXT,
                bbox=bbox,
                text=block_text,
            )
            text_elements.append(el)

        # ── 3. Image elements ────────────────────────────────────────────────
        image_elements: list[PageElement] = []
        try:
            img_list = page.get_images(full=True)
        except Exception:
            img_list = []

        for img_info in img_list:
            xref = img_info[0]
            pix  = None
            try:
                rects = page.get_image_rects(img_info)
                if not rects:
                    # Image exists in xref table but has no renderable location
                    # on this page (e.g. shared resource, form XObject, etc.)
                    self.dlog.debug(
                        f"Page {page_number}: xref={xref} has no rects — skipped"
                    )
                    continue

                ib   = rects[0]
                bbox = BBox(ib.x0, ib.y0, ib.x1, ib.y1)

                # Skip tiny images (decorative bullets, logos < 50×50 pts)
                if bbox.area < 2500:
                    continue

                # Render image region to PNG bytes for downstream OCR
                clip    = fitz.Rect(ib.x0, ib.y0, ib.x1, ib.y1)
                mat     = fitz.Matrix(OCR_RENDER_DPI / 72, OCR_RENDER_DPI / 72)
                pix     = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                img_png = pix.tobytes("png")

                # Classify as chart or image
                el_type = ElementType.CHART if _is_chart(bbox, text_elements) \
                          else ElementType.IMAGE
                el = PageElement(
                    element_type=el_type,
                    bbox=bbox,
                    image_bytes=img_png,
                )
                image_elements.append(el)

            except (IndexError, ValueError) as e:
                # IndexError: PyMuPDF internal get_image_rects edge case
                # ValueError: malformed rect / zero-size image
                self.dlog.debug(
                    f"Page {page_number}: image geometry error xref={xref}: {e}"
                )
                continue
            except Exception as e:
                self.dlog.debug(
                    f"Page {page_number}: image extract error xref={xref}: {e}"
                )
                continue
            finally:
                # Explicitly free C-heap pixmap memory — critical for 6500+ page
                # docs where PyMuPDF's C allocator won't release until doc.close()
                if pix is not None:
                    pix = None

        # ── 4. Scanned page detection ────────────────────────────────────────
        #    If native text is almost zero but images exist → needs full OCR
        native_text_len = sum(len(e.text) for e in text_elements)
        if native_text_len < SCANNED_PAGE_TEXT_THRESHOLD and image_elements:
            pc.needs_ocr = True
            # Render entire page for OCR (not just individual images)
            pix = None
            try:
                mat     = fitz.Matrix(OCR_RENDER_DPI / 72, OCR_RENDER_DPI / 72)
                pix     = page.get_pixmap(matrix=mat, alpha=False)
                img_png = pix.tobytes("png")
                # Add as a single IMAGE element covering the whole page
                whole_page_el = PageElement(
                    element_type=ElementType.IMAGE,
                    bbox=BBox(0, 0, width, height),
                    image_bytes=img_png,
                )
                image_elements = [whole_page_el]   # replace partial images
            except Exception as e:
                self.dlog.warning(f"Page {page_number}: full-page render failed: {e}")
            finally:
                pix = None   # free C-heap memory immediately

        # ── 5. Table region detection (bounding boxes only — pdfplumber extracts) ─
        table_bboxes = self._detect_table_regions(page)
        table_elements: list[PageElement] = []
        for tb in table_bboxes:
            el = PageElement(
                element_type=ElementType.TABLE,
                bbox=tb,
            )
            table_elements.append(el)

        # ── 6. Blank page check ──────────────────────────────────────────────
        all_elements = text_elements + image_elements + table_elements
        if not all_elements:
            pc.is_blank = True
            return pc

        # ── 7. Reading order sort (top-to-bottom, then left-to-right) ────────
        #    Two-column detection: if median x of elements < page_width/2 and
        #    there's content with median x > page_width/2 → sort by column first
        all_elements = self._sort_reading_order(all_elements, width)
        for i, el in enumerate(all_elements):
            el.reading_order = i

        # ── 8. Assemble PageContent ──────────────────────────────────────────
        pc.elements   = all_elements
        pc.has_tables = len(table_elements) > 0
        pc.has_images = len(image_elements) > 0

        # raw_text = joined text from TEXT elements in reading order
        # (image/table .text filled later by vision_ocr + table_extractor)
        text_parts = [
            e.text for e in all_elements
            if e.element_type == ElementType.TEXT and e.text
        ]
        pc.raw_text = "\n".join(text_parts)

        return pc

    # ── Table region detection ────────────────────────────────────────────────

    def _detect_table_regions(self, page) -> list[BBox]:
        """
        Use pdfplumber to detect table bounding boxes on this page.
        Returns list of BBox objects (may be empty).
        """
        pdfplumber = _pdfplumber()
        bboxes = []
        try:
            # pdfplumber needs to open the same file — we reopen just this page
            with pdfplumber.open(self.file_path) as plumb_doc:
                # page_number is 1-based in our system, pdfplumber is 0-based
                if page.number < len(plumb_doc.pages):
                    plumb_page = plumb_doc.pages[page.number]
                    tables = plumb_page.find_tables(TABLE_SETTINGS)
                    for tbl in tables:
                        bb = tbl.bbox  # (x0, top, x1, bottom)
                        bboxes.append(BBox(bb[0], bb[1], bb[2], bb[3]))
        except Exception as e:
            # Non-fatal — page just won't have table regions flagged
            self.dlog.debug(f"Table detection failed page {page.number + 1}: {e}")
        return bboxes

    # ── Reading order ──────────────────────────────────────────────────────────

    def _sort_reading_order(
        self,
        elements: list[PageElement],
        page_width: float,
    ) -> list[PageElement]:
        """
        Sort elements into natural reading order.

        Single-column: sort by y0 (top), break ties by x0.
        Two-column:    bucket into left/right column first, then sort by y0 within each.
        """
        if not elements:
            return elements

        # Detect two-column layout:
        # If > 30% of elements have x_centre < page_width/2 AND
        #    > 30% have x_centre > page_width/2  →  two-column
        mid = page_width / 2
        left_count  = sum(1 for e in elements if (e.bbox.x0 + e.bbox.x1) / 2 < mid)
        right_count = sum(1 for e in elements if (e.bbox.x0 + e.bbox.x1) / 2 >= mid)
        total = len(elements)

        is_two_col = (
            total >= 4
            and left_count / total > 0.25
            and right_count / total > 0.25
        )

        if is_two_col:
            left  = [e for e in elements if (e.bbox.x0 + e.bbox.x1) / 2 < mid]
            right = [e for e in elements if (e.bbox.x0 + e.bbox.x1) / 2 >= mid]
            left.sort(key=lambda e: (e.bbox.y0, e.bbox.x0))
            right.sort(key=lambda e: (e.bbox.y0, e.bbox.x0))
            # Interleave: pair rows at similar y positions
            return self._interleave_columns(left, right)
        else:
            elements.sort(key=lambda e: (round(e.bbox.y0 / 5) * 5, e.bbox.x0))
            return elements

    @staticmethod
    def _interleave_columns(
        left: list[PageElement],
        right: list[PageElement],
    ) -> list[PageElement]:
        """
        Merge left and right column elements in y-order.
        Elements at similar y positions are output left-then-right.
        """
        result = []
        li, ri = 0, 0
        while li < len(left) and ri < len(right):
            le, re = left[li], right[ri]
            # Elements within 10 pts vertically are on the "same row"
            if abs(le.bbox.y0 - re.bbox.y0) < 10:
                result.append(le); li += 1
                result.append(re); ri += 1
            elif le.bbox.y0 < re.bbox.y0:
                result.append(le); li += 1
            else:
                result.append(re); ri += 1
        result.extend(left[li:])
        result.extend(right[ri:])
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function used by main.py
# ─────────────────────────────────────────────────────────────────────────────

def process_pdf(
    file_path: str,
    doc_id:    str,
    file_name: str,
) -> DocumentStructure:
    """
    Top-level entry point for the pipeline.
    Returns DocumentStructure (never raises).
    """
    try:
        proc = PDFProcessor(file_path, doc_id, file_name)
        return proc.process()
    except Exception as e:
        logger.error(f"process_pdf fatal error {file_name}: {e}", exc_info=True)
        return DocumentStructure(
            file_path=file_path,
            file_name=file_name,
            doc_id=doc_id,
            page_count=0,
            error=str(e),
        )
