"""Isolated, transactional storage for opt-in processing profiles.

The original ingestion tables and their consumers are never altered here.
"""
from __future__ import annotations

import json
import re

import psycopg2.extras

from config import DB_SCHEMA, FOLDER, EMBEDDING_DIM
from db import get_conn

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)


def _safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Unsafe SQL identifier: " + repr(value))
    return '"' + value + '"'


def _table(name: str) -> str:
    return _safe_identifier(DB_SCHEMA) + "." + _safe_identifier(name + "_" + FOLDER)


CHUNKS_TABLE = _table("profile_chunks")
RESULTS_TABLE = _table("profile_results")


def init_profile_store() -> None:
    """One-time additive DDL; only called when the profile mode is selected."""
    if EMBEDDING_DIM != 1536:
        raise ValueError("Profile storage currently supports the existing 1536-dimension model")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                    doc_id TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (doc_id, profile_hash)
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {CHUNKS_TABLE} (
                    doc_id TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    chunk_vector vector(1536),
                    page_start INTEGER,
                    page_end INTEGER,
                    section_title TEXT,
                    headers JSONB NOT NULL DEFAULT '[]'::jsonb,
                    document_metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    chunk_metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (doc_id, profile_hash, chunk_id)
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {_safe_identifier('profile_chunks_profile_doc_idx_' + FOLDER)}
                ON {CHUNKS_TABLE} (profile_hash, doc_id)
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def profile_is_current(doc_id: str, source_hash: str, profile_hash: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {RESULTS_TABLE} WHERE doc_id=%s AND source_hash=%s AND profile_hash=%s",
                (doc_id, source_hash, profile_hash),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def save_profile_result(doc_id: str, source_hash: str, profile_hash: str, chunks: list) -> None:
    """Atomic replacement: a failed write retains the previous complete output."""
    if not chunks:
        raise ValueError("Cannot save zero chunks")
    for chunk in chunks:
        if chunk.doc_id != doc_id:
            raise ValueError("Mixed document IDs in profile save")
    rows = []
    for ch in chunks:
        vector = ch.chunk_vector or []
        if vector and (len(vector) != EMBEDDING_DIM or not any(vector)):
            raise ValueError("Invalid embedding vector")
        rows.append((
            doc_id, profile_hash, ch.chunk_id, ch.chunk_index, ch.file_path,
            ch.file_name, ch.chunk_text,
            "[" + ",".join(str(float(v)) for v in vector) + "]" if vector else None,
            ch.page_start, ch.page_end, ch.section_title,
            json.dumps(getattr(ch, "headers", [])),
            json.dumps(getattr(ch, "document_metadata", {})),
            json.dumps(getattr(ch, "chunk_metadata", {})),
        ))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {CHUNKS_TABLE} WHERE doc_id=%s AND profile_hash=%s",
                (doc_id, profile_hash),
            )
            psycopg2.extras.execute_values(cur, f"""
                INSERT INTO {CHUNKS_TABLE} (
                    doc_id, profile_hash, chunk_id, chunk_index, file_path,
                    file_name, chunk_text, chunk_vector, page_start, page_end,
                    section_title, headers, document_metadata, chunk_metadata
                ) VALUES %s
            """, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)", page_size=100)
            cur.execute(f"""
                INSERT INTO {RESULTS_TABLE} (doc_id, profile_hash, source_hash, chunk_count)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (doc_id, profile_hash) DO UPDATE SET
                    source_hash=EXCLUDED.source_hash,
                    chunk_count=EXCLUDED.chunk_count,
                    updated_at=NOW()
            """, (doc_id, profile_hash, source_hash, len(chunks)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
