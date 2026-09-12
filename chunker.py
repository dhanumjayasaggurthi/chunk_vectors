"""Structure-aware, token-bounded chunking for MIR-AI.

Preserves the existing heading/TOC-first architecture while enforcing the new
1200–1500 token target, ~100 token text overlap, and atomic tables.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from config import (
    CHUNK_MIN_CHARS,
    CHUNK_MIN_TOKENS,
    CHUNK_TARGET_TOKENS,
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_TOKENS,
)
from logger import get_logger, DocLogger
from pdf_processor import DocumentStructure, PageContent, PageElement, ElementType
from token_utils import count_tokens, tail_tokens, truncate_tokens

logger = get_logger("epod.chunker")


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    file_name: str
    file_path: str
    chunk_index: int
    chunk_total: int
    chunk_level: str
    section_title: str
    page_start: int
    page_end: int
    content_types: list[str]
    chunk_text: str
    chunk_vector: list[float] = field(default_factory=list, repr=False)
    metadata: dict = field(default_factory=dict)

    def to_db_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "chunk_index": self.chunk_index,
            "chunk_total": self.chunk_total,
            "chunk_level": self.chunk_level,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "content_types": self.content_types,
            "chunk_text": self.chunk_text,
            "chunk_vector": self.chunk_vector,
            "metadata": self.metadata,
        }


@dataclass
class _Atom:
    text: str
    page_start: int
    page_end: int
    content_types: list[str]
    atomic: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


def _make_chunk_id(doc_id: str, chunk_index: int) -> str:
    return f"chunk-{hashlib.sha256(f'{doc_id}:{chunk_index}'.encode()).hexdigest()[:32]}"


def _extract_page_heading(page: PageContent) -> Optional[str]:
    for el in sorted(page.elements, key=lambda e: e.reading_order):
        if el.element_type != ElementType.TEXT:
            continue
        t = (el.text or "").strip().splitlines()[0] if el.text else ""
        if not t or len(t) > 140:
            continue
        if re.match(r"^\d+(?:\.\d+)*\s+\S", t) or re.match(r"^(SECTION|CHAPTER|ANNEX|APPENDIX|PART)\b", t, re.I):
            return t
    return None


def _split_long_text(text: str, max_tokens: int = CHUNK_MAX_TOKENS) -> list[tuple[str, bool]]:
    """Split prose at paragraph/sentence boundaries. bool marks unavoidable hard split."""
    if count_tokens(text) <= max_tokens:
        return [(text.strip(), False)]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paragraphs or [text]:
        if count_tokens(p) <= max_tokens:
            units.append(p)
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", p) if s.strip()]
        if len(sentences) == 1 and count_tokens(p) > max_tokens:
            # Last resort for pathological unbroken text. Preserve every character.
            remaining = p
            while remaining:
                piece = truncate_tokens(remaining, max_tokens)
                if not piece:
                    break
                units.append(piece)
                remaining = remaining[len(piece):]
        else:
            units.extend(sentences)

    out: list[tuple[str, bool]] = []
    cur: list[str] = []
    for unit in units:
        candidate = "\n\n".join(cur + [unit])
        if cur and count_tokens(candidate) > max_tokens:
            out.append(("\n\n".join(cur), False))
            cur = [unit]
        elif count_tokens(unit) > max_tokens:
            # Can only occur with fallback token estimator edge cases.
            piece = truncate_tokens(unit, max_tokens)
            out.append((piece, True))
            rest = unit[len(piece):]
            cur = [rest] if rest else []
        else:
            cur.append(unit)
    if cur:
        out.append(("\n\n".join(cur), False))
    return [(x, hard) for x, hard in out if x.strip()]


def _element_atom(page: PageContent, el: PageElement) -> list[_Atom]:
    text = (el.text or "").strip()
    if not text:
        return []
    typ = el.element_type.value
    meta = dict(getattr(el, "metadata", {}) or {})
    if el.element_type == ElementType.TABLE:
        # A table and its summary live inside el.text and remain indivisible.
        meta.setdefault("bbox", [el.bbox.x0, el.bbox.y0, el.bbox.x1, el.bbox.y1])
        return [_Atom(text, page.page_number, page.page_number, ["table"], True, meta)]
    if el.element_type in (ElementType.IMAGE, ElementType.CHART):
        meta.setdefault("bbox", [el.bbox.x0, el.bbox.y0, el.bbox.x1, el.bbox.y1])
        return [_Atom(text, page.page_number, page.page_number, [typ], True, meta)]
    atoms = []
    for part, forced in _split_long_text(text):
        atoms.append(_Atom(part, page.page_number, page.page_number, [typ], False, {"forced_split": forced}))
    return atoms


def _page_atoms(page: PageContent) -> list[_Atom]:
    atoms: list[_Atom] = []
    for el in sorted(page.elements, key=lambda e: e.reading_order):
        atoms.extend(_element_atom(page, el))
    # Full-page OCR can leave useful raw text without elements.
    if not atoms and page.raw_text.strip():
        for part, forced in _split_long_text(page.raw_text):
            atoms.append(_Atom(part, page.page_number, page.page_number, ["ocr" if page.needs_ocr else "text"], False, {"forced_split": forced}))
    return atoms


def _merge_meta(atoms: list[_Atom]) -> dict:
    pages = sorted({p for a in atoms for p in range(a.page_start, a.page_end + 1)})
    bboxes, originals, table_groups = [], [], []
    forced = False
    oversize = False
    for a in atoms:
        m = a.metadata
        if m.get("bbox"):
            bboxes.append({"page": a.page_start, "bbox": m["bbox"], "type": a.content_types[0]})
        if m.get("bboxes"):
            for pn, bb in zip(m.get("page_numbers", []), m["bboxes"]):
                bboxes.append({"page": pn, "bbox": bb, "type": "table"})
        if m.get("original_text"):
            originals.append(m["original_text"])
        if m.get("table_group_id"):
            table_groups.append(m["table_group_id"])
        forced = forced or bool(m.get("forced_split"))
        oversize = oversize or bool(m.get("oversize_atomic"))
    return {
        "page_label": pages,
        "bboxes": bboxes,
        "original_text": "\n\n".join(originals) if originals else None,
        "table_group_ids": sorted(set(table_groups)),
        "forced_split": forced,
        "oversize_atomic": oversize,
    }


class Chunker:
    def __init__(self, struct: DocumentStructure):
        self.struct = struct
        self.doc_id = struct.doc_id
        self.file_name = struct.file_name
        self.file_path = struct.file_path
        self.dlog = DocLogger(self.file_name, self.doc_id, "epod.chunker")
        self._chunks: list[Chunk] = []
        self._idx = 0

    def chunk(self) -> list[Chunk]:
        sections = self._sections()
        for title, pages in sections:
            atoms = [a for p in pages if not p.is_blank and not p.error for a in _page_atoms(p)]
            if atoms:
                self._pack_section(title, atoms)
        total = len(self._chunks)
        for c in self._chunks:
            c.chunk_total = total
        self.dlog.info(f"Chunking complete — {total} chunks")
        return self._chunks

    def _sections(self) -> list[tuple[str, list[PageContent]]]:
        pages = {p.page_number: p for p in self.struct.pages}
        if self.struct.has_toc and self.struct.toc:
            out = []
            toc = self.struct.toc
            for i, entry in enumerate(toc):
                start = max(1, entry.page_number)
                end = min(self.struct.page_count, (toc[i+1].page_number - 1) if i + 1 < len(toc) else self.struct.page_count)
                sec_pages = [pages[n] for n in range(start, end + 1) if n in pages]
                if sec_pages:
                    out.append((entry.title, sec_pages))
            return out

        out: list[tuple[str, list[PageContent]]] = []
        title = ""
        group: list[PageContent] = []
        for p in self.struct.pages:
            heading = _extract_page_heading(p)
            if heading and group:
                out.append((title, group))
                group = []
            if heading:
                title = heading
            group.append(p)
        if group:
            out.append((title, group))
        return out

    def _pack_section(self, title: str, atoms: list[_Atom]) -> None:
        cur: list[_Atom] = []
        cur_tokens = 0
        previous_text = ""

        def flush():
            nonlocal cur, cur_tokens, previous_text
            if not cur:
                return
            text = "\n\n".join(a.text for a in cur).strip()
            # Add overlap only for normal text chunks, never by slicing atomic data.
            if previous_text and not cur[0].atomic:
                overlap = tail_tokens(previous_text, CHUNK_OVERLAP_TOKENS).strip()
                if overlap and count_tokens(overlap + "\n\n" + text) <= CHUNK_MAX_TOKENS:
                    text = overlap + "\n\n" + text
            self._emit(title, cur, text)
            previous_text = text
            cur, cur_tokens = [], 0

        for atom in atoms:
            t = atom.tokens
            if atom.atomic and t > CHUNK_MAX_TOKENS:
                flush()
                atom.metadata["oversize_atomic"] = True
                atom.metadata["oversize_reason"] = "semantic unit preserved intact"
                self._emit(title, [atom], atom.text)
                previous_text = atom.text
                continue

            if not cur:
                cur = [atom]
                cur_tokens = t
                continue

            candidate = "\n\n".join(a.text for a in cur + [atom])
            candidate_tokens = count_tokens(candidate)
            if candidate_tokens <= CHUNK_TARGET_TOKENS:
                cur.append(atom)
                cur_tokens = candidate_tokens
                continue
            if candidate_tokens <= CHUNK_MAX_TOKENS and cur_tokens < CHUNK_MIN_TOKENS:
                cur.append(atom)
                cur_tokens = candidate_tokens
                continue
            flush()
            cur = [atom]
            cur_tokens = t

        flush()

        # Merge a tiny final chunk backward if safe and in the same section.
        if len(self._chunks) >= 2:
            last, prev = self._chunks[-1], self._chunks[-2]
            if last.section_title == title and count_tokens(last.chunk_text) < CHUNK_MIN_TOKENS:
                merged = prev.chunk_text.rstrip() + "\n\n" + last.chunk_text.lstrip()
                if count_tokens(merged) <= CHUNK_MAX_TOKENS and not last.metadata.get("oversize_atomic"):
                    prev.chunk_text = merged
                    prev.page_end = max(prev.page_end, last.page_end)
                    prev.content_types = sorted(set(prev.content_types + last.content_types))
                    prev.metadata["page_label"] = sorted(set(prev.metadata.get("page_label", []) + last.metadata.get("page_label", [])))
                    prev.metadata["bboxes"] = prev.metadata.get("bboxes", []) + last.metadata.get("bboxes", [])
                    if last.metadata.get("original_text"):
                        prev.metadata["original_text"] = "\n\n".join(x for x in [prev.metadata.get("original_text"), last.metadata.get("original_text")] if x)
                    self._chunks.pop()
                    self._idx -= 1

    def _emit(self, title: str, atoms: list[_Atom], text: str) -> None:
        if not text.strip() or len(text.strip()) < CHUNK_MIN_CHARS:
            return
        page_start = min(a.page_start for a in atoms)
        page_end = max(a.page_end for a in atoms)
        types = sorted({t for a in atoms for t in a.content_types}) or ["text"]
        meta = _merge_meta(atoms)
        meta.update({
            "sourcefile_url": getattr(self.struct, "sourcefile_url", "") or self.file_path,
            "full_doc_id": self.doc_id,
            "token_count": count_tokens(text),
            "section_title": title,
        })
        self._chunks.append(Chunk(
            chunk_id=_make_chunk_id(self.doc_id, self._idx),
            doc_id=self.doc_id,
            file_name=self.file_name,
            file_path=self.file_path,
            chunk_index=self._idx,
            chunk_total=0,
            chunk_level="section" if title else "page",
            section_title=title,
            page_start=page_start,
            page_end=page_end,
            content_types=types,
            chunk_text=text.strip(),
            metadata=meta,
        ))
        self._idx += 1


def chunk_document(struct: DocumentStructure) -> list[Chunk]:
    try:
        return Chunker(struct).chunk()
    except Exception as e:
        logger.exception("chunk_document fatal error %s: %s", struct.file_name, e)
        return []
