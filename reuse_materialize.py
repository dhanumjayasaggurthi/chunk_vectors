"""Opt-in profile materialization from existing PostgreSQL content.

NO PDF/OCR calls. NO Azure calls. NO changes to fast_embedding_runtime.py.
Creates a dedicated embedding-ready output table (chunk_text, chunk_vector,
chunk_id) and keeps the original chunk text in source_chunk_text. Atomic
per-document replacement, resumability and durable error events.

This entrypoint deliberately has no default production target.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import re
import sys
import time
from datetime import date, datetime

LOG = logging.getLogger("epod.reuse_materialize")


def identifiers(profile):
    from profile_store import _safe_identifier
    from extracted_source import validate_source
    source = validate_source(profile)
    target = profile.get("output") or {}
    if not target.get("schema") or not target.get("table"):
        raise ValueError("Explicit output.schema and output.table are mandatory")
    if (source["schema"].casefold(), source["table"].casefold()) == (
        target["schema"].casefold(), target["table"].casefold()
    ):
        raise ValueError("Source and destination must be different tables")
    for value in (target["schema"], target["table"]):
        _safe_identifier(value)
    if len(target["table"]) > 48:
        raise ValueError("Output table name is too long for companion tables/indexes")
    return {
        "source": (_safe_identifier(source["schema"]) + "." +
                   _safe_identifier(source["table"])),
        "table": (_safe_identifier(target["schema"]) + "." +
                  _safe_identifier(target["table"])),
        "status": (_safe_identifier(target["schema"]) + "." +
                   _safe_identifier(target["table"] + "_status")),
        "failures": (_safe_identifier(target["schema"]) + "." +
                     _safe_identifier(target["table"] + "_failures")),
        "schema": _safe_identifier(target["schema"]),
        "table_name": target["table"],
    }


def _safe_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def compose_embedding_text(chunk, fields, sources, *, max_tokens=6000, tokenizer="cl100k_base"):
    """Deterministic labeled concatenation; never hallucinate missing fields.

    The source's original chunk text remains a separate DB column.
    Reject excessive composite inputs instead of silently truncating.
    """
    if not isinstance(fields, list) or not fields or "chunk_text" not in fields:
        raise ValueError("input_fields must contain chunk_text")
    import tiktoken
    encoder = tiktoken.get_encoding(tokenizer)
    values = []
    document = getattr(chunk, "document_metadata", {}) or {}
    local = getattr(chunk, "chunk_metadata", {}) or {}
    for key in fields:
        if key == "chunk_text":
            value = chunk.chunk_text
        elif key in ("section_title", "file_name", "file_path"):
            value = getattr(chunk, key)
        elif key.startswith("document."):
            value = document.get(key[9:])
        elif key.startswith("chunk."):
            value = local.get(key[6:])
        elif key.startswith("source."):
            value = sources.get(key[7:])
        else:
            raise ValueError("Unknown embedding input field: " + key)
        if value is None or value == "":
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("Embedding field " + key + " is not scalar")
        val = str(value).strip()
        if not val:
            continue
        if key == "chunk_text":
            values.append(val)
        else:
            values.append(key.replace("_", " ").replace(".", " / ") + ": " + val)
    result = "\n\n".join(values).strip()
    if not result:
        raise ValueError("Empty embedding text")
    size = len(encoder.encode(result, disallowed_special=()))
    if size > max_tokens:
        raise ValueError(
            f"Composed embedding input exceeds {max_tokens} token limit ({size})"
        )
    return result


def prepare_document(struct, profile):
    """Reuse the existing token chunker and metadata extractor."""
    from configurable_chunking import chunk_with_profile
    from chunker import chunk_document
    from metadata_extractor import attach_metadata

    policy = profile["chunking"]
    if policy["strategy"] == "legacy":
        chunks = chunk_document(struct)
    else:
        chunks = chunk_with_profile(struct, policy)
    if not chunks:
        raise ValueError("Document yielded no chunks; source remains unchanged")
    doc_meta = attach_metadata(struct, chunks, profile["metadata"])
    source_fields = getattr(struct, "source_metadata", {})
    # A source column is authoritative only when consistent across all source
    # rows. Never replace a contradictory extracted metadata value silently.
    evidence = dict(getattr(struct, "metadata_evidence", {}))
    for key, value in source_fields.items():
        if key in doc_meta and str(doc_meta[key]) != str(value):
            raise ValueError("Metadata conflict between source column and extracted field: " + key)
        doc_meta[key] = value
        evidence[key] = {
            "method": "source_column",
            "column": profile["extracted_content"].get("source_columns", {}).get(key)
        }
    fields = profile["embedding"].get("input_fields", ["chunk_text"])
    max_tokens = profile["embedding"].get("input_max_tokens", 6000)
    tokenizer = policy.get("tokenizer", "cl100k_base")
    result = []
    for chunk in chunks:
        chunk.document_metadata = dict(doc_meta)
        chunk.effective_metadata = {
            **(doc_meta if profile["metadata"].get("inherit_document_fields", True) else {}),
            **getattr(chunk, "chunk_metadata", {}),
        }
        vector_input = compose_embedding_text(
            chunk, fields, source_fields,
            max_tokens=max_tokens, tokenizer=tokenizer,
        )
        identity = hashlib.sha256(
            (profile["_fingerprint"] + "\x00" + chunk.doc_id + "\x00" +
             str(chunk.chunk_index)).encode("utf-8")
        ).hexdigest()
        result.append((
            identity, chunk.doc_id, profile["_fingerprint"], chunk.chunk_index,
            chunk.file_path, chunk.file_name, chunk.chunk_text, vector_input,
            chunk.page_start, chunk.page_end, chunk.section_title,
            _safe_json(getattr(chunk, "headers", [])),
            _safe_json(doc_meta), _safe_json(getattr(chunk, "chunk_metadata", {})),
            _safe_json(getattr(chunk, "content_types", [])),
            _safe_json(evidence),
        ))
    return result


def init_output(profile, *, apply=False):
    """Only an explicit --apply writes DDL; existing tables are never migrated."""
    names = identifiers(profile)
    if not apply:
        return {"ddl_required": True, "destination": names["table"],
                "status": "NO_WRITES"}
    from db import get_conn
    from profile_store import _safe_identifier
    dims = profile["embedding"].get("dimensions", 1536)
    if type(dims) is not int or not 1 <= dims <= 16000:
        raise ValueError("embedding.dimensions must be 1..16000")
    if len(names["table_name"] + "_doc_idx") > 63:
        raise ValueError("Output index identifier too long")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Do not enable extensions or change source schemas.
            cur.execute("SELECT 1 FROM pg_type WHERE typname='vector'")
            if not cur.fetchone():
                raise ValueError("pgvector must already be installed by the DBA")
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {names['schema']}")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {names['table']} (
                  chunk_id text PRIMARY KEY,
                  doc_id text NOT NULL,
                  profile_hash text NOT NULL,
                  chunk_index integer NOT NULL,
                  file_path text NOT NULL,
                  file_name text NOT NULL,
                  source_chunk_text text NOT NULL,
                  chunk_text text NOT NULL,
                  chunk_vector vector({dims}),
                  embedding_processing_note text,
                  page_start integer,
                  page_end integer,
                  section_title text,
                  headers jsonb NOT NULL,
                  document_metadata jsonb NOT NULL,
                  chunk_metadata jsonb NOT NULL,
                  content_types jsonb NOT NULL,
                  metadata_evidence jsonb NOT NULL
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {names['status']} (
                  doc_id text PRIMARY KEY, profile_hash text NOT NULL,
                  source_hash text NOT NULL, chunk_count bigint NOT NULL,
                  updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {names['failures']} (
                  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  doc_id text NOT NULL, profile_hash text NOT NULL,
                  error_code text NOT NULL, error_message text NOT NULL,
                  occurred_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_safe_identifier(names['table_name'] + '_doc_idx')} "
                f"ON {names['table']} (doc_id)"
            )
            cur.execute(f"SELECT DISTINCT profile_hash FROM {names['status']} LIMIT 2")
            seen = {x[0] for x in cur.fetchall()}
            if seen and seen != {profile["_fingerprint"]}:
                raise ValueError("Output table already contains a different processing profile")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"created_or_verified": names["table"], "source_unchanged": True}


def verify_output(profile):
    """Refuse to write to an uninitialized or differently configured table."""
    from db import get_conn
    names = identifiers(profile)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for name in ("table", "status", "failures"):
                cur.execute("SELECT to_regclass(%s)", (names[name],))
                if cur.fetchone()[0] is None:
                    raise ValueError("Missing output table; run init --apply in staging first")
            cur.execute(f"SELECT DISTINCT profile_hash FROM {names['status']} LIMIT 2")
            seen = {x[0] for x in cur.fetchall()}
            if seen and seen != {profile["_fingerprint"]}:
                raise ValueError("Output table has a different profile hash; use a NEW table")
            cur.execute(f"""SELECT column_name,data_type FROM information_schema.columns
                            WHERE table_schema=%s AND table_name=%s""",
                        (profile["output"]["schema"], profile["output"]["table"]))
            columns = dict(cur.fetchall())
            expected = {"chunk_id","doc_id","profile_hash","chunk_index","file_path",
                        "file_name","source_chunk_text","chunk_text","chunk_vector",
                        "embedding_processing_note","page_start","page_end",
                        "section_title","headers","document_metadata",
                        "chunk_metadata","content_types","metadata_evidence"}
            if not expected.issubset(columns):
                raise ValueError("Output table incompatible with materialization schema")
            cur.execute(f"""SELECT a.atttypmod FROM pg_attribute a
                WHERE a.attrelid=%s::regclass AND a.attname='chunk_vector'""",
                (names["table"],))
            dim_row = cur.fetchone()
            dims = profile["embedding"].get("dimensions",1536)
            if dim_row is None or dim_row[0] != dims:
                raise ValueError("Output pgvector dimension differs from profile")
    finally:
        conn.close()
    return names


def _current(doc_id, source_hash, profile, names):
    from db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT 1 FROM {names['status']}
                WHERE doc_id=%s AND source_hash=%s AND profile_hash=%s""",
                (doc_id, source_hash, profile["_fingerprint"]))
            return cur.fetchone() is not None
    finally:
        conn.close()


def save_document(doc_id, source_hash, profile, names, rows):
    from db import get_conn
    from psycopg2.extras import execute_values
    if not rows or any(row[1] != doc_id for row in rows):
        raise ValueError("Cannot publish missing/mixed document chunks")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # A document is replaced only after every new chunk has passed
            # deterministic chunking, metadata and embedding-input validation.
            cur.execute(f"DELETE FROM {names['table']} WHERE doc_id=%s", (doc_id,))
            execute_values(cur, f"""INSERT INTO {names['table']} (
                chunk_id,doc_id,profile_hash,chunk_index,file_path,file_name,
                source_chunk_text,chunk_text,page_start,page_end,section_title,
                headers,document_metadata,chunk_metadata,content_types,metadata_evidence
                ) VALUES %s""", rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)",
                page_size=200)
            cur.execute(f"""INSERT INTO {names['status']} (
                    doc_id,profile_hash,source_hash,chunk_count,updated_at)
                    VALUES (%s,%s,%s,%s,now())
                    ON CONFLICT (doc_id) DO UPDATE SET
                    profile_hash=EXCLUDED.profile_hash,
                    source_hash=EXCLUDED.source_hash,
                    chunk_count=EXCLUDED.chunk_count,updated_at=now()""",
                    (doc_id, profile["_fingerprint"], source_hash, len(rows)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_failure(doc_id, profile, names, exc):
    from db import get_conn
    conn = get_conn()
    message = (type(exc).__name__ + ": " + str(exc))[:1000]
    try:
        with conn.cursor() as cur:
            cur.execute(f"""INSERT INTO {names['failures']} (
                doc_id, profile_hash, error_code, error_message
                ) VALUES (%s,%s,%s,%s)""",
                (doc_id, profile["_fingerprint"], type(exc).__name__[:80], message))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_one(doc_id, profile, names):
    from profile_store import profile_document_lock
    from extracted_source import load_extracted_document
    try:
        with profile_document_lock(str(doc_id), profile["_fingerprint"]) as acquired:
            if not acquired:
                return {"doc_id":str(doc_id),"status":"BUSY"}
            struct, digest = load_extracted_document(profile, doc_id)
            if _current(str(doc_id),digest,profile,names):
                return {"doc_id":str(doc_id),"status":"SKIPPED"}
            rows=prepare_document(struct,profile)
            save_document(str(doc_id),digest,profile,names,rows)
            return {"doc_id":str(doc_id),"status":"SUCCESS","chunks":len(rows)}
    except Exception as exc:
        # A broken document cannot poison other documents. A DB outage must
        # still surface: never claim durable error logging when it failed.
        record_failure(str(doc_id),profile,names,exc)
        LOG.error("Document %s materialization failed: %s",doc_id,type(exc).__name__)
        return {"doc_id":str(doc_id),"status":"FAILED","error":type(exc).__name__}


def run(profile, *, max_docs, workers):
    from extracted_source import iter_document_ids
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("workers must be 1..32")
    if type(max_docs) is not int or max_docs < 1:
        raise ValueError("max_docs must be positive (bounded opt-in run)")
    names=verify_output(profile)
    counts={"SUCCESS":0,"SKIPPED":0,"FAILED":0,"BUSY":0,"chunks":0}
    pending={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        def collect(done):
            for future in done:
                doc=pending.pop(future)
                try:
                    result=future.result()
                except Exception as exc:
                    # A failure to persist a failure event is a hard failure
                    # for the run; do not silently continue.
                    raise RuntimeError(
                        "Fatal materialization error for " + str(doc)
                    ) from exc
                counts[result["status"]]+=1
                counts["chunks"]+=result.get("chunks",0)
        for doc in iter_document_ids(profile,max_docs=max_docs):
            pending[pool.submit(run_one,doc,profile,names)]=doc
            if len(pending)>=workers*2:
                done,_=concurrent.futures.wait(
                    pending,return_when=concurrent.futures.FIRST_COMPLETED)
                collect(done)
        while pending:
            done,_=concurrent.futures.wait(
                pending,return_when=concurrent.futures.FIRST_COMPLETED)
            collect(done)
    return counts


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("validate","init","run","preview"))
    parser.add_argument("--profile",required=True)
    parser.add_argument("--apply",action="store_true",
                        help="Explicit DDL approval for a new output table")
    parser.add_argument("--max-docs",type=int,default=10)
    parser.add_argument("--doc-workers",type=int,default=2)
    parser.add_argument("--doc-id",help="Required for read-only document preview")
    args=parser.parse_args(argv)

    from processing_profile import load_profile
    profile=load_profile(args.profile)
    names=identifiers(profile)
    if args.command=="validate":
        info={"valid":True,"source":names["source"],
              "output":names["table"],"profile_hash":profile["_fingerprint"],
              "embedding_input_fields":profile["embedding"].get("input_fields",["chunk_text"]),
              "note":"No database writes, OCR calls or Azure calls"}
    elif args.command=="init":
        info=init_output(profile,apply=args.apply)
    elif args.command=="preview":
        if not args.doc_id:
            parser.error("--doc-id required for preview")
        from extracted_source import load_extracted_document
        struct,source_hash=load_extracted_document(profile,args.doc_id)
        rows=prepare_document(struct,profile)
        info={"document_id":str(args.doc_id),"source_hash":source_hash,
              "prepared_chunks":len(rows),
              "first_chunk_input_characters":len(rows[0][7]),
              "first_chunk_metadata_fields":list(json.loads(rows[0][12])),
              "note":"Preview only; no document text or private data printed"}
    else:
        info=run(profile,max_docs=args.max_docs,workers=args.doc_workers)
    print(json.dumps(info,indent=2))
    return 0 if not info.get("FAILED") else 1


if __name__=="__main__":
    raise SystemExit(main())
