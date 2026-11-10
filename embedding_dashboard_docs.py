"""Document-centric, read-only metrics for leadership dashboards.

A document is identified by doc_id; it is complete only when EVERY source
chunk in that document has a non-NULL vector. Source data is authoritative,
not the per-run task ledger (which can include old overlapping jobs).
"""
from __future__ import annotations

import json
from pathlib import Path

from psycopg2 import sql

from embedding_dashboard_data import _rows, connect_readonly

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
DOCUMENT_QUERY_TIMEOUT_MS = 60000
DOCUMENT_CACHE_SECONDS = 600
MAX_EXPLORE_CHUNKS = 200


def datasets_from_profiles(directory=PROFILE_DIR):
    """Curated local profile mappings, not arbitrary user-entered SQL identifiers."""
    datasets = {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mapping = data.get("existing_chunks")
        if not isinstance(mapping, dict):
            continue
        if not all(isinstance(mapping.get(k),str) and mapping[k]
                   for k in ("schema","table","id_column","vector_column")):
            continue
        key = (mapping["schema"],mapping["table"],mapping["vector_column"])
        # Prefer the named immbot profile when profiles target the same table.
        item = {"profile": path.name, "schema":mapping["schema"],
                "table":mapping["table"], "vector_column":mapping["vector_column"],
                "id_column":mapping["id_column"]}
        if key not in datasets or "immbot_approved" in path.name:
            datasets[key] = item
    return sorted(datasets.values(),key=lambda p: (
        "immbot_approved" not in p["profile"],p["schema"],p["table"]))


def document_stage(total_chunks, embedded_chunks):
    if total_chunks <= 0:
        return "Unknown"
    if embedded_chunks >= total_chunks:
        return "Complete"
    if embedded_chunks > 0:
        return "Partial"
    return "Not started"


def calculate_coverage(embedded_chunks, total_chunks):
    return (embedded_chunks / total_chunks) if total_chunks else 0.0


def _source_columns(conn, dataset):
    # Identifiers are always quoted through psycopg2.sql.Identifier below.
    cols = _rows(conn, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
    """,(dataset["schema"],dataset["table"]))
    found = {row["column_name"] for row in cols}
    required={"doc_id",dataset["vector_column"],dataset["id_column"]}
    if not required.issubset(found):
        raise ValueError(
            "This profile's source table needs doc_id, its row key and the vector column "
            "to show document-level completion."
        )
    return found


def _table(dataset):
    return sql.Identifier(dataset["schema"],dataset["table"])


def _col(name, available, fallback):
    return sql.Identifier(name) if name in available else sql.SQL(fallback)


def fetch_document_rollup(dataset):
    """Full-table document aggregate; only call from a long-TTL cache.

    One source-table pass groups by doc_id. A single MATERIALIZED CTE powers
    summary totals and a bounded list of unfinished documents. No vectors or
    source text leave PostgreSQL. A 60-second timeout protects the database.
    """
    conn=connect_readonly()
    try:
        available=_source_columns(conn,dataset)
        table=_table(dataset)
        vector=sql.Identifier(dataset["vector_column"])
        file_col=_col("file_name",available,"NULL::text")
        note_col=_col("embedding_processing_note",available,"NULL::text")
        updated_col=_col("updated_at",available,"NULL::timestamptz")
        query=sql.SQL("""
          WITH per_document AS MATERIALIZED (
              SELECT NULLIF(BTRIM(doc_id),'') AS document_id,
                     COUNT(*)::bigint AS chunks,
                     COUNT(*) FILTER (WHERE {vector} IS NOT NULL)::bigint AS embedded,
                     COUNT(*) FILTER (
                         WHERE {vector} IS NULL AND {note} IS NOT NULL
                     )::bigint AS noted_chunks,
                     MIN({file}) AS file_name,
                     MAX({updated}) AS last_source_update
              FROM {table}
              GROUP BY NULLIF(BTRIM(doc_id),'')
          ),
          totals AS (
              SELECT COALESCE(SUM(chunks),0)::bigint AS total_chunks,
                     COALESCE(SUM(embedded),0)::bigint AS embedded_chunks,
                     COALESCE(SUM(chunks-embedded),0)::bigint AS missing_chunks,
                     COUNT(*) FILTER (WHERE document_id IS NOT NULL)::bigint AS total_documents,
                     COUNT(*) FILTER (
                         WHERE document_id IS NOT NULL AND chunks=embedded
                     )::bigint AS completed_documents,
                     COUNT(*) FILTER (
                         WHERE document_id IS NOT NULL AND embedded>0 AND embedded<chunks
                     )::bigint AS partial_documents,
                     COUNT(*) FILTER (
                         WHERE document_id IS NOT NULL AND embedded=0
                     )::bigint AS untouched_documents,
                     COUNT(*) FILTER (
                         WHERE document_id IS NOT NULL AND noted_chunks>0
                     )::bigint AS attention_documents,
                     COALESCE(SUM(chunks) FILTER (WHERE document_id IS NULL),0)::bigint
                         AS unmapped_chunks
              FROM per_document
          ),
          priority AS (
              SELECT document_id, file_name, chunks AS total_chunks,
                     embedded AS embedded_chunks,
                     chunks-embedded AS remaining_chunks, noted_chunks,
                     last_source_update
              FROM per_document
              WHERE document_id IS NOT NULL AND embedded<chunks
              ORDER BY (chunks-embedded) DESC, document_id
              LIMIT 25
          )
          SELECT totals.*,
                 (SELECT COALESCE(jsonb_agg(to_jsonb(priority)),
                                  '[]'::jsonb) FROM priority) AS priority_documents
          FROM totals
        """).format(vector=vector,note=note_col,file=file_col,
                    updated=updated_col,table=table)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s",(f"{DOCUMENT_QUERY_TIMEOUT_MS}ms",))
        data=_rows(conn,query)[0]
        data["snapshot_at"]=_rows(conn,"SELECT now() AS at")[0]["at"]
        return data
    finally:
        conn.close()


def fetch_document_details(dataset,doc_id):
    """Index-friendly exact doc_id lookup, bounded to 200 returned chunks."""
    doc_id=doc_id.strip()
    if not doc_id or len(doc_id)>1024:
        raise ValueError("Provide a doc_id between 1 and 1024 characters")
    conn=connect_readonly()
    try:
        available=_source_columns(conn,dataset)
        table=_table(dataset)
        vector=sql.Identifier(dataset["vector_column"])
        key=sql.Identifier(dataset["id_column"])
        chunk_index=_col("chunk_index",available,"NULL::integer")
        file_col=_col("file_name",available,"NULL::text")
        note_col=_col("embedding_processing_note",available,"NULL::text")
        updated_col=_col("updated_at",available,"NULL::timestamptz")
        expected_col=_col("chunk_total",available,"NULL::integer")

        query=sql.SQL("""
          SELECT {key}::text AS chunk_id,
                 {index} AS chunk_index,
                 {expected} AS source_chunk_total,
                 {file} AS file_name,
                 ({vector} IS NOT NULL) AS embedded,
                 LEFT(COALESCE({note},''),200) AS note,
                 {updated} AS updated_at
          FROM {table}
          WHERE doc_id = %s
          ORDER BY {index} NULLS LAST, {key}
          LIMIT %s
        """).format(key=key,index=chunk_index,expected=expected_col,
                    file=file_col,vector=vector,note=note_col,
                    updated=updated_col,table=table)
        chunks=_rows(conn,query,(doc_id,MAX_EXPLORE_CHUNKS+1))
        aggregate=sql.SQL("""
          SELECT COUNT(*)::bigint AS total_chunks,
                 COUNT(*) FILTER (WHERE {vector} IS NOT NULL)::bigint AS embedded_chunks,
                 MAX({expected}) AS declared_chunk_total,
                 COUNT(*) FILTER (WHERE {note} IS NOT NULL AND {vector} IS NULL)::bigint
                   AS noted_chunks
          FROM {table} WHERE doc_id=%s
        """).format(vector=vector,expected=expected_col,note=note_col,table=table)
        total=_rows(conn,aggregate,(doc_id,))[0]
        return {"document_id":doc_id,"summary":total,
                "rows":chunks[:MAX_EXPLORE_CHUNKS],
                "truncated":len(chunks)>MAX_EXPLORE_CHUNKS}
    finally:
        conn.close()
