"""Load already extracted document/page text from a configured PostgreSQL table.

Reads existing data only. The caller performs the standard chunking, metadata,
embedding, and profiled transactional output stages; no PDF/OCR work is needed.
"""
from __future__ import annotations

import hashlib
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
              "file_name_column", "position_column", "section_column"]
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
        pages.append(PageContent(
            page_number=pg, width=0, height=0, raw_text=text,
            is_blank=not text.strip(),
            elements=[PageElement(ElementType.TEXT, BBox(0, 0, 0, 0), text=text)]
            if text.strip() else [],
        ))
    doc = DocumentStructure(
        file_path=file_path, file_name=file_name, doc_id=str(document_key),
        page_count=max(
            int(row.get(cfg.get("page_end_column")) or page.page_number)
            for row, page in zip(parsed, pages)
        ), pages=pages, toc=toc, has_toc=bool(toc),
    )
    doc.source_metadata = source_metadata
    digest.update(
        __import__("json").dumps(source_metadata, sort_keys=True, default=str).encode("utf-8")
    )
    return doc, digest.hexdigest()
