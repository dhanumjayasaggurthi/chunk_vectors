"""Opt-in token-aware chunking; the legacy chunker remains the default.

Uses heading-aware grouping with exact tokenizer limits, preserving the source
text. Oversized structural elements are split only as a last resort.
"""
from __future__ import annotations

import re
from chunker import Chunk, _make_chunk_id

_NUMBERED = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\s+([A-Za-z][^\n]{1,120})\s*$")
_COMMON = re.compile(r"^\s*(?:SECTION|CHAPTER|APPENDIX|ANNEX)\s+\w.{0,100}$", re.I)


def _heading(text: str, known_titles: set[str]) -> tuple[int, str] | None:
    text = text.strip()
    if not text or len(text) > 150:
        return None
    numbered = _NUMBERED.fullmatch(text)
    if numbered:
        return len(numbered.group(1).split(".")), text
    if text.casefold() in known_titles or _COMMON.fullmatch(text):
        return 1, text
    return None


def _units(struct):
    """Page-traceable blocks with heading paths and original content type."""
    known_titles = {item.title.strip().casefold() for item in struct.toc}
    toc_by_page = {}
    for entry in struct.toc:
        toc_by_page.setdefault(entry.page_number, []).append(entry)
    headings = []
    for page in sorted(struct.pages, key=lambda x: x.page_number):
        if page.is_blank or page.error:
            continue
        toc_entries = toc_by_page.get(page.page_number, [])
        # The first bookmark establishes the page's initial section; text
        # headings handle additional same-page sections.
        if toc_entries:
            first = toc_entries[0]
            headings = headings[:max(0, first.level - 1)] + [first.title]
        elements = [element for element in page.elements if element.text]
        if not elements and page.raw_text:
            yield (page.raw_text.strip(), page.page_number, tuple(headings), "text")
        for element in elements:
            kind = getattr(element.element_type, "value", str(element.element_type))
            if kind != "text":
                yield (element.text.strip(), page.page_number, tuple(headings), kind)
                continue
            # Preserve paragraphs where the extractor provided them.
            for paragraph in re.split(r"\n\s*\n", element.text):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                lines = paragraph.splitlines()
                buffer = []
                for line in lines:
                    matched = _heading(line, known_titles)
                    if matched:
                        if buffer:
                            yield (" ".join(buffer), page.page_number, tuple(headings), kind)
                            buffer = []
                        depth, title = matched
                        headings = headings[:depth - 1] + [title]
                        yield (title, page.page_number, tuple(headings), kind)
                    else:
                        buffer.append(line.strip())
                if buffer:
                    yield (" ".join(buffer), page.page_number, tuple(headings), kind)


def chunk_with_profile(struct, policy: dict) -> list[Chunk]:
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError("tiktoken must be installed for toc_then_semantic") from exc
    encoding_name = policy.get("tokenizer", "cl100k_base")
    if encoding_name not in {"cl100k_base", "o200k_base"}:
        raise ValueError("Unsupported tokenizer; use cl100k_base or o200k_base")
    encoder = tiktoken.get_encoding(encoding_name)
    maximum, minimum, overlap = (
        policy["max_tokens"], policy["min_tokens"], policy["overlap_tokens"]
    )
    preserve = policy.get("preserve_headers", True)
    emitted: list[Chunk] = []
    pending = []

    def tokens(text):
        return encoder.encode(text, disallowed_special=())

    def render(parts):
        body = "\n".join(item[0] for item in parts)
        title = parts[0][2][-1] if preserve and parts[0][2] else ""
        if title and not body.casefold().startswith(title.casefold()):
            return title + "\n\n" + body
        return body

    def emit(parts):
        if not parts:
            return
        value = render(parts)
        if len(tokens(value)) > maximum:
            raise ValueError("Chunk exceeded configured maximum token count")
        paths = [h for item in parts for h in item[2]]
        headers = list(dict.fromkeys(paths))
        kind = sorted({item[3] for item in parts})
        chunk = Chunk(
            chunk_id=_make_chunk_id(struct.doc_id, len(emitted)),
            doc_id=struct.doc_id, file_name=struct.file_name,
            file_path=struct.file_path, chunk_index=len(emitted),
            chunk_total=0, chunk_level="section" if headers else "page",
            section_title=parts[0][2][-1] if parts[0][2] else "",
            page_start=min(item[1] for item in parts),
            page_end=max(item[1] for item in parts),
            content_types=kind, chunk_text=value,
        )
        chunk.headers = headers
        emitted.append(chunk)

    for text, page, path, kind in _units(struct):
        if not text:
            continue
        # Ensure an oversized individual block never forces silent truncation.
        title_size = len(tokens(path[-1] + "\n\n")) if preserve and path else 0
        budget = maximum - title_size - 4
        if budget < 8:
            raise ValueError("Heading too long for selected max_tokens")
        ids = tokens(text)
        if len(ids) <= budget:
            pieces = [text]
        else:
            # Split on whole words where possible; token-slice a single
            # oversized token/word only as the final safety fallback.
            pieces = []
            current = ""
            for word in re.findall(r"\\S+\\s*", text):
                if len(tokens(word)) > budget:
                    if current.strip():
                        pieces.append(current.strip())
                        current = ""
                    word_ids = tokens(word)
                    pieces.extend(
                        encoder.decode(word_ids[offset:offset + budget])
                        for offset in range(0, len(word_ids), budget)
                    )
                elif current and len(tokens(current + word)) > budget:
                    pieces.append(current.strip())
                    current = word
                else:
                    current += word
            if current.strip():
                pieces.append(current.strip())
        for piece in pieces:
            unit = (piece, page, path, kind)
            if pending:
                new_section = path != pending[-1][2]
                overflow = len(tokens(render(pending + [unit]))) > maximum
                if overflow or (new_section and len(tokens(render(pending))) >= minimum):
                    previous = pending
                    emit(previous)
                    pending = []
                    if overflow and not new_section and overlap:
                        previous_ids = tokens(previous[-1][0])
                        tail = encoder.decode(previous_ids[-overlap:])
                        carry = (tail, previous[-1][1], previous[-1][2], previous[-1][3])
                        if len(tokens(render([carry, unit]))) <= maximum:
                            pending = [carry]
            pending.append(unit)
            if len(tokens(render(pending))) > maximum:
                raise ValueError("Individual unit cannot fit in configured token budget")
    emit(pending)
    for chunk in emitted:
        chunk.chunk_total = len(emitted)
    return emitted
