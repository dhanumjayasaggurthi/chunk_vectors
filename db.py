"""
db.py
=====
All database interactions for the EPOD ingestion pipeline.

  - Creates the epod schema + both tables on first run
  - Ingestion log  : tracks file hash, status, retry count
  - Doc chunks     : stores chunk text + pgvector embeddings
  - Helper functions used by crawler, chunker, embedder
"""

import json
import hashlib
import logging
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras

from config import POSTGRES, DB_SCHEMA, FOLDER, EMBEDDING_DIM

logger = logging.getLogger("epod.db")


# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────

def get_conn():
    """Return a new psycopg2 connection. Caller must close it."""
    return psycopg2.connect(
        host=POSTGRES["host"],
        port=POSTGRES["port"],
        dbname=POSTGRES["database"],
        user=POSTGRES["user"],
        password=POSTGRES["password"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema + table creation  (idempotent — safe to run repeatedly)
# ─────────────────────────────────────────────────────────────────────────────

CREATE_SCHEMA_SQL = f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA};"

CREATE_INGESTION_LOG_SQL = f"""
CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.ingestion_log_{FOLDER} (
    doc_id          TEXT        PRIMARY KEY,          -- sha256 of file_path
    file_path       TEXT        NOT NULL UNIQUE,
    file_name       TEXT        NOT NULL,
    file_hash       TEXT        NOT NULL,             -- sha256 of file bytes
    file_size       BIGINT,
    last_modified   TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT        NOT NULL DEFAULT 'PENDING',
                                                      -- PENDING | RUNNING | SUCCESS | ERROR | SKIPPED
    error_message   TEXT,
    retry_count     INTEGER     DEFAULT 0,
    chunk_count     INTEGER     DEFAULT 0,            -- how many chunks were created
    pages_total     INTEGER     DEFAULT 0
);
"""

CREATE_DOC_CHUNKS_SQL = f"""
CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.doc_chunks_{FOLDER} (
    chunk_id        TEXT        PRIMARY KEY,          -- doc_id + chunk_index
    doc_id          TEXT        NOT NULL
                    REFERENCES {DB_SCHEMA}.ingestion_log_{FOLDER}(doc_id) ON DELETE CASCADE,
    file_name       TEXT        NOT NULL,
    file_path       TEXT        NOT NULL,

    -- Position
    chunk_index     INTEGER     NOT NULL,
    chunk_total     INTEGER,
    chunk_level     TEXT,                             -- 'section' | 'page' | 'split'

    -- Content context
    section_title   TEXT,
    page_start      INTEGER,
    page_end        INTEGER,
    content_types   TEXT[],                           -- ['text','table','ocr','chart']

    -- The actual text sent to embeddings
    chunk_text      TEXT        NOT NULL,

    -- Vector  (pgvector)
    chunk_vector    vector(1536),

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_VECTOR_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS epod_chunks_vector_idx_{FOLDER}
ON {DB_SCHEMA}.doc_chunks_{FOLDER}
USING hnsw (chunk_vector vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
"""

CREATE_INDEXES_SQL = [
    f"CREATE INDEX IF NOT EXISTS epod_chunks_doc_idx_{FOLDER}      ON {DB_SCHEMA}.doc_chunks_{FOLDER} (doc_id);",
    f"CREATE INDEX IF NOT EXISTS epod_chunks_filename_idx_{FOLDER}  ON {DB_SCHEMA}.doc_chunks_{FOLDER} (file_name);",
    f"CREATE INDEX IF NOT EXISTS epod_chunks_level_idx_{FOLDER}     ON {DB_SCHEMA}.doc_chunks_{FOLDER} (chunk_level);",
    f"CREATE INDEX IF NOT EXISTS epod_log_status_idx_{FOLDER}       ON {DB_SCHEMA}.ingestion_log_{FOLDER} (status);",
    f"CREATE INDEX IF NOT EXISTS epod_log_hash_idx_{FOLDER}         ON {DB_SCHEMA}.ingestion_log_{FOLDER} (file_hash);",
]


def init_db():
    """
    Create the epod schema, both tables, and all indexes.
    Safe to call on every startup — all statements use IF NOT EXISTS.
    Also ensures the pgvector extension is enabled.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # pgvector extension (must exist for vector type)
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            logger.info("pgvector extension confirmed.")

            cur.execute(CREATE_SCHEMA_SQL)
            logger.info(f"Schema '{DB_SCHEMA}' confirmed.")

            cur.execute(CREATE_INGESTION_LOG_SQL)
            logger.info("Table epod.ingestion_log_{FOLDER} confirmed.")

            cur.execute(CREATE_DOC_CHUNKS_SQL)
            logger.info("Table epod.doc_chunks_{FOLDER} confirmed.")

            cur.execute(CREATE_VECTOR_INDEX_SQL)
            logger.info("HNSW vector index confirmed.")

            for idx_sql in CREATE_INDEXES_SQL:
                cur.execute(idx_sql)
            logger.info("Supporting indexes confirmed.")

        conn.commit()
        logger.info("✅ DB initialisation complete.")
    except Exception as e:
        conn.rollback()
        logger.error(f"DB init failed: {e}")
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Crash recovery
# ─────────────────────────────────────────────────────────────────────────────

def reset_stale_running(max_age_minutes: int = 60) -> int:
    """
    Reset RUNNING records that are older than max_age_minutes back to PENDING.

    Call this at pipeline startup.  Docs left in RUNNING state after a crash
    would otherwise be silently skipped on the next run.  Any doc that has
    been RUNNING for more than max_age_minutes is almost certainly from a
    dead process and safe to re-queue.

    Returns the number of records reset.
    """
    sql = f"""
        UPDATE {DB_SCHEMA}.ingestion_log_{FOLDER}
        SET    status = 'PENDING',
               updated_at = NOW()
        WHERE  status = 'RUNNING'
          AND  updated_at < NOW() - INTERVAL '{max_age_minutes} minutes'
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            count = cur.rowcount
        conn.commit()
        if count:
            logger.warning(
                f"reset_stale_running: {count} RUNNING records older than "
                f"{max_age_minutes} min reset to PENDING"
            )
        return count
    except Exception as e:
        conn.rollback()
        logger.error(f"reset_stale_running failed: {e}")
        return 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion log helpers
# ─────────────────────────────────────────────────────────────────────────────

def hash_file_path(file_path: str) -> str:
    """Stable doc_id: sha256 of the normalised file path string."""
    return hashlib.sha256(file_path.encode("utf-8")).hexdigest()


def hash_file_bytes(file_path: str) -> str:
    """sha256 of the actual file bytes — used to detect content changes."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def get_ingestion_record(file_path: str) -> Optional[dict]:
    """
    Return the ingestion_log_{FOLDER} row for this file path, or None if never seen.
    """
    doc_id = hash_file_path(file_path)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {DB_SCHEMA}.ingestion_log_{FOLDER} WHERE doc_id = %s;",
                (doc_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def should_ingest(file_path: str, current_hash: str) -> tuple[bool, str]:
    """
    Decide whether a file needs ingesting.

    Returns:
        (True,  reason)  → proceed with ingestion
        (False, reason)  → skip
    """
    record = get_ingestion_record(file_path)

    if record is None:
        return True, "new_file"

    if record["status"] == "ERROR" and record["retry_count"] >= 2:
        return False, f"max_retries_exceeded ({record['retry_count']})"

    if record["status"] in ("RUNNING",):
        return False, "already_running"

    if record["status"] == "SUCCESS" and record["file_hash"] == current_hash:
        return False, "unchanged"

    if record["file_hash"] != current_hash:
        return True, "file_modified"

    if record["status"] in ("PENDING", "ERROR"):
        return True, f"retry (status={record['status']})"

    return False, f"skip (status={record['status']})"


def upsert_ingestion_log(
    file_path:     str,
    file_name:     str,
    file_hash:     str,
    file_size:     int,
    last_modified: datetime,
    status:        str,
    error_message: str   = None,
    chunk_count:   int   = 0,
    pages_total:   int   = 0,
    increment_retry: bool = False,
):
    """
    Insert or update an ingestion_log_{FOLDER} row.
    Called at start (RUNNING), end (SUCCESS), and on failure (ERROR).
    """
    doc_id = hash_file_path(file_path)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if increment_retry:
                retry_sql = f"retry_count = ingestion_log_{FOLDER}.retry_count + 1,"
            else:
                retry_sql = ""

            cur.execute(f"""
                INSERT INTO {DB_SCHEMA}.ingestion_log_{FOLDER}
                    (doc_id, file_path, file_name, file_hash, file_size,
                     last_modified, status, error_message, chunk_count, pages_total,
                     ingested_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s,
                     NOW(), NOW())
                ON CONFLICT (doc_id) DO UPDATE SET
                    file_hash     = EXCLUDED.file_hash,
                    file_size     = EXCLUDED.file_size,
                    last_modified = EXCLUDED.last_modified,
                    status        = EXCLUDED.status,
                    error_message = EXCLUDED.error_message,
                    chunk_count   = EXCLUDED.chunk_count,
                    pages_total   = EXCLUDED.pages_total,
                    {retry_sql}
                    updated_at    = NOW();
            """, (
                doc_id, file_path, file_name, file_hash, file_size,
                last_modified, status, error_message, chunk_count, pages_total,
            ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"upsert_ingestion_log failed for {file_path}: {e}")
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Chunk upsert
# ─────────────────────────────────────────────────────────────────────────────

def delete_chunks_for_doc(doc_id: str):
    """Remove all existing chunks before re-ingesting a modified document."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {DB_SCHEMA}.doc_chunks_{FOLDER} WHERE doc_id = %s;",
                (doc_id,)
            )
            deleted = cur.rowcount
        conn.commit()
        logger.info(f"Deleted {deleted} existing chunks for doc {doc_id[:12]}…")
    except Exception as e:
        conn.rollback()
        logger.error(f"delete_chunks_for_doc failed: {e}")
        raise
    finally:
        conn.close()


def insert_chunks_batch(chunks: list[dict]):
    """
    Bulk-insert a list of chunk dicts into epod.doc_chunks_{FOLDER}.
    Each dict must contain all required fields.

    Expected keys per chunk:
        chunk_id, doc_id, file_name, file_path,
        chunk_index, chunk_total, chunk_level,
        section_title, page_start, page_end, content_types,
        chunk_text, chunk_vector   (list of floats)
    """
    if not chunks:
        return

    # Format vector as pgvector literal string, or None (→ SQL NULL) when empty.
    def _vec(v):
        if not v:
            return None
        return "[" + ",".join(f"{float(x):.8f}" for x in v) + "]"

    rows = []
    for c in chunks:
        rows.append((
            c["chunk_id"],
            c["doc_id"],
            c["file_name"],
            c["file_path"],
            c["chunk_index"],
            c.get("chunk_total"),
            c.get("chunk_level"),
            c.get("section_title"),
            c.get("page_start"),
            c.get("page_end"),
            c.get("content_types", []),
            c["chunk_text"],
            _vec(c["chunk_vector"]),
        ))

    sql = f"""
        INSERT INTO {DB_SCHEMA}.doc_chunks_{FOLDER}
            (chunk_id, doc_id, file_name, file_path,
             chunk_index, chunk_total, chunk_level,
             section_title, page_start, page_end, content_types,
             chunk_text, chunk_vector,
             created_at, updated_at)
        VALUES
            (%s, %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s, %s,
             %s, %s::vector,
             NOW(), NOW())
        ON CONFLICT (chunk_id) DO UPDATE SET
            chunk_text   = EXCLUDED.chunk_text,
            chunk_vector = EXCLUDED.chunk_vector,
            updated_at   = NOW();
    """

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=50)
        conn.commit()
        logger.info(f"Inserted/updated {len(chunks)} chunks.")
    except Exception as e:
        conn.rollback()
        logger.error(f"insert_chunks_batch failed: {e}")
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Stats / monitoring queries
# ─────────────────────────────────────────────────────────────────────────────

def get_pipeline_stats() -> dict:
    """Return a summary of the current ingestion state."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT
                    status,
                    COUNT(*)        AS doc_count,
                    SUM(chunk_count) AS total_chunks,
                    SUM(pages_total) AS total_pages
                FROM {DB_SCHEMA}.ingestion_log_{FOLDER}
                GROUP BY status
                ORDER BY status;
            """)
            rows = cur.fetchall()
            return {"by_status": [dict(r) for r in rows]}
    finally:
        conn.close()


def get_failed_docs() -> list[dict]:
    """Return all documents that failed ingestion — for debugging."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT doc_id, file_name, file_path, status,
                       error_message, retry_count, updated_at
                FROM {DB_SCHEMA}.ingestion_log_{FOLDER}
                WHERE status = 'ERROR'
                ORDER BY updated_at DESC;
            """)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
