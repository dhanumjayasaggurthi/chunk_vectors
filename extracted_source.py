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


def _source_rows(profile: dict, document_key, batch_size: int):
    """Yield mapped source rows through a PostgreSQL server-side cursor.

    ``fetchmany`` on a normal psycopg2 cursor still leaves the complete result
    buffered by libpq. A named cursor keeps both the database transfer and the
    Python row working set bounded for multi-thousand-page documents.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 4096:
        raise ValueError("source batch_size must be 1..4096")
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

    def rows():
        conn = get_conn()
        try:
            # Cursor names are scoped to a connection. Every scan owns its
            # connection, so this stable name is safe across worker threads.
            with conn.cursor(name="epod_materialize_source") as cur:
                cur.itersize = batch_size
                cur.execute(
                    f"SELECT {quoted} FROM {table} WHERE {doc_col}=%s{order}",
                    (document_key,),
                )
                while True:
                    batch = cur.fetchmany(batch_size)
                    if not batch:
                        break
                    for item in batch:
                        yield dict(zip(cols, item))
        finally:
            # Close the server-side cursor transaction before a pooled lease
            # is reset and returned to another worker.
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
            conn.close()

    return cfg, order_field, rows()


def _validate_position(row, order_field, index, previous):
    if index and not order_field:
        raise ValueError("Multiple extracted rows require page_column or position_column")
    position = row.get(order_field) if order_field else None
    if index and (previous is None or position is None or position == previous):
        raise ValueError(
            "Ambiguous extracted row order; configure a unique non-null "
            "position_column (e.g. chunk_index) for each document"
        )
    return position


def _validated_row(row, cfg, index, digest, source_metadata):
    """Validate and hash one staged row without retaining the source mapping."""
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
    payload = None
    if cfg.get("element_json_column"):
        payload = row.get(cfg["element_json_column"])
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, list) or len(payload) > 50000:
            raise ValueError("Invalid page geometry JSON array")
        digest.update(json.dumps(
            payload, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
    width = row.get(cfg.get("width_column"), 0) or 0
    height = row.get(cfg.get("height_column"), 0) or 0
    rotation = row.get(cfg.get("rotation_column"), 0) or 0
    if (
        type(width) not in (int, float) or not math.isfinite(width) or width < 0
        or type(height) not in (int, float) or not math.isfinite(height) or height < 0
        or type(rotation) is not int or rotation not in (0, 90, 180, 270)
    ):
        raise ValueError("Invalid PDF page size or rotation metadata")
    digest.update(f"{width}:{height}:{rotation}".encode("utf-8"))
    return text, pg, last, title, payload, width, height, rotation


def fingerprint_extracted_document(profile: dict, document_key, *, batch_size: int = 128):
    """Return the exact materialization source digest with bounded memory.

    This replay probe intentionally avoids rebuilding page/element objects. A
    digest mismatch is followed by the full validating load before any write.
    """
    cfg, order_field, rows = _source_rows(profile, document_key, batch_size)
    digest = hashlib.sha256()
    source_metadata = {}
    previous = None
    count = 0
    for index, row in enumerate(rows):
        previous = _validate_position(row, order_field, index, previous)
        _validated_row(row, cfg, index, digest, source_metadata)
        count += 1
    if not count:
        raise ValueError("No extracted content found for document")
    digest.update(json.dumps(
        source_metadata, sort_keys=True, default=str,
    ).encode("utf-8"))
    return digest.hexdigest()


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


def load_extracted_document(profile: dict, document_key, *, batch_size: int = 128):
    """Return reconstructed DocumentStructure and content fingerprint."""
    cfg, order_field, parsed = _source_rows(profile, document_key, batch_size)
    file_path = None
    file_name = None
    pages = []
    source_metadata = {}
    heading = None
    toc = []
    digest = hashlib.sha256()
    previous = None
    row_count = 0
    page_count = 0
    for index, row in enumerate(parsed):
        previous = _validate_position(row, order_field, index, previous)
        text, pg, last, title, payload, width, height, rotation = _validated_row(
            row, cfg, index, digest, source_metadata,
        )
        row_count += 1
        page_count = max(page_count, last)
        if file_path is None and cfg.get("file_path_column") and row[cfg["file_path_column"]]:
            file_path = str(row[cfg["file_path_column"]])
        if file_name is None and cfg.get("file_name_column") and row[cfg["file_name_column"]]:
            file_name = str(row[cfg["file_name_column"]])
        if title and title != heading:
            heading = title
            toc.append(TocEntry(level=1, title=title, page_number=pg))
        elements = None
        if payload is not None:
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
                if "visual_asset" in item:
                    from pdf_processor import validate_visual_asset
                    if item["type"] not in ("image","chart") or item.get("text"):
                        raise ValueError("Invalid deferred visual element")
                    el.visual_asset = validate_visual_asset(
                        item["visual_asset"], page_number=pg,
                    )
                if "unresolved_region" in item:
                    from pdf_processor import validate_unresolved_region
                    if (item["type"]!=ElementType.VECTOR_REGION.value
                            or item.get("text") or "vector_preview" in item):
                        raise ValueError("Invalid unresolved table/graphic element")
                    el.unresolved_region=validate_unresolved_region(
                        item["unresolved_region"],page_number=pg,
                    )
                if "source_table" in item:
                    if item["type"] != ElementType.TABLE.value:
                        raise ValueError("Source table attached to non-table element")
                    from table_extractor import validate_source_table
                    el.source_table=validate_source_table(item["source_table"])
                if "physical_table_body" in item:
                    from open_grid_recovery import validate_physical_body
                    if "source_table" not in item or item["type"]!=ElementType.TABLE.value:
                        raise ValueError("Physical body requires original source header grid")
                    el.physical_table_body=validate_physical_body(
                        item["physical_table_body"],header_bbox=coords,max_json_bytes=100_000_000)
                if "approved_asset" in item:
                    import re as _re
                    approval=item["approved_asset"]
                    if (not isinstance(approval,dict)
                            or set(approval)!={"source_sha256","source_xref",
                                "source_bytes","kind","text","reviewed_by",
                                "approval_reference","approval_status"}
                            or approval["approval_status"]!="approved"
                            or not isinstance(approval["source_sha256"],str)
                            or not _re.fullmatch(r"[0-9a-f]{64}",
                                                 approval["source_sha256"])
                            or type(approval["source_xref"]) is not int
                            or approval["source_xref"]<=0
                            or type(approval["source_bytes"]) is not int
                            or approval["source_bytes"]<=0
                            or approval["kind"] not in
                                ("logo","seal","repeated_header")
                            or not isinstance(approval["text"],str)
                            or not approval["text"].strip()
                            or len(approval["text"])>512
                            or item["type"]!="image"
                            or item.get("text")!=approval["text"]
                            or not isinstance(approval["reviewed_by"],str)
                            or not approval["reviewed_by"].strip()
                            or not isinstance(approval["approval_reference"],str)
                            or not approval["approval_reference"].strip()):
                        raise ValueError("Invalid approved-image provenance")
                    el.approved_asset=approval
                if "vector_preview" in item:
                    import base64
                    preview=item["vector_preview"]
                    encoded=preview.get("base64") if isinstance(preview,dict) else None
                    if (item["type"] != ElementType.VECTOR_REGION.value
                            or not isinstance(encoded,str) or len(encoded)>172_000
                            or preview.get("format")!="png"
                            or preview.get("dpi")!=144):
                        raise ValueError("Invalid vector preview metadata")
                    try:
                        png=base64.b64decode(encoded,validate=True)
                    except Exception as exc:
                        raise ValueError("Invalid vector preview base64") from exc
                    if (not 0<len(png)<=128_000
                            or not png.startswith(b"\x89PNG\r\n\x1a\n")
                            or hashlib.sha256(png).hexdigest()!=preview.get("sha256")):
                        raise ValueError("Corrupt vector preview checksum")
                    el.vector_png=png
                if "runs" in item:
                    from rich_document import validate_runs
                    el.runs = validate_runs(item["runs"])
                if "language" in item:
                    lang = item["language"]
                    if not isinstance(lang, str) or len(lang) > 32:
                        raise ValueError("Invalid extracted language")
                    el.language = lang
                if "semantic_role" in item:
                    if item["semantic_role"] not in ("heading", ""):
                        raise ValueError("Unsupported semantic role")
                    level = item.get("heading_level", 0)
                    if type(level) is not int or not 0 <= level <= 6:
                        raise ValueError("Invalid heading level")
                    el.semantic_role = item["semantic_role"]
                    el.heading_level = level
                if "ocr_regions" in item:
                    regions = item["ocr_regions"]
                    if not isinstance(regions,list):
                        raise ValueError("Invalid OCR geometry payload")
                    el.ocr_regions = regions
                elements.append(el)
        if elements is None:
            elements = (
                [PageElement(ElementType.TEXT, BBox(0,0,0,0),text=text)]
                if text.strip() else []
            )
        page_entry = PageContent(
            page_number=pg, width=float(width), height=float(height),
            raw_text=text, is_blank=not text.strip(), elements=elements,
        )
        page_entry.rotation = rotation
        page_entry.source_section_title = title
        page_entry.source_page_end = last
        pages.append(page_entry)
    if not row_count:
        raise ValueError("No extracted content found for document")
    if file_path is None:
        file_path = f"db://{cfg['schema']}/{cfg['table']}/{document_key}"
    if file_name is None:
        file_name = Path(file_path).name
    doc = DocumentStructure(
        file_path=file_path, file_name=file_name, doc_id=str(document_key),
        page_count=page_count, pages=pages, toc=toc, has_toc=bool(toc),
    )
    doc.source_metadata = source_metadata
    digest.update(
        json.dumps(source_metadata, sort_keys=True, default=str).encode("utf-8")
    )
    return doc, digest.hexdigest()
