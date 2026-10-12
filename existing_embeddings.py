"""Generate only missing vectors in an existing PostgreSQL table.

Supports configured schema/table/column names. Never opens PDFs or re-extracts text.
Keyset pagination and per-batch transactions bound memory regardless of table size.
"""
from __future__ import annotations

from config import EMBEDDING_DIM
from logger import get_logger

logger = get_logger("epod.existing_embeddings")
NOTE_EMPTY = "Question text is empty; no embedding needed"
NOTE_RETRY = "Embedding not completed; eligible for retry"
NOTE_INVALID = "Embedding response needs review; eligible for retry"
from db import get_conn
from embedder import _embed_batch
from profile_store import _safe_identifier


def _source_config(profile: dict) -> dict:
    source = profile.get("existing_chunks") or {}
    for key in ("schema", "table", "id_column", "text_column", "vector_column"):
        if not source.get(key):
            raise ValueError("existing_chunks." + key + " is required for --embed-existing")
        _safe_identifier(source[key])
    if source.get("note_column"):
        _safe_identifier(source["note_column"])
    return source


def embed_existing(profile: dict, *, dry_run: bool = False, max_chunks: int | None = None) -> dict:
    source = _source_config(profile)
    if not profile["embedding"]["enabled"]:
        raise ValueError("Embeddings are disabled in the selected profile")
    table = _safe_identifier(source["schema"]) + "." + _safe_identifier(source["table"])
    id_col = _safe_identifier(source["id_column"])
    text_col = _safe_identifier(source["text_column"])
    vector_col = _safe_identifier(source["vector_column"])
    note_col = _safe_identifier(source["note_column"]) if source.get("note_column") else None
    progress_every = profile["embedding"].get("progress_every_batches", 10)
    batch_size = profile["embedding"]["batch_size"]
    requested_dimensions = profile["embedding"].get("dimensions", EMBEDDING_DIM)
    deployment = profile["embedding"].get("deployment")
    model = profile["embedding"].get("model")
    reuse = profile["embedding"]["reuse_existing"]
    where = f"{vector_col} IS NULL" if reuse else "TRUE"
    stats = {"scanned": 0, "embedded": 0, "failed": 0, "skipped_empty": 0}
    last_id = None
    batch_number = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Fail fast on invalid mappings / permissions; do not mutate schema.
            cur.execute(f"SELECT {id_col}, {text_col}, {vector_col} FROM {table} LIMIT 0")
        # Detect a mismatched pgvector column before making any Azure requests.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.atttypmod
                FROM pg_attribute a
                WHERE a.attrelid = %s::regclass AND a.attname = %s
                  AND a.attnum > 0 AND NOT a.attisdropped
            """, (source["schema"] + "." + source["table"], source["vector_column"]))
            metadata = cur.fetchone()
            if not metadata or metadata[0] != requested_dimensions:
                raise ValueError(
                    f"Target vector column dimension {metadata[0] if metadata else 'unknown'} "
                    f"does not match configured embedding dimension {requested_dimensions}"
                )
        if note_col:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {note_col} FROM {table} LIMIT 0")
        while True:
            if max_chunks is not None and stats["scanned"] >= max_chunks:
                break
            limit = min(batch_size, max_chunks - stats["scanned"]) if max_chunks is not None else batch_size
            cursor_clause = f"AND {id_col} > %s" if last_id is not None else ""
            params = (last_id, limit) if last_id is not None else (limit,)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {id_col}, {text_col} FROM {table} "
                    f"WHERE {where} {cursor_clause} ORDER BY {id_col} LIMIT %s", params
                )
                rows = cur.fetchall()
            if not rows:
                break
            batch_number += 1
            last_id = rows[-1][0]
            stats["scanned"] += len(rows)
            usable = [(key, content) for key, content in rows if isinstance(content, str) and content.strip()]
            empty_keys = [key for key, content in rows
                          if not isinstance(content, str) or not content.strip()]
            stats["skipped_empty"] += len(empty_keys)
            if dry_run:
                if batch_number % progress_every == 0:
                    logger.info("Preview scanned=%s pending=%s empty=%s",
                                stats["scanned"], len(usable), stats["skipped_empty"])
                continue
            if note_col and empty_keys:
                with conn.cursor() as cur:
                    for key in empty_keys:
                        cur.execute(f"UPDATE {table} SET {note_col}=%s WHERE {id_col}=%s AND ({where})",
                                    (NOTE_EMPTY, key))
                conn.commit()
            if not usable:
                if batch_number % progress_every == 0:
                    logger.info("Embedding progress scanned=%s embedded=%s failed=%s empty=%s",
                                stats["scanned"], stats["embedded"], stats["failed"], stats["skipped_empty"])
                continue
            embed_kwargs = {}
            if "dimensions" in profile["embedding"]:
                embed_kwargs["dimensions"] = requested_dimensions
            if deployment:
                embed_kwargs["deployment"] = deployment
            if model:
                embed_kwargs["model"] = model
            vectors = _embed_batch([text for _, text in usable], **embed_kwargs)
            if vectors is None or len(vectors) != len(usable) or any(
                len(v) != requested_dimensions or not any(v) for v in vectors
            ):
                stats["failed"] += len(usable)
                if note_col:
                    with conn.cursor() as cur:
                        for key, _ in usable:
                            cur.execute(f"UPDATE {table} SET {note_col}=%s WHERE {id_col}=%s AND ({where})",
                                        (NOTE_INVALID, key))
                    conn.commit()
                logger.warning("Embedding batch %s incomplete; %s rows remain eligible for retry",
                               batch_number, len(usable))
                continue
            try:
                with conn.cursor() as cur:
                    for (key, _), vector in zip(usable, vectors):
                        serialized = "[" + ",".join(str(float(v)) for v in vector) + "]"
                        note_assignment = f", {note_col}=NULL" if note_col else ""
                        cur.execute(
                            f"UPDATE {table} SET {vector_col}=%s::vector{note_assignment} "
                            f"WHERE {id_col}=%s AND ({where})",
                            (serialized, key),
                        )
                        stats["embedded"] += cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            if batch_number % progress_every == 0:
                logger.info("Embedding progress scanned=%s embedded=%s failed=%s empty=%s",
                            stats["scanned"], stats["embedded"], stats["failed"], stats["skipped_empty"])
    finally:
        conn.close()
    return stats
