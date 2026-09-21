"""PostgreSQL persistence for lossless PDF layout manifests and source bytes.

The existing semantic chunk tables remain unchanged. This module adds a
separate fidelity layer:

* document metadata / source hash
* page-level JSONB layout records
* section fragment coordinates
* original PDF bytes for DB-only exact retrieval

Storing the original source bytes is intentional: a layout manifest alone can
reconstruct text and geometry, but byte-for-byte source verification and exact
vector/page retrieval require the original PDF content streams and embedded
font/image resources.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import psycopg2.extras

from config import DB_SCHEMA, FOLDER
from db import get_conn
from layout_preserver import (
    CharRecord,
    DocumentLayout,
    DrawingRecord,
    ImageRecord,
    LineRecord,
    LinkRecord,
    PageRecord,
    SectionFragment,
    SectionRecord,
    SpanRecord,
    TableCellRecord,
    TableRecord,
    TextBlockRecord,
)

_init_lock = threading.Lock()
_initialized = False


DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.doc_layout_manifest_{FOLDER} (
        doc_id          TEXT PRIMARY KEY,
        file_name       TEXT NOT NULL,
        file_path       TEXT NOT NULL,
        schema_version  TEXT NOT NULL,
        source_sha256   TEXT NOT NULL,
        page_count      INTEGER NOT NULL,
        metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        toc             JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.doc_layout_pages_{FOLDER} (
        doc_id          TEXT NOT NULL,
        page_number     INTEGER NOT NULL,
        width           DOUBLE PRECISION NOT NULL,
        height          DOUBLE PRECISION NOT NULL,
        rotation        INTEGER NOT NULL,
        layout_json     JSONB NOT NULL,
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (doc_id, page_number)
    );
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.doc_layout_sections_{FOLDER} (
        section_id      TEXT PRIMARY KEY,
        doc_id          TEXT NOT NULL,
        title           TEXT NOT NULL,
        level           INTEGER NOT NULL,
        source          TEXT NOT NULL,
        start_page      INTEGER NOT NULL,
        end_page        INTEGER NOT NULL,
        anchor_bbox     DOUBLE PRECISION[],
        fragments       JSONB NOT NULL,
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.doc_layout_source_{FOLDER} (
        doc_id          TEXT PRIMARY KEY,
        file_name       TEXT NOT NULL,
        media_type      TEXT NOT NULL DEFAULT 'application/pdf',
        source_sha256   TEXT NOT NULL,
        byte_length     BIGINT NOT NULL,
        source_bytes    BYTEA NOT NULL,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    f"CREATE INDEX IF NOT EXISTS layout_pages_doc_idx_{FOLDER} ON {DB_SCHEMA}.doc_layout_pages_{FOLDER}(doc_id);",
    f"CREATE INDEX IF NOT EXISTS layout_sections_doc_idx_{FOLDER} ON {DB_SCHEMA}.doc_layout_sections_{FOLDER}(doc_id);",
    f"CREATE INDEX IF NOT EXISTS layout_sections_title_idx_{FOLDER} ON {DB_SCHEMA}.doc_layout_sections_{FOLDER}(doc_id, title);",
    f"CREATE INDEX IF NOT EXISTS layout_source_sha_idx_{FOLDER} ON {DB_SCHEMA}.doc_layout_source_{FOLDER}(source_sha256);",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_layout_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for sql in DDL:
                    cur.execute(sql)
            conn.commit()
            _initialized = True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def delete_document_layout(doc_id: str) -> None:
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_sections_{FOLDER} WHERE doc_id=%s", (doc_id,))
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_pages_{FOLDER} WHERE doc_id=%s", (doc_id,))
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_source_{FOLDER} WHERE doc_id=%s", (doc_id,))
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_manifest_{FOLDER} WHERE doc_id=%s", (doc_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_document_layout(
    doc_id: str,
    file_path: str,
    file_name: str,
    layout: DocumentLayout,
    *,
    source_file: str | Path | None = None,
    source_bytes: bytes | None = None,
    media_type: str = "application/pdf",
) -> None:
    """Atomically replace the fidelity manifest for one document.

    Pass either ``source_file`` or ``source_bytes`` to make the document fully
    retrievable from PostgreSQL even when the NAS/S3 copy is unavailable.
    The source bytes are verified against ``layout.source_sha256`` before any
    DB write is committed.
    """
    if source_file is not None and source_bytes is not None:
        raise ValueError("pass only one of source_file or source_bytes")
    if source_file is not None:
        source_bytes = Path(source_file).read_bytes()
    if source_bytes is not None:
        source_bytes = bytes(source_bytes)
        actual_sha = _sha256_bytes(source_bytes)
        if actual_sha != layout.source_sha256:
            raise ValueError(
                f"source SHA mismatch: layout={layout.source_sha256} bytes={actual_sha}"
            )

    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_sections_{FOLDER} WHERE doc_id=%s", (doc_id,))
            cur.execute(f"DELETE FROM {DB_SCHEMA}.doc_layout_pages_{FOLDER} WHERE doc_id=%s", (doc_id,))
            cur.execute(
                f"""
                INSERT INTO {DB_SCHEMA}.doc_layout_manifest_{FOLDER}
                    (doc_id,file_name,file_path,schema_version,source_sha256,page_count,metadata,toc,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,NOW(),NOW())
                ON CONFLICT (doc_id) DO UPDATE SET
                    file_name=EXCLUDED.file_name,
                    file_path=EXCLUDED.file_path,
                    schema_version=EXCLUDED.schema_version,
                    source_sha256=EXCLUDED.source_sha256,
                    page_count=EXCLUDED.page_count,
                    metadata=EXCLUDED.metadata,
                    toc=EXCLUDED.toc,
                    updated_at=NOW()
                """,
                (
                    doc_id, file_name, file_path, layout.schema_version,
                    layout.source_sha256, layout.page_count,
                    json.dumps(layout.metadata, ensure_ascii=False),
                    json.dumps(layout.toc, ensure_ascii=False),
                ),
            )
            page_rows = [
                (
                    doc_id, p.page_number, p.width, p.height, p.rotation,
                    json.dumps(asdict(p), ensure_ascii=False, separators=(",", ":")),
                )
                for p in layout.pages
            ]
            if page_rows:
                psycopg2.extras.execute_batch(
                    cur,
                    f"""
                    INSERT INTO {DB_SCHEMA}.doc_layout_pages_{FOLDER}
                        (doc_id,page_number,width,height,rotation,layout_json,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,NOW())
                    """,
                    page_rows,
                    page_size=25,
                )
            section_rows = [
                (
                    s.section_id, doc_id, s.title, s.level, s.source,
                    s.start_page, s.end_page, s.anchor_bbox,
                    json.dumps([asdict(f) for f in s.fragments], ensure_ascii=False),
                )
                for s in layout.sections
            ]
            if section_rows:
                psycopg2.extras.execute_batch(
                    cur,
                    f"""
                    INSERT INTO {DB_SCHEMA}.doc_layout_sections_{FOLDER}
                        (section_id,doc_id,title,level,source,start_page,end_page,anchor_bbox,fragments,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
                    """,
                    section_rows,
                    page_size=50,
                )
            if source_bytes is not None:
                cur.execute(
                    f"""
                    INSERT INTO {DB_SCHEMA}.doc_layout_source_{FOLDER}
                        (doc_id,file_name,media_type,source_sha256,byte_length,source_bytes,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW())
                    ON CONFLICT (doc_id) DO UPDATE SET
                        file_name=EXCLUDED.file_name,
                        media_type=EXCLUDED.media_type,
                        source_sha256=EXCLUDED.source_sha256,
                        byte_length=EXCLUDED.byte_length,
                        source_bytes=EXCLUDED.source_bytes,
                        updated_at=NOW()
                    """,
                    (
                        doc_id, file_name, media_type, layout.source_sha256,
                        len(source_bytes), psycopg2.Binary(source_bytes),
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_manifest(doc_id: str) -> Optional[dict[str, Any]]:
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {DB_SCHEMA}.doc_layout_manifest_{FOLDER} WHERE doc_id=%s",
                (doc_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def get_source_blob(doc_id: str) -> Optional[dict[str, Any]]:
    """Return DB-resident original source bytes plus integrity metadata."""
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT doc_id,file_name,media_type,source_sha256,byte_length,source_bytes
                FROM {DB_SCHEMA}.doc_layout_source_{FOLDER}
                WHERE doc_id=%s
                """,
                (doc_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            result["source_bytes"] = bytes(result["source_bytes"])
            return result
    finally:
        conn.close()


def find_section(doc_id: str, title: str) -> Optional[dict[str, Any]]:
    """Find a section by exact title first, then a unique case-insensitive substring."""
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {DB_SCHEMA}.doc_layout_sections_{FOLDER} WHERE doc_id=%s AND lower(title)=lower(%s) ORDER BY start_page LIMIT 1",
                (doc_id, title),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute(
                f"SELECT * FROM {DB_SCHEMA}.doc_layout_sections_{FOLDER} WHERE doc_id=%s AND title ILIKE %s ORDER BY start_page LIMIT 2",
                (doc_id, f"%{title}%"),
            )
            rows = cur.fetchall()
            return dict(rows[0]) if len(rows) == 1 else None
    finally:
        conn.close()


def get_page_layout(doc_id: str, page_number: int) -> Optional[dict[str, Any]]:
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT layout_json FROM {DB_SCHEMA}.doc_layout_pages_{FOLDER} WHERE doc_id=%s AND page_number=%s",
                (doc_id, page_number),
            )
            row = cur.fetchone()
            return row["layout_json"] if row else None
    finally:
        conn.close()


def _page_from_dict(pd: dict[str, Any]) -> PageRecord:
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
    return PageRecord(
        **{
            **pd,
            "text_blocks": text_blocks,
            "images": images,
            "drawings": drawings,
            "tables": tables,
            "links": links,
        }
    )


def load_document_layout(doc_id: str) -> Optional[DocumentLayout]:
    """Rehydrate the complete fidelity manifest from PostgreSQL."""
    ensure_layout_schema()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {DB_SCHEMA}.doc_layout_manifest_{FOLDER} WHERE doc_id=%s",
                (doc_id,),
            )
            manifest = cur.fetchone()
            if not manifest:
                return None
            cur.execute(
                f"SELECT layout_json FROM {DB_SCHEMA}.doc_layout_pages_{FOLDER} WHERE doc_id=%s ORDER BY page_number",
                (doc_id,),
            )
            pages = [_page_from_dict(dict(r["layout_json"])) for r in cur.fetchall()]
            cur.execute(
                f"""
                SELECT section_id,title,level,source,start_page,end_page,anchor_bbox,fragments
                FROM {DB_SCHEMA}.doc_layout_sections_{FOLDER}
                WHERE doc_id=%s
                ORDER BY start_page, level, section_id
                """,
                (doc_id,),
            )
            sections = []
            for row in cur.fetchall():
                fragments = [SectionFragment(**dict(f)) for f in row["fragments"]]
                sections.append(
                    SectionRecord(
                        section_id=row["section_id"],
                        title=row["title"],
                        level=row["level"],
                        source=row["source"],
                        start_page=row["start_page"],
                        end_page=row["end_page"],
                        anchor_bbox=list(row["anchor_bbox"]) if row["anchor_bbox"] is not None else None,
                        fragments=fragments,
                    )
                )
            return DocumentLayout(
                schema_version=manifest["schema_version"],
                source_file=manifest["file_name"],
                source_sha256=manifest["source_sha256"],
                page_count=manifest["page_count"],
                metadata=dict(manifest["metadata"] or {}),
                toc=list(manifest["toc"] or []),
                pages=pages,
                sections=sections,
            )
    finally:
        conn.close()
