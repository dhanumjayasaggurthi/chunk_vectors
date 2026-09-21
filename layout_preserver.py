from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

import fitz  # PyMuPDF

SCHEMA_VERSION = "1.0.0"


def _round(v: float, ndigits: int = 4) -> float:
    return round(float(v), ndigits)


def _bbox(value: Iterable[float]) -> list[float]:
    vals = list(value)
    return [_round(x) for x in vals[:4]]


def _point(value: Iterable[float]) -> list[float]:
    vals = list(value)
    return [_round(x) for x in vals[:2]]


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def _color_hex(color: Any) -> Optional[str]:
    if color is None:
        return None
    if isinstance(color, int):
        return f"#{(color >> 16) & 0xFF:02x}{(color >> 8) & 0xFF:02x}{color & 0xFF:02x}"
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        vals = []
        for c in color[:3]:
            c = float(c)
            if c <= 1.0:
                c *= 255.0
            vals.append(max(0, min(255, int(round(c)))))
        return "#" + "".join(f"{v:02x}" for v in vals)
    return None


def _serialise_obj(obj: Any) -> Any:
    """Convert PyMuPDF geometry and nested objects into JSON-safe values."""
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        return _round(obj)
    if isinstance(obj, fitz.Point):
        return [_round(obj.x), _round(obj.y)]
    if isinstance(obj, fitz.Rect):
        return _bbox(obj)
    if isinstance(obj, fitz.Quad):
        return [_serialise_obj(p) for p in (obj.ul, obj.ur, obj.ll, obj.lr)]
    if isinstance(obj, fitz.Matrix):
        return [_round(x) for x in (obj.a, obj.b, obj.c, obj.d, obj.e, obj.f)]
    if isinstance(obj, bytes):
        return {"sha256": hashlib.sha256(obj).hexdigest(), "length": len(obj)}
    if isinstance(obj, (tuple, list)):
        return [_serialise_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _serialise_obj(v) for k, v in obj.items()}
    return str(obj)


@dataclass
class CharRecord:
    c: str
    bbox: list[float]
    origin: list[float]
    synthetic: bool = False


@dataclass
class SpanRecord:
    span_index: int
    text: str
    bbox: list[float]
    origin: list[float]
    font: str
    size: float
    flags: int
    bold: bool
    italic: bool
    serif: bool
    monospace: bool
    superscript: bool
    color: Optional[str]
    alpha: Optional[int]
    ascender: Optional[float]
    descender: Optional[float]
    chars: list[CharRecord] = field(default_factory=list)


@dataclass
class LineRecord:
    line_index: int
    bbox: list[float]
    direction: list[float]
    writing_mode: int
    spans: list[SpanRecord]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class TextBlockRecord:
    block_index: int
    source_order: int
    bbox: list[float]
    lines: list[LineRecord]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class ImageRecord:
    block_index: int
    source_order: int
    bbox: list[float]
    width: Optional[int]
    height: Optional[int]
    ext: Optional[str]
    colorspace: Optional[int]
    xres: Optional[int]
    yres: Optional[int]
    transform: Any
    asset_sha256: Optional[str]
    asset_path: Optional[str]
    asset_length: Optional[int]


@dataclass
class DrawingRecord:
    drawing_index: int
    bbox: list[float]
    stroke: Optional[str]
    fill: Optional[str]
    width: Optional[float]
    dashes: Optional[str]
    close_path: Optional[bool]
    even_odd: Optional[bool]
    opacity: Optional[float]
    items: Any


@dataclass
class TableCellRecord:
    row: int
    col: int
    bbox: Optional[list[float]]
    text: str


@dataclass
class TableRecord:
    table_index: int
    bbox: list[float]
    row_count: int
    col_count: int
    cells: list[TableCellRecord]


@dataclass
class LinkRecord:
    link_index: int
    bbox: list[float]
    kind: int
    uri: Optional[str]
    page: Optional[int]
    to: Any
    xref: Optional[int]


@dataclass
class PageRecord:
    page_number: int
    width: float
    height: float
    rotation: int
    mediabox: list[float]
    cropbox: list[float]
    text_blocks: list[TextBlockRecord]
    images: list[ImageRecord]
    drawings: list[DrawingRecord]
    tables: list[TableRecord]
    links: list[LinkRecord]

    @property
    def exact_text(self) -> str:
        # Preserve raw line breaks and span characters; do not normalize spaces.
        return "\n".join(block.text for block in self.text_blocks)


@dataclass
class SectionFragment:
    page_number: int
    bbox: list[float]


@dataclass
class SectionRecord:
    section_id: str
    title: str
    level: int
    source: str
    start_page: int
    end_page: int
    anchor_bbox: Optional[list[float]]
    fragments: list[SectionFragment]


@dataclass
class DocumentLayout:
    schema_version: str
    source_file: str
    source_sha256: str
    page_count: int
    metadata: dict[str, Any]
    toc: list[dict[str, Any]]
    pages: list[PageRecord]
    sections: list[SectionRecord]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save_json(self, path: str | Path, compress: Optional[bool] = None) -> Path:
        p = Path(path)
        if compress is None:
            compress = p.suffix.lower() == ".gz"
        payload = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if compress:
            if p.suffix.lower() != ".gz":
                p = p.with_suffix(p.suffix + ".gz")
            with gzip.open(p, "wt", encoding="utf-8") as f:
                f.write(payload)
        else:
            p.write_text(payload, encoding="utf-8")
        return p

    def get_section(self, query: str) -> SectionRecord:
        q = _normalize_text(query)
        if not q:
            raise KeyError("section query is empty")
        exact = [s for s in self.sections if _normalize_text(s.title) == q]
        if exact:
            return exact[0]
        contains = [s for s in self.sections if q in _normalize_text(s.title)]
        if len(contains) == 1:
            return contains[0]
        raise KeyError(f"section not uniquely found: {query!r}")


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _span_from_raw(span: dict[str, Any], span_index: int) -> SpanRecord:
    chars: list[CharRecord] = []
    for ch in span.get("chars", []):
        chars.append(
            CharRecord(
                c=ch.get("c", ""),
                bbox=_bbox(ch.get("bbox", (0, 0, 0, 0))),
                origin=_point(ch.get("origin", (0, 0))),
                synthetic=bool(ch.get("synthetic", False)),
            )
        )
    # rawdict exposes chars, not a span-level text field. Joining chars retains
    # spaces exactly as represented by the PDF text layer.
    text = "".join(c.c for c in chars)
    flags = int(span.get("flags", 0) or 0)
    origin = span.get("origin")
    if origin is None and chars:
        origin = chars[0].origin
    elif origin is None:
        origin = (0, 0)
    return SpanRecord(
        span_index=span_index,
        text=text,
        bbox=_bbox(span.get("bbox", (0, 0, 0, 0))),
        origin=_point(origin),
        font=str(span.get("font", "")),
        size=_round(span.get("size", 0.0)),
        flags=flags,
        bold=bool(flags & 16),
        italic=bool(flags & 2),
        serif=bool(flags & 4),
        monospace=bool(flags & 8),
        superscript=bool(flags & 1),
        color=_color_hex(span.get("color")),
        alpha=span.get("alpha"),
        ascender=_round(span["ascender"]) if span.get("ascender") is not None else None,
        descender=_round(span["descender"]) if span.get("descender") is not None else None,
        chars=chars,
    )


def _extract_text_and_images(page: fitz.Page, asset_dir: Optional[Path]) -> tuple[list[TextBlockRecord], list[ImageRecord]]:
    flags = getattr(fitz, "TEXTFLAGS_RAWDICT", 0) | getattr(fitz, "TEXT_PRESERVE_WHITESPACE", 0)
    raw = page.get_text("rawdict", flags=flags)
    text_blocks: list[TextBlockRecord] = []
    images: list[ImageRecord] = []

    for source_order, block in enumerate(raw.get("blocks", [])):
        block_type = int(block.get("type", -1))
        if block_type == 0:
            lines: list[LineRecord] = []
            for li, line in enumerate(block.get("lines", [])):
                spans = [_span_from_raw(s, si) for si, s in enumerate(line.get("spans", []))]
                lines.append(
                    LineRecord(
                        line_index=li,
                        bbox=_bbox(line.get("bbox", (0, 0, 0, 0))),
                        direction=_point(line.get("dir", (1, 0))),
                        writing_mode=int(line.get("wmode", 0) or 0),
                        spans=spans,
                    )
                )
            text_blocks.append(
                TextBlockRecord(
                    block_index=int(block.get("number", source_order)),
                    source_order=source_order,
                    bbox=_bbox(block.get("bbox", (0, 0, 0, 0))),
                    lines=lines,
                )
            )
        elif block_type == 1:
            data = block.get("image")
            digest = hashlib.sha256(data).hexdigest() if isinstance(data, (bytes, bytearray)) else None
            asset_path = None
            if digest and asset_dir is not None:
                asset_dir.mkdir(parents=True, exist_ok=True)
                ext = block.get("ext") or "bin"
                target = asset_dir / f"{digest}.{ext}"
                if not target.exists():
                    target.write_bytes(bytes(data))
                asset_path = target.name
            images.append(
                ImageRecord(
                    block_index=int(block.get("number", source_order)),
                    source_order=source_order,
                    bbox=_bbox(block.get("bbox", (0, 0, 0, 0))),
                    width=block.get("width"),
                    height=block.get("height"),
                    ext=block.get("ext"),
                    colorspace=block.get("colorspace"),
                    xres=block.get("xres"),
                    yres=block.get("yres"),
                    transform=_serialise_obj(block.get("transform")),
                    asset_sha256=digest,
                    asset_path=asset_path,
                    asset_length=len(data) if isinstance(data, (bytes, bytearray)) else None,
                )
            )
    return text_blocks, images


def _extract_drawings(page: fitz.Page) -> list[DrawingRecord]:
    records: list[DrawingRecord] = []
    try:
        drawings = page.get_drawings(extended=True)
    except TypeError:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    for i, d in enumerate(drawings):
        rect = d.get("rect") or d.get("bbox") or (0, 0, 0, 0)
        records.append(
            DrawingRecord(
                drawing_index=i,
                bbox=_bbox(rect),
                stroke=_color_hex(d.get("color")),
                fill=_color_hex(d.get("fill")),
                width=_round(d["width"]) if d.get("width") is not None else None,
                dashes=d.get("dashes"),
                close_path=d.get("closePath"),
                even_odd=d.get("even_odd"),
                opacity=_round(d.get("stroke_opacity", d.get("opacity", 1.0))) if d.get("stroke_opacity", d.get("opacity")) is not None else None,
                items=_serialise_obj(d.get("items", [])),
            )
        )
    return records


def _extract_tables(page: fitz.Page) -> list[TableRecord]:
    results: list[TableRecord] = []
    try:
        finder = page.find_tables()
        tables = getattr(finder, "tables", []) or []
    except Exception:
        tables = []

    for ti, table in enumerate(tables):
        try:
            values = table.extract() or []
        except Exception:
            values = []
        rows = getattr(table, "rows", []) or []
        row_count = int(getattr(table, "row_count", len(rows)) or len(rows))
        col_count = int(getattr(table, "col_count", max((len(r) for r in values), default=0)) or 0)
        cells: list[TableCellRecord] = []
        for r in range(max(row_count, len(values))):
            row_obj = rows[r] if r < len(rows) else None
            row_cells = getattr(row_obj, "cells", []) if row_obj is not None else []
            value_row = values[r] if r < len(values) else []
            max_cols = max(col_count, len(row_cells), len(value_row))
            for c in range(max_cols):
                cb = row_cells[c] if c < len(row_cells) else None
                text = value_row[c] if c < len(value_row) else ""
                if text is None:
                    text = ""
                cells.append(
                    TableCellRecord(
                        row=r,
                        col=c,
                        bbox=_bbox(cb) if cb is not None else None,
                        text=str(text),
                    )
                )
        results.append(
            TableRecord(
                table_index=ti,
                bbox=_bbox(getattr(table, "bbox", (0, 0, 0, 0))),
                row_count=row_count,
                col_count=col_count,
                cells=cells,
            )
        )
    return results


def _extract_links(page: fitz.Page) -> list[LinkRecord]:
    links: list[LinkRecord] = []
    try:
        raw = page.get_links()
    except Exception:
        raw = []
    for i, link in enumerate(raw):
        links.append(
            LinkRecord(
                link_index=i,
                bbox=_bbox(link.get("from", (0, 0, 0, 0))),
                kind=int(link.get("kind", 0) or 0),
                uri=link.get("uri"),
                page=link.get("page"),
                to=_serialise_obj(link.get("to")),
                xref=link.get("xref"),
            )
        )
    return links


def _heading_candidates(page: PageRecord) -> list[tuple[str, float, list[float], float, bool]]:
    spans = [s for b in page.text_blocks for l in b.lines for s in l.spans if s.text.strip()]
    if not spans:
        return []
    sizes = [s.size for s in spans if s.size > 0]
    base = median(sizes) if sizes else 10.0
    out = []
    for block in page.text_blocks:
        for line in block.lines:
            text = line.text.strip()
            if not text or len(text) > 180:
                continue
            nonempty = [s for s in line.spans if s.text.strip()]
            if not nonempty:
                continue
            max_size = max(s.size for s in nonempty)
            is_bold = any(s.bold for s in nonempty)
            heading_like = (
                max_size >= base * 1.22
                or (is_bold and len(text) <= 120)
                or bool(re.match(r"^(?:[IVXLC]+\.|\d+(?:\.\d+)*\.?|SECTION|PART|CHAPTER|APPENDIX|ANNEX)\s+", text, re.I))
            )
            if heading_like:
                out.append((text, line.bbox[1], line.bbox, max_size, is_bold))
    return out


def _find_title_anchor(page: PageRecord, title: str) -> Optional[list[float]]:
    target = _normalize_text(title)
    if not target:
        return None
    candidates = []
    for block in page.text_blocks:
        for line in block.lines:
            line_text = _normalize_text(line.text)
            if not line_text:
                continue
            if line_text == target:
                return line.bbox
            if target in line_text or line_text in target:
                # Prefer close length matches, then topmost.
                score = abs(len(line_text) - len(target))
                candidates.append((score, line.bbox[1], line.bbox))
    return min(candidates, default=(None, None, None))[2] if candidates else None


def _make_fragments(
    pages: list[PageRecord],
    start_page: int,
    start_y: float,
    boundary_page: Optional[int],
    boundary_y: Optional[float],
) -> list[SectionFragment]:
    fragments: list[SectionFragment] = []
    last_page = len(pages)
    end_page = boundary_page if boundary_page is not None else last_page

    for pn in range(start_page, end_page + 1):
        page = pages[pn - 1]
        y0 = start_y if pn == start_page else 0.0
        y1 = page.height
        if boundary_page is not None and pn == boundary_page:
            y1 = max(0.0, min(page.height, boundary_y if boundary_y is not None else 0.0))
        if y1 - y0 > 0.1:
            fragments.append(SectionFragment(page_number=pn, bbox=[0.0, _round(y0), _round(page.width), _round(y1)]))
    return fragments


def _build_sections(doc: fitz.Document, pages: list[PageRecord], toc_rows: list[dict[str, Any]]) -> list[SectionRecord]:
    anchors: list[dict[str, Any]] = []
    if toc_rows:
        for i, item in enumerate(toc_rows):
            pn = max(1, min(len(pages), int(item["page_number"])))
            anchor = _find_title_anchor(pages[pn - 1], str(item["title"]))
            anchors.append(
                {
                    "title": str(item["title"]),
                    "level": int(item["level"]),
                    "page": pn,
                    "y": anchor[1] if anchor else 0.0,
                    "bbox": anchor,
                    "source": "toc",
                    "order": i,
                }
            )
    else:
        order = 0
        for page in pages:
            for text, y, bbox, size, is_bold in _heading_candidates(page):
                anchors.append(
                    {
                        "title": text,
                        "level": 1,
                        "page": page.page_number,
                        "y": y,
                        "bbox": bbox,
                        "source": "heuristic",
                        "order": order,
                    }
                )
                order += 1
        # Remove repeated page headers / duplicates at same normalized title + page.
        seen = set()
        uniq = []
        for a in anchors:
            k = (a["page"], _normalize_text(a["title"]), round(a["y"], 1))
            if k not in seen:
                seen.add(k)
                uniq.append(a)
        anchors = uniq

    if not anchors:
        page = pages[0] if pages else None
        if page is None:
            return []
        return [
            SectionRecord(
                section_id="document",
                title="Document",
                level=1,
                source="fallback",
                start_page=1,
                end_page=len(pages),
                anchor_bbox=None,
                fragments=_make_fragments(pages, 1, 0.0, None, None),
            )
        ]

    # Preserve bookmark order but make sure page/y progression is monotonic for clips.
    anchors.sort(key=lambda a: (a["page"], a["y"], a["order"]))
    sections: list[SectionRecord] = []
    for i, a in enumerate(anchors):
        # Boundary = next section of same or higher hierarchy (level <= current).
        boundary = None
        for b in anchors[i + 1 :]:
            if b["level"] <= a["level"]:
                boundary = b
                break
        fragments = _make_fragments(
            pages,
            start_page=a["page"],
            start_y=float(a["y"]),
            boundary_page=boundary["page"] if boundary else None,
            boundary_y=float(boundary["y"]) if boundary else None,
        )
        if fragments:
            end_page = fragments[-1].page_number
        else:
            end_page = a["page"]
        seed = f"{a['level']}|{a['page']}|{a['y']:.3f}|{a['title']}"
        sid = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        sections.append(
            SectionRecord(
                section_id=sid,
                title=a["title"],
                level=a["level"],
                source=a["source"],
                start_page=a["page"],
                end_page=end_page,
                anchor_bbox=a["bbox"],
                fragments=fragments,
            )
        )
    return sections


def extract_document_layout(pdf_path: str | Path, asset_dir: str | Path | None = None) -> DocumentLayout:
    """
    Extract a lossless-enough structural manifest from a native-text PDF.

    The manifest preserves character text/whitespace, span typography, absolute
    bounding boxes, page geometry, drawings, tables and links. Exact visual
    retrieval is done from the original PDF with extract_section_pdf(), which
    copies the source page content rather than re-typesetting it.
    """
    pdf_path = Path(pdf_path)
    assets = Path(asset_dir) if asset_dir is not None else None
    if assets is not None:
        assets.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("Password-protected PDF is not supported")

    metadata = dict(doc.metadata or {})
    try:
        toc_raw = doc.get_toc(simple=True) or []
    except Exception:
        toc_raw = []
    toc = [
        {"level": int(row[0]), "title": str(row[1]), "page_number": int(row[2])}
        for row in toc_raw
        if len(row) >= 3 and int(row[2]) > 0
    ]

    pages: list[PageRecord] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        text_blocks, images = _extract_text_and_images(page, assets)
        pages.append(
            PageRecord(
                page_number=pno + 1,
                width=_round(page.rect.width),
                height=_round(page.rect.height),
                rotation=int(page.rotation),
                mediabox=_bbox(page.mediabox),
                cropbox=_bbox(page.cropbox),
                text_blocks=text_blocks,
                images=images,
                drawings=_extract_drawings(page),
                tables=_extract_tables(page),
                links=_extract_links(page),
            )
        )

    sections = _build_sections(doc, pages, toc)
    layout = DocumentLayout(
        schema_version=SCHEMA_VERSION,
        source_file=pdf_path.name,
        source_sha256=_sha256_file(pdf_path),
        page_count=doc.page_count,
        metadata=metadata,
        toc=toc,
        pages=pages,
        sections=sections,
    )
    doc.close()
    return layout


def extract_section_pdf(
    source_pdf: str | Path,
    layout: DocumentLayout,
    section: str | SectionRecord,
    output_pdf: str | Path,
) -> Path:
    """Create a vector-preserving PDF containing exactly the section clips."""
    if isinstance(section, str):
        section = layout.get_section(section)
    source_pdf = Path(source_pdf)
    output_pdf = Path(output_pdf)
    src = fitz.open(source_pdf)
    out = fitz.open()

    for fragment in section.fragments:
        src_page = src[fragment.page_number - 1]
        clip = fitz.Rect(fragment.bbox)
        clip &= src_page.rect
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            continue
        new_page = out.new_page(width=clip.width, height=clip.height)
        new_page.show_pdf_page(new_page.rect, src, fragment.page_number - 1, clip=clip, keep_proportion=False)

    if out.page_count == 0:
        out.close()
        src.close()
        raise ValueError(f"Section {section.title!r} produced no non-empty fragments")

    out.save(output_pdf, garbage=4, deflate=True)
    out.close()
    src.close()
    return output_pdf


def reconstruct_page_text(page: PageRecord) -> str:
    return page.exact_text


def section_manifest(layout: DocumentLayout, section: str | SectionRecord) -> dict[str, Any]:
    if isinstance(section, str):
        section = layout.get_section(section)
    page_map = {p.page_number: p for p in layout.pages}
    return {
        "section": asdict(section),
        "pages": [
            {
                "page_number": f.page_number,
                "clip_bbox": f.bbox,
                "page": asdict(page_map[f.page_number]),
            }
            for f in section.fragments
        ],
    }


def load_document_layout(path: str | Path) -> DocumentLayout:
    """Load a manifest produced by DocumentLayout.save_json()."""
    p = Path(path)
    if p.suffix.lower() == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(p.read_text(encoding="utf-8"))

    pages: list[PageRecord] = []
    for pd in data.get("pages", []):
        text_blocks = []
        for bd in pd.get("text_blocks", []):
            lines = []
            for ld in bd.get("lines", []):
                spans = []
                for sd in ld.get("spans", []):
                    chars = [CharRecord(**cd) for cd in sd.get("chars", [])]
                    spans.append(SpanRecord(**{**sd, "chars": chars}))
                lines.append(LineRecord(**{**ld, "spans": spans}))
            text_blocks.append(TextBlockRecord(**{**bd, "lines": lines}))
        images = [ImageRecord(**x) for x in pd.get("images", [])]
        drawings = [DrawingRecord(**x) for x in pd.get("drawings", [])]
        tables = []
        for td in pd.get("tables", []):
            cells = [TableCellRecord(**c) for c in td.get("cells", [])]
            tables.append(TableRecord(**{**td, "cells": cells}))
        links = [LinkRecord(**x) for x in pd.get("links", [])]
        pages.append(PageRecord(**{**pd, "text_blocks": text_blocks, "images": images, "drawings": drawings, "tables": tables, "links": links}))

    sections = []
    for sd in data.get("sections", []):
        fragments = [SectionFragment(**f) for f in sd.get("fragments", [])]
        sections.append(SectionRecord(**{**sd, "fragments": fragments}))

    return DocumentLayout(
        schema_version=data["schema_version"],
        source_file=data["source_file"],
        source_sha256=data["source_sha256"],
        page_count=data["page_count"],
        metadata=data.get("metadata", {}),
        toc=data.get("toc", []),
        pages=pages,
        sections=sections,
    )


def find_text_matches(layout: DocumentLayout, query: str, case_sensitive: bool = False) -> list[dict[str, Any]]:
    """
    Find literal text within individual source lines and return exact source bboxes.
    Matching is character-based, so it can cross font/style span boundaries.
    """
    if not query:
        return []
    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    for page in layout.pages:
        for block in page.text_blocks:
            for line in block.lines:
                chars: list[CharRecord] = [ch for span in line.spans for ch in span.chars]
                hay = "".join(ch.c for ch in chars)
                search_hay = hay if case_sensitive else hay.casefold()
                start = 0
                while True:
                    idx = search_hay.find(needle, start)
                    if idx < 0:
                        break
                    selected = chars[idx : idx + len(query)]
                    if selected:
                        x0 = min(ch.bbox[0] for ch in selected)
                        y0 = min(ch.bbox[1] for ch in selected)
                        x1 = max(ch.bbox[2] for ch in selected)
                        y1 = max(ch.bbox[3] for ch in selected)
                        matches.append({
                            "page_number": page.page_number,
                            "block_index": block.block_index,
                            "line_index": line.line_index,
                            "text": hay[idx : idx + len(query)],
                            "bbox": [_round(x0), _round(y0), _round(x1), _round(y1)],
                            "start_char": idx,
                            "end_char": idx + len(query),
                        })
                    start = idx + max(1, len(query))
    return matches


def extract_content_pdf(
    source_pdf: str | Path,
    page_number: int,
    bbox: Iterable[float],
    output_pdf: str | Path,
    padding: float = 2.0,
) -> Path:
    """Return an exact vector clip for one matched content bounding box."""
    source_pdf = Path(source_pdf)
    output_pdf = Path(output_pdf)
    src = fitz.open(source_pdf)
    if page_number < 1 or page_number > src.page_count:
        src.close()
        raise IndexError("page_number out of range")
    page = src[page_number - 1]
    r = fitz.Rect(list(bbox))
    r = fitz.Rect(r.x0 - padding, r.y0 - padding, r.x1 + padding, r.y1 + padding)
    r &= page.rect
    if r.is_empty:
        src.close()
        raise ValueError("empty content bbox")
    out = fitz.open()
    dst = out.new_page(width=r.width, height=r.height)
    dst.show_pdf_page(dst.rect, src, page_number - 1, clip=r, keep_proportion=False)
    out.save(output_pdf, garbage=4, deflate=True)
    out.close()
    src.close()
    return output_pdf