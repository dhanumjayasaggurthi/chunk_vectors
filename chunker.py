"""
chunker.py
==========
The most critical component of the EPOD pipeline.

Converts a DocumentStructure (from pdf_processor) into a list of Chunk
objects ready for embedding. Three-level strategy, tried in order:

  Level 1 — SECTION   Uses PDF Table of Contents bookmarks.
                       Each TOC section → one chunk (pages merged).
                       If a section exceeds CHUNK_MAX_CHARS it is
                       sub-split at page boundaries first, then by
                       sliding window if still too large.

  Level 2 — PAGE      No TOC, or TOC has < 3 entries.
                       Detects heading-style text on each page to
                       group consecutive pages under the same heading.
                       Each group → one chunk.

  Level 3 — SIZE      Any chunk from Level 1 or 2 that still exceeds
                       CHUNK_MAX_CHARS is split with a sliding window
                       (CHUNK_TARGET_CHARS tokens, CHUNK_OVERLAP_CHARS
                       overlap) so context is preserved across splits.

Every chunk carries:
  - Full provenance (file, section title, page range, level, content types)
  - Cleaned, deduplicated text
  - A unique deterministic chunk_id

Works correctly on 3000-page PDFs by streaming page content — never
loads all page text into memory at once during section assembly.

Dependencies: only stdlib + project modules (no extra pip installs).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from config import (
    CHUNK_TARGET_CHARS,
    CHUNK_OVERLAP_CHARS,
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    SECTION_MIN_CHARS,
    MAX_PAGES_PER_GROUP,
)
from logger import get_logger, DocLogger
from pdf_processor import DocumentStructure, PageContent, ElementType, TocEntry

logger = get_logger("epod.chunker")


# ─────────────────────────────────────────────────────────────────────────────
# Chunk data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """
    A single embeddable unit of document content.
    Produced by Chunker and consumed by embedder.py → db.insert_chunks_batch().
    """
    chunk_id:      str            # sha256(doc_id + chunk_index)
    doc_id:        str
    file_name:     str
    file_path:     str
    chunk_index:   int            # 0-based position within document
    chunk_total:   int            # filled in after all chunks are known
    chunk_level:   str            # "section" | "page" | "split"
    section_title: str            # nearest section heading
    page_start:    int            # 1-based first page
    page_end:      int            # 1-based last page
    content_types: list[str]      # ["text","table","ocr","chart"]
    chunk_text:    str            # clean text sent to embedding
    chunk_vector:  list[float] = field(default_factory=list, repr=False)

    def to_db_dict(self) -> dict:
        """Convert to the dict format expected by db.insert_chunks_batch()."""
        return {
            "chunk_id":      self.chunk_id,
            "doc_id":        self.doc_id,
            "file_name":     self.file_name,
            "file_path":     self.file_path,
            "chunk_index":   self.chunk_index,
            "chunk_total":   self.chunk_total,
            "chunk_level":   self.chunk_level,
            "section_title": self.section_title,
            "page_start":    self.page_start,
            "page_end":      self.page_end,
            "content_types": self.content_types,
            "chunk_text":    self.chunk_text,
            "chunk_vector":  self.chunk_vector,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Text assembly helpers
# ─────────────────────────────────────────────────────────────────────────────

def _page_full_text(page: PageContent) -> str:
    """
    Assemble all element text for a page in reading order.
    Elements must already have .text populated (OCR + table extraction done).
    """
    parts = []
    for el in page.elements:
        text = (el.text or "").strip()
        if not text:
            continue
        if el.element_type == ElementType.TABLE:
            parts.append(f"\n[TABLE]\n{text}\n[/TABLE]\n")
        elif el.element_type in (ElementType.IMAGE, ElementType.CHART):
            parts.append(f"\n[{el.element_type.upper()}]\n{text}\n[/{el.element_type.upper()}]\n")
        else:
            parts.append(text)
    return "\n".join(parts).strip()


def _page_content_types(page: PageContent) -> list[str]:
    """Return list of distinct element types present on this page."""
    seen = set()
    for el in page.elements:
        if el.text:
            et = el.element_type
            seen.add(et.value if hasattr(et, 'value') else str(et))
    return sorted(seen)


def _merge_content_types(pages: list[PageContent]) -> list[str]:
    seen = set()
    for p in pages:
        for ct in _page_content_types(p):
            seen.add(ct)
    return sorted(seen)


def _make_chunk_id(doc_id: str, chunk_index: int) -> str:
    raw = f"{doc_id}::chunk::{chunk_index:06d}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _dedup_text(text: str) -> str:
    """
    Remove paragraph-level duplicates within the same chunk.
    Splits on double-newline, deduplicates, rejoins.
    Common in PDFs where headers repeat across pages.
    """
    if not text:
        return text
    paragraphs = re.split(r"\n{2,}", text)
    seen = []
    seen_set: set[str] = set()
    for para in paragraphs:
        key = re.sub(r"\s+", " ", para).strip().lower()
        if key and key not in seen_set:
            seen_set.add(key)
            seen.append(para)
    return "\n\n".join(seen)


def _prefix_with_section(text: str, section_title: str) -> str:
    """
    Prepend section title to chunk text if not already present.
    Helps retrieval when query mentions the section name.
    """
    if not section_title:
        return text
    if text.strip().lower().startswith(section_title.strip().lower()):
        return text
    return f"{section_title}\n\n{text}"


# ─────────────────────────────────────────────────────────────────────────────
# Size-based sliding window splitter  (Level 3)
# ─────────────────────────────────────────────────────────────────────────────

def _sliding_window_split(
    text:          str,
    target_chars:  int = CHUNK_TARGET_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
    max_chars:     int = CHUNK_MAX_CHARS,
) -> list[str]:
    """
    Split a long text into overlapping windows.

    Strategy:
      1. Try to split at paragraph boundaries (double newline) first
         so we never cut mid-sentence.
      2. Fall back to sentence boundaries (period + space).
      3. Hard-split at target_chars only as last resort.

    Returns list of text chunks, each <= max_chars.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if len(text) >= CHUNK_MIN_CHARS else []

    chunks   = []
    position = 0
    total    = len(text)

    while position < total:
        end = min(position + target_chars, total)

        if end < total:
            # Try paragraph boundary
            para_break = text.rfind("\n\n", position, end)
            if para_break > position + (target_chars // 2):
                end = para_break + 2
            else:
                # Try sentence boundary
                sent_break = max(
                    text.rfind(". ", position, end),
                    text.rfind(".\n", position, end),
                    text.rfind("? ", position, end),
                    text.rfind("! ", position, end),
                )
                if sent_break > position + (target_chars // 3):
                    end = sent_break + 2
                # else: hard cut at target_chars

        segment = text[position:end].strip()
        if len(segment) >= CHUNK_MIN_CHARS:
            chunks.append(segment)

        # Advance with overlap
        if end >= total:
            break
        position = max(position + 1, end - overlap_chars)

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Heading detection for page-level grouping  (Level 2)
# ─────────────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(
    r"^\s*("
    r"\d+(\.\d+){0,3}\s+[A-Z][^\n]{0,100}"      # "1.2.3 Heading text"
    r"|[A-Z][A-Z\s\-]{3,50}"                      # "ALL CAPS HEADING"
    r"|(SECTION|CHAPTER|ANNEX|APPENDIX|PART|SCHEDULE)\s+\w[^\n]{0,80}"
    r"|(Executive Summary|Introduction|Conclusion|References|Background"
    r"|Objectives?|Methods?|Results?|Discussion|Summary|Overview"
    r"|Efficacy|Safety|Pharmacokinetics|Pharmacodynamics"
    r"|Clinical|Preclinical|Regulatory|Administrative)[^\n]{0,80}"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_page_heading(page: PageContent) -> Optional[str]:
    """
    Detect a section heading on this page by examining the first
    few text elements. Returns heading text or None.
    """
    if page.is_blank or page.error:
        return None

    for el in page.elements[:5]:    # only first 5 elements
        if el.element_type != ElementType.TEXT:
            continue
        text = (el.text or "").strip()
        if not text or len(text) > 150:
            continue
        if _HEADING_RE.match(text):
            return text

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main Chunker
# ─────────────────────────────────────────────────────────────────────────────

class Chunker:
    """
    Converts a fully-processed DocumentStructure into a list of Chunks.

    The pages in struct must have .elements[*].text already populated
    (OCR done by vision_ocr, tables done by table_extractor).

    Usage:
        chunker = Chunker(struct)
        chunks  = chunker.chunk()
        # chunks is a list[Chunk] with chunk_total set correctly
    """

    def __init__(self, struct: DocumentStructure):
        self.struct    = struct
        self.doc_id    = struct.doc_id
        self.file_name = struct.file_name
        self.file_path = struct.file_path
        self.dlog      = DocLogger(struct.file_name, struct.doc_id, "epod.chunker")
        self._chunks:  list[Chunk] = []
        self._idx      = 0          # running chunk index counter

    # ── Public ───────────────────────────────────────────────────────────────

    def chunk(self) -> list[Chunk]:
        """
        Main entry. Returns all chunks with chunk_total set.
        Picks the best strategy automatically.
        """
        if self.struct.error:
            self.dlog.warning(f"Skipping chunking — struct has error: {self.struct.error}")
            return []

        usable_pages = [
            p for p in self.struct.pages
            if not p.is_blank and not p.error and p.raw_text or p.elements
        ]

        if not usable_pages:
            self.dlog.warning("No usable pages found — no chunks produced")
            return []

        self.dlog.info(
            f"Chunking {len(usable_pages)}/{self.struct.page_count} usable pages  "
            f"toc_entries={len(self.struct.toc)}"
        )

        # ── Strategy selection ───────────────────────────────────────────────
        if self.struct.has_toc and len(self.struct.toc) >= 3:
            self.dlog.info("Strategy: SECTION-LEVEL (TOC bookmarks)")
            self._chunk_by_sections()
        else:
            self.dlog.info("Strategy: PAGE-LEVEL (heading detection)")
            self._chunk_by_pages()

        # ── Finalise ─────────────────────────────────────────────────────────
        total = len(self._chunks)
        for chunk in self._chunks:
            chunk.chunk_total = total

        self.dlog.info(
            f"Chunking complete — {total} chunks  "
            f"(levels: {self._level_summary()})"
        )
        return self._chunks

    # ── Level 1: Section chunking ─────────────────────────────────────────────

    def _chunk_by_sections(self):
        """
        Use TOC entries to group pages into sections.
        Each section span = pages from this TOC entry to the next.
        """
        toc    = self.struct.toc
        pages  = {p.page_number: p for p in self.struct.pages}
        total_pages = self.struct.page_count

        for i, entry in enumerate(toc):
            section_start = entry.page_number
            section_end   = (
                toc[i + 1].page_number - 1
                if i + 1 < len(toc)
                else total_pages
            )
            section_end = min(section_end, total_pages)

            # Collect page text for this section
            section_pages = [
                pages[pn] for pn in range(section_start, section_end + 1)
                if pn in pages and not pages[pn].is_blank and not pages[pn].error
            ]

            if not section_pages:
                continue

            section_text = self._assemble_pages(section_pages)
            section_text = _prefix_with_section(section_text, entry.title)

            if len(section_text) < SECTION_MIN_CHARS:
                self.dlog.debug(
                    f"Section '{entry.title[:40]}' too short "
                    f"({len(section_text)} chars) — skipped"
                )
                continue

            content_types = _merge_content_types(section_pages)

            if len(section_text) <= CHUNK_MAX_CHARS:
                # Fits in one chunk
                self._add_chunk(
                    text=section_text,
                    level="section",
                    section_title=entry.title,
                    page_start=section_start,
                    page_end=section_end,
                    content_types=content_types,
                )
            else:
                # Section too long → try page-boundary sub-splits first
                self.dlog.debug(
                    f"Section '{entry.title[:40]}' too long "
                    f"({len(section_text)} chars) — sub-splitting"
                )
                self._sub_split_section(
                    section_pages=section_pages,
                    section_title=entry.title,
                    page_start=section_start,
                    page_end=section_end,
                    content_types=content_types,
                )

    def _sub_split_section(
        self,
        section_pages:  list[PageContent],
        section_title:  str,
        page_start:     int,
        page_end:       int,
        content_types:  list[str],
    ):
        """
        A section is too large for one chunk.
        Strategy: group pages into MAX_PAGES_PER_GROUP batches,
        then apply sliding window on any batch still > CHUNK_MAX_CHARS.
        """
        # Group into page batches
        batch_size = MAX_PAGES_PER_GROUP
        for batch_start in range(0, len(section_pages), batch_size):
            batch = section_pages[batch_start: batch_start + batch_size]
            batch_text  = self._assemble_pages(batch)
            batch_text  = _prefix_with_section(batch_text, section_title)
            pg_start    = batch[0].page_number
            pg_end      = batch[-1].page_number
            batch_types = _merge_content_types(batch)

            if len(batch_text) <= CHUNK_MAX_CHARS:
                self._add_chunk(
                    text=batch_text,
                    level="section",
                    section_title=section_title,
                    page_start=pg_start,
                    page_end=pg_end,
                    content_types=batch_types,
                )
            else:
                # Still too large — sliding window
                splits = _sliding_window_split(batch_text)
                for seg in splits:
                    self._add_chunk(
                        text=seg,
                        level="split",
                        section_title=section_title,
                        page_start=pg_start,
                        page_end=pg_end,
                        content_types=batch_types,
                    )

    # ── Level 2: Page-level chunking ──────────────────────────────────────────

    def _chunk_by_pages(self):
        """
        No TOC — detect headings on each page and group consecutive
        pages under the same heading.
        Falls back to individual page chunks if no headings found.
        """
        usable = [
            p for p in self.struct.pages
            if not p.is_blank and not p.error
        ]

        if not usable:
            return

        # Build groups: list of (heading, [pages])
        groups: list[tuple[str, list[PageContent]]] = []
        current_heading = ""
        current_group:  list[PageContent] = []

        for page in usable:
            heading = _extract_page_heading(page)

            if heading and heading != current_heading:
                # New heading detected — flush current group
                if current_group:
                    groups.append((current_heading, current_group))
                current_heading = heading
                current_group   = [page]
            else:
                current_group.append(page)

                # Cap group size so chunks don't get enormous
                if len(current_group) >= MAX_PAGES_PER_GROUP:
                    groups.append((current_heading, current_group))
                    current_group = []

        if current_group:
            groups.append((current_heading, current_group))

        self.dlog.info(f"Page grouping produced {len(groups)} groups")

        for heading, group_pages in groups:
            self._emit_page_group(heading, group_pages)

    def _emit_page_group(self, heading: str, pages: list[PageContent]):
        """
        Turn one page group into one or more chunks.
        Applies size splitting if necessary.
        """
        group_text    = self._assemble_pages(pages)
        group_text    = _prefix_with_section(group_text, heading)
        content_types = _merge_content_types(pages)
        pg_start      = pages[0].page_number
        pg_end        = pages[-1].page_number

        if not group_text or len(group_text) < CHUNK_MIN_CHARS:
            return

        if len(group_text) <= CHUNK_MAX_CHARS:
            self._add_chunk(
                text=group_text,
                level="page",
                section_title=heading,
                page_start=pg_start,
                page_end=pg_end,
                content_types=content_types,
            )
        else:
            splits = _sliding_window_split(group_text)
            for seg in splits:
                self._add_chunk(
                    text=seg,
                    level="split",
                    section_title=heading,
                    page_start=pg_start,
                    page_end=pg_end,
                    content_types=content_types,
                )

    # ── Text assembly ─────────────────────────────────────────────────────────

    def _assemble_pages(self, pages: list[PageContent]) -> str:
        """
        Combine text from a list of pages into a single string.
        Deduplicates repeated paragraphs (e.g. running headers/footers).
        """
        parts = []
        for page in pages:
            text = _page_full_text(page)
            if text:
                parts.append(text)

        combined = "\n\n".join(parts)
        combined = _dedup_text(combined)
        return combined.strip()

    # ── Chunk emission ────────────────────────────────────────────────────────

    def _add_chunk(
        self,
        text:          str,
        level:         str,
        section_title: str,
        page_start:    int,
        page_end:      int,
        content_types: list[str],
    ):
        """Create a Chunk and append to internal list."""
        text = text.strip()
        if len(text) < CHUNK_MIN_CHARS:
            return

        chunk = Chunk(
            chunk_id      = _make_chunk_id(self.doc_id, self._idx),
            doc_id        = self.doc_id,
            file_name     = self.file_name,
            file_path     = self.file_path,
            chunk_index   = self._idx,
            chunk_total   = 0,           # set after all chunks known
            chunk_level   = level,
            section_title = section_title,
            page_start    = page_start,
            page_end      = page_end,
            content_types = content_types or ["text"],
            chunk_text    = text,
        )
        self._chunks.append(chunk)
        self._idx += 1

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def _level_summary(self) -> str:
        from collections import Counter
        c = Counter(ch.chunk_level for ch in self._chunks)
        return "  ".join(f"{k}={v}" for k, v in sorted(c.items()))


# ─────────────────────────────────────────────────────────────────────────────
# Convenience entry point for main.py
# ─────────────────────────────────────────────────────────────────────────────

def chunk_document(struct: DocumentStructure) -> list[Chunk]:
    """
    Top-level function called by main.py.
    Returns list[Chunk] — never raises.
    """
    try:
        return Chunker(struct).chunk()
    except Exception as e:
        logger.error(
            f"chunk_document fatal error {struct.file_name}: {e}",
            exc_info=True,
        )
        return []
