"""Load already extracted document/page text from a configured PostgreSQL table.

Reads existing data only. The caller performs the standard chunking, metadata,
embedding, and profiled transactional output stages; no PDF/OCR work is needed.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from db import get_conn
from pdf_processor import BBox, DocumentStructure, PageContent, PageElement, ElementType, TocEntry
from profile_store import _safe_identifier


def validate_source(profile: dict) -> dict:
    settings = profile.get("extracted_content", {})
    for key in ("schema", "table", "doc_id_column", "text_column"):
        if not settings.get(key):
            raise ValueError("extracted_content." + key + " is required")
    for key, value in settings.items():
        if key not in {
            "schema", "table", "doc_id_column", "text_column",
            "page_column", "file_path_column", "file_name_column",
            "position_column", "section_column", "page_end_column",
            "element_json_column", "width_column",
            "height_column", "rotation_column",
            "source_columns",
        }:
            raise ValueError("Unknown extracted_content mapping: " + key)
        if key == "source_columns":
            if not isinstance(value, dict) or len(value) > 50:
                raise ValueError("source_columns must map at most 50 logical metadata fields")
            for logical_name, column in value.items():
                _safe_identifier(logical_name)
                _safe_identifier(column)
        elif value is not None:
            _safe_identifier(value)
    return settings


def _qualified(settings):
    return _safe_identifier(settings["schema"]) + "." + _safe_identifier(settings["table"])


def iter_document_ids(profile: dict, max_docs: int | None = None, batch_size: int = 500):
    """Keyset pagination over document identifiers; memory stays bounded."""
    settings = validate_source(profile)
    table = _qualified(settings)
    doc_col = _safe_identifier(settings["doc_id_column"])
    last = None
    count = 0
    conn = get_conn()
    try:
        while max_docs is None or count < max_docs:
            batch_limit = min(batch_size, max_docs - count) if max_docs is not None else batch_size
            cursor_where = f"AND {doc_col} > %s" if last is not None else ""
            params = (last, batch_limit) if last is not None else (batch_limit,)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {doc_col} FROM {table} WHERE {doc_col} IS NOT NULL "
                    f"{cursor_where} GROUP BY {doc_col} ORDER BY {doc_col} LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            if not rows:
                return
            for (doc_id,) in rows:
                yield doc_id
                count += 1
            last = rows[-1][0]
    finally:
        conn.close()


def load_extracted_document(profile: dict, document_key):
    """Return reconstructed DocumentStructure and content fingerprint."""
    cfg = validate_source(profile)
    table = _qualified(cfg)
    doc_col = _safe_identifier(cfg["doc_id_column"])
    cols = [cfg["text_column"]]
    extras = ["page_column", "page_end_column", "file_path_column",
              "file_name_column", "position_column", "section_column",
              "element_json_column", "width_column", "height_column",
              "rotation_column"]
    cols.extend(cfg[k] for k in extras if cfg.get(k))
    cols.extend(cfg.get("source_columns", {}).values())
    cols = list(dict.fromkeys(cols))
    quoted = ", ".join(_safe_identifier(name) for name in cols)
    order_field = cfg.get("position_column") or cfg.get("page_column")
    order = f" ORDER BY {_safe_identifier(order_field)}" if order_field else ""
    conn = get_conn()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {quoted} FROM {table} WHERE {doc_col}=%s{order}",
                (document_key,),
            )
            while True:
                batch = cur.fetchmany(256)
                if not batch:
                    break
                rows.extend(batch)
    finally:
        conn.close()
    if not rows:
        raise ValueError("No extracted content found for document")
    if len(rows) > 1 and not order_field:
        raise ValueError("Multiple extracted rows require page_column or position_column")
    parsed = [dict(zip(cols, item)) for item in rows]
    # PostgreSQL does not guarantee ordering of rows tied on ORDER BY.
    # A duplicate/null position would change chunk boundaries and identity
    # across reruns. Require a unique stable ordering key per document.
    if len(parsed) > 1:
        positions = [row.get(order_field) for row in parsed]
        if any(value is None for value in positions) or len(set(positions)) != len(positions):
            raise ValueError(
                "Ambiguous extracted row order; configure a unique non-null "
                "position_column (e.g. chunk_index) for each document"
            )
    file_path = next(
        (str(row[cfg["file_path_column"]]) for row in parsed
         if cfg.get("file_path_column") and row[cfg["file_path_column"]]),
        f"db://{cfg['schema']}/{cfg['table']}/{document_key}",
    )
    file_name = next(
        (str(row[cfg["file_name_column"]]) for row in parsed
         if cfg.get("file_name_column") and row[cfg["file_name_column"]]),
        Path(file_path).name,
    )
    pages = []
    source_metadata = {}
    heading = None
    toc = []
    digest = hashlib.sha256()
    for index, row in enumerate(parsed):
        text = row[cfg["text_column"]]
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("Extracted content must be text")
        pg = row.get(cfg.get("page_column"), index + 1)
        pg = int(pg) if pg is not None else index + 1
        if pg < 1:
            raise ValueError("Invalid extracted page number")
        last = row.get(cfg.get("page_end_column"), pg)
        last = int(last) if last is not None else pg
        if last < pg:
            raise ValueError("Page end precedes page start")
        title = str(row.get(cfg.get("section_column")) or "").strip()
        if title and title != heading:
            heading = title
            toc.append(TocEntry(level=1, title=title, page_number=pg))
        for logical_name, source_col in cfg.get("source_columns", {}).items():
            value = row.get(source_col)
            if value is None or value == "":
                continue
            existing = source_metadata.get(logical_name)
            if existing is not None and existing != value:
                raise ValueError("Conflicting document-level source column: " + logical_name)
            source_metadata[logical_name] = value
        digest.update(str(pg).encode("utf-8") + b"\x00")
        digest.update(str(last).encode("utf-8") + b"\x00")
        digest.update(title.encode("utf-8") + b"\x00")
        digest.update(text.encode("utf-8") + b"\x00")
        elements = None
        if cfg.get("element_json_column"):
            payload = row.get(cfg["element_json_column"])
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, list) or len(payload) > 50000:
                raise ValueError("Invalid page geometry JSON array")
            elements = []
            for item in payload:
                if not isinstance(item, dict) or item.get("type") not in {
                    kind.value for kind in ElementType
                }:
                    raise ValueError("Unsupported staged page element type")
                coords = item.get("bbox")
                if (
                    not isinstance(coords, list) or len(coords) != 4
                    or any(type(v) not in (float,int) or not math.isfinite(v)
                           for v in coords)
                    or coords[0] > coords[2] or coords[1] > coords[3]
                ):
                    raise ValueError("Invalid staged element bounding box")
                el = PageElement(
                    ElementType(item["type"]), BBox(*coords),
                    text=str(item.get("text") or ""),
                    confidence=float(item.get("confidence",1.0)),
                    reading_order=int(item.get("reading_order",len(elements))),
                )
                if "ocr_regions" in item:
                    regions = item["ocr_regions"]
                    if not isinstance(regions,list):
                        raise ValueError("Invalid OCR geometry payload")
                    el.ocr_regions = regions
                elements.append(el)
            digest.update(json.dumps(
                payload,sort_keys=True,ensure_ascii=False,
                separators=(",",":"),allow_nan=False
            ).encode("utf-8"))
        if elements is None:
            elements = (
                [PageElement(ElementType.TEXT, BBox(0,0,0,0),text=text)]
                if text.strip() else []
            )
        width = row.get(cfg.get("width_column"), 0) or 0
        height = row.get(cfg.get("height_column"), 0) or 0
        rotation = row.get(cfg.get("rotation_column"), 0) or 0
        if (
            type(width) not in (int,float) or not math.isfinite(width) or width < 0
            or type(height) not in (int,float) or not math.isfinite(height) or height < 0
            or type(rotation) is not int or rotation not in (0,90,180,270)
        ):
            raise ValueError("Invalid PDF page size or rotation metadata")
        page_entry = PageContent(
            page_number=pg, width=float(width), height=float(height),
            raw_text=text, is_blank=not text.strip(), elements=elements,
        )
        page_entry.rotation = rotation
        digest.update(f"{width}:{height}:{rotation}".encode("utf-8"))
        page_entry.source_section_title = title
        page_entry.source_page_end = last
        pages.append(page_entry)
    doc = DocumentStructure(
        file_path=file_path, file_name=file_name, doc_id=str(document_key),
        page_count=max(
            int(row.get(cfg.get("page_end_column")) or page.page_number)
            for row, page in zip(parsed, pages)
        ), pages=pages, toc=toc, has_toc=bool(toc),
    )
    doc.source_metadata = source_metadata
    digest.update(
        json.dumps(source_metadata, sort_keys=True, default=str).encode("utf-8")
    )
    return doc, digest.hexdigest()
