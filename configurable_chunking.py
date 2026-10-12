"""Opt-in token-aware section chunking; legacy Chunker remains the default.

Uses the embedding tokenizer, section headings, and paragraph/sentence boundaries.
Small sections merge forward where possible. Hard token limits are enforced.
"""
from __future__ import annotations

import re
from chunker import Chunk, _make_chunk_id, _page_full_text
from pdf_processor import ElementType

_NUMBERED = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\s+([A-Za-z][^\n]{1,120})\s*$")
_COMMON = re.compile(r"^\s*(?:SECTION|CHAPTER|APPENDIX|ANNEX)\s+\w.{0,100}$", re.I)


def _heading(line: str, toc_titles: set[str]) -> tuple[int, str] | None:
    value = line.strip()
    if not value or len(value) > 150:
        return None
    numbered = _NUMBERED.fullmatch(value)
    if numbered:
        return len(numbered.group(1).split(".")), value
    if value.casefold() in toc_titles or _COMMON.fullmatch(value):
        return 1, value
    return None


def _units(struct) -> list[tuple[str, int, tuple[str, ...]]]:
    """Preserve page provenance and heading hierarchy, including same-page sections."""
    toc_titles = {entry.title.strip().casefold() for entry in struct.toc}
    toc_pages = sorted(struct.toc, key=lambda item: item.page_number)
    headings: list[str] = []
    units = []
    toc_index = 0
    for page in sorted(struct.pages, key=lambda p: p.page_number):
        if page.is_blank or page.error:
            continue
        while toc_index < len(toc_pages) and toc_pages[toc_index].page_number <= page.page_number:
            entry = toc_pages[toc_index]
            depth = max(1, entry.level)
            headings = headings[:depth - 1] + [entry.title]
            toc_index += 1
        elements = [e for e in page.elements if e.text]
        for el in elements:
            value = el.text.strip()
            # A table/image is an indivisible unit until oversized-unit fallback.
            if el.element_type != ElementType.TEXT:
                units.append((value, page.page_number, tuple(headings)))
                continue
            for line in value.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                heading = _heading(stripped, toc_titles)
                if heading:
                    depth, title = heading
                    headings = headings[:depth - 1] + [title]
                units.append((stripped, page.page_number, tuple(headings)))
        if not elements and page.raw_text:
            units.append((page.raw_text.strip(), page.page_number, tuple(headings)))
    return units


def chunk_with_profile(struct, policy: dict) -> list[Chunk]:
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError("Install tiktoken for token-aware chunking") from exc

    encoder = tiktoken.get_encoding("cl100k_base")
    maximum = policy["max_tokens"]
    minimum = policy["min_tokens"]
    overlap = policy["overlap_tokens"]
    keep_headers = policy.get("preserve_headers", True)
    chunks = []
    pending: list[tuple[str, int, tuple[str, ...]]] = []
    pending_tokens = 0

    def size(text):
        return len(encoder.encode(text, disallowed_special=()))

    def header(parts):
        names = parts[0][2] if parts else ()
        return (names[-1] + "\n\n") if keep_headers and names else ""

    def emit(parts):
        if not parts:
            return
        hdr = header(parts)
        body = "\n".join(p[0] for p in parts)
        combined = body if body.startswith(hdr.strip()) else hdr + body
        # Bound even if an unusual heading alone is exceptionally large.
        ids = encoder.encode(combined, disallowed_special=())
        if len(ids) > maximum:
            combined = encoder.decode(ids[:maximum])
        seen_headers = list(dict.fromkeys(name for _, _, path in parts for name in path))
        first, last = min(p[1] for p in parts), max(p[1] for p in parts)
        chunk = Chunk(
            chunk_id=_make_chunk_id(struct.doc_id, len(chunks)),
            doc_id=struct.doc_id, file_name=struct.file_name, file_path=struct.file_path,
            chunk_index=len(chunks), chunk_total=0, chunk_level="section",
            section_title=parts[0][2][-1] if parts[0][2] else "",
            page_start=first, page_end=last,
            content_types=["text"], chunk_text=combined,
        )
        chunk.headers = seen_headers
        chunks.append(chunk)

    # Section grouping: prefer preserving headings, merge only if section too small.
    for raw_text, page, headings in _units(struct):
        if not raw_text:
            continue
        parts = [(raw_text, page, headings)]
        # Token slicing guarantees termination even for unpunctuated or huge blocks.
        # Keep some room for the section title; the exact boundary is checked below.
        budget = max(8, maximum - size(headings[-1] + "\n\n") if keep_headers and headings else maximum)
        raw_ids = encoder.encode(raw_text, disallowed_special=())
        if len(raw_ids) > budget:
            parts = [
                (encoder.decode(raw_ids[n:n + budget]), page, headings)
                for n in range(0, len(raw_ids), budget)
            ]
        for part in parts:
            block_tokens = size(part[0]) + 1
            changed_section = bool(pending and pending[-1][2] != part[2])
            exceeds = bool(pending and
                           pending_tokens + block_tokens + size(header(pending)) > maximum)
            if pending and (exceeds or (changed_section and pending_tokens >= minimum)):
                emit(pending)
                previous = pending
                pending = []
                pending_tokens = 0
                # Context overlap applies only to size-driven splits in the same section.
                if exceeds and not changed_section and overlap:
                    tail = encoder.encode(previous[-1][0], disallowed_special=())[-overlap:]
                    if tail:
                        carry = (encoder.decode(tail), previous[-1][1], previous[-1][2])
                        if size(carry[0]) + block_tokens + size(header([carry])) <= maximum:
                            pending.append(carry)
                            pending_tokens += size(carry[0]) + 1
            pending.append(part)
            pending_tokens += block_tokens
    if pending:
        # A short last section is retained rather than silently dropped.
        emit(pending)
    for ch in chunks:
        ch.chunk_total = len(chunks)
    return chunks
