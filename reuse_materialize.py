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
from itertools import chain

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


def iter_prepared_document_rows(struct, profile, *, release_source=False):
    """Yield deterministic publication rows without retaining a second copy.

    Chunk boundary calculation remains document-scoped and unchanged. For the
    native-only giant-document path, validated source pages are released after
    metadata extraction and each chunk object is released after its row is
    consumed by the transactional writer.
    """
    from configurable_chunking import chunk_with_profile
    from chunker import chunk_document
    from metadata_extractor import (
        attach_chunk_metadata, attach_metadata, extract_document_metadata,
    )

    policy = profile["chunking"]
    if release_source:
        doc_meta,evidence=extract_document_metadata(struct,profile["metadata"])
        struct.release_page_payloads_after_chunking=True
    try:
        if policy["strategy"] == "legacy":
            chunks = chunk_document(struct)
        else:
            chunks = chunk_with_profile(struct, policy)
    finally:
        if release_source and hasattr(struct,"release_page_payloads_after_chunking"):
            del struct.release_page_payloads_after_chunking
    if release_source:
        attach_chunk_metadata(chunks,profile["metadata"],doc_meta)
        struct.metadata_evidence=evidence
    else:
        doc_meta = attach_metadata(struct, chunks, profile["metadata"])
    if not chunks:
        raise ValueError("Document yielded no chunks; source remains unchanged")
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
    # Materialized chunk IDs include the processing-profile fingerprint.
    # Chunker neighbor IDs are source-local and would NOT resolve to rows
    # in the output table. Remap links before writing metadata.
    published_ids=[
        hashlib.sha256(
            (profile["_fingerprint"] + "\x00" + item.doc_id + "\x00" +
             str(item.chunk_index)).encode("utf-8")
        ).hexdigest()
        for item in chunks
    ]
    if policy.get("boundary_mode","v1")=="semantic_v2":
        for index,item in enumerate(chunks):
            item.chunk_metadata["previous_chunk_id"]=(
                published_ids[index-1] if index else None
            )
            item.chunk_metadata["next_chunk_id"]=(
                published_ids[index+1] if index+1<len(chunks) else None
            )
    document_metadata_json = _safe_json(doc_meta)
    metadata_evidence_json = _safe_json(evidence)
    if release_source:
        struct.pages.clear()
        struct.toc.clear()
    for index in range(len(chunks)):
        chunk = chunks[index]
        chunk.document_metadata = dict(doc_meta)
        chunk.effective_metadata = {
            **(doc_meta if profile["metadata"].get("inherit_document_fields", True) else {}),
            **getattr(chunk, "chunk_metadata", {}),
        }
        vector_input = compose_embedding_text(
            chunk, fields, source_fields,
            max_tokens=max_tokens, tokenizer=tokenizer,
        )
        identity = published_ids[chunk.chunk_index]
        row = (
            identity, chunk.doc_id, profile["_fingerprint"], chunk.chunk_index,
            chunk.file_path, chunk.file_name, chunk.chunk_text, vector_input,
            chunk.page_start, chunk.page_end, chunk.section_title,
            _safe_json(getattr(chunk, "headers", [])),
            document_metadata_json, _safe_json(getattr(chunk, "chunk_metadata", {})),
            _safe_json(getattr(chunk, "content_types", [])),
            metadata_evidence_json,
        )
        yield row
        if release_source:
            chunks[index] = None


def prepare_document(struct, profile):
    """Compatibility wrapper returning the existing in-memory row list."""
    return list(iter_prepared_document_rows(struct, profile))



DOCX_CHUNK_CONTRACT = "docx_logical_spans_v1"


def _docx_identity(doc_id, file_name, capture_id):
    from visual_evidence import _hash_id
    _hash_id(capture_id)
    if not isinstance(doc_id,str) or not re.fullmatch(r"docx:[A-Za-z0-9_.:-]{1,240}",doc_id):
        raise ValueError("DOCX requires an explicit stable docx: namespaced document ID")
    if (not isinstance(file_name,str) or not file_name.lower().endswith(".docx")
            or len(file_name)>512 or any(c in file_name for c in ("/","\\","\x00"))):
        raise ValueError("Explicit DOCX file name (not source path) required")
    return hashlib.sha256(_safe_json([DOCX_CHUNK_CONTRACT,doc_id,file_name,capture_id]).encode()).hexdigest()


def _exact_slices(text,budget,count):
    """Unicode character ranges, not token decoding or whitespace normalization."""
    start=0
    while start<len(text):
        # Use a bounded initial probe, avoiding repeated encoding of the entire
        # remaining paragraph. Every emitted piece is checked with the tokenizer.
        hi=min(len(text),start+budget*4)
        while hi<len(text) and count(text[start:hi])<=budget:
            hi=min(len(text),start+2*(hi-start))
        if count(text[start:hi])>budget:
            lo=start+1
            while lo<hi:
                mid=(lo+hi+1)//2
                if count(text[start:mid])<=budget:
                    lo=mid
                else:
                    hi=mid-1
            hi=lo
        if count(text[start:hi])>budget:
            raise ValueError("Single Unicode character exceeds logical chunk budget")
        yield start,hi,text[start:hi]
        start=hi


def prepare_docx_document(root,capture_id,profile,*,config,doc_id,file_name):
    """Prepare the SAME existing 16-column publication rows from logical evidence.

    Source spans address character offsets in the concatenated native segments
    of a retained block. Paragraphs can pack within the same story/cell only.
    Review-sensitive blocks and whitespace-only blocks stay in evidence, not
    automatically indexed. No page geometry, inferred table headers, or AI.
    """
    from types import SimpleNamespace
    import tiktoken
    from docx_evidence import iter_logical_blocks
    source_hash=_docx_identity(doc_id,file_name,capture_id)
    policy=profile["chunking"]
    tokenizer=policy.get("tokenizer","cl100k_base")
    if tokenizer not in ("cl100k_base","o200k_base"):
        raise ValueError("Unsupported tokenizer")
    budget=policy["max_tokens"]
    if type(budget) is not int or budget<16:
        raise ValueError("Invalid DOCX chunk token budget")
    encoder=tiktoken.get_encoding(tokenizer)
    count=lambda text:len(encoder.encode(text,disallowed_special=()))
    # PDF metadata rules can be page-scoped. Never pretend they ran on DOCX.
    omitted=[]
    for level in ("document","chunk"):
        for key,rule in profile.get("metadata",{}).get(level,{}).items():
            if rule.get("enabled",True):
                if rule.get("required",False):
                    raise ValueError("Required PDF metadata rule is not supported for logical DOCX: "+key)
                omitted.append(level+"."+key)
    source_uri="evidence://docx/"+capture_id
    fields=profile["embedding"].get("input_fields",["chunk_text"])
    max_input=profile["embedding"].get("input_max_tokens",6000)
    sources={"source_file_name":file_name,"source_format":"docx"}
    pending=[]
    prepared=[]
    scope=None
    omitted_review=omitted_whitespace=omitted_split_whitespace=0
    indexed_chars=withheld_chars=0
    source_sha=None

    def emit():
        nonlocal pending
        if not pending:
            return
        text="\n".join(x[0] for x in pending)
        if not text.strip() or count(text)>budget:
            raise ValueError("Invalid logical chunk token budget")
        prepared.append((text,[x[1] for x in pending]))
        pending=[]

    for block in iter_logical_blocks(root,capture_id,config=config):
        source_sha=block["source_docx_sha256"]
        if block["kind"] not in ("paragraph","unscoped_text"):
            continue
        text="".join(s["text"] for s in block["segments"])
        if not text.strip():
            omitted_whitespace+=1
            continue
        if block["review_context"]:
            emit()
            omitted_review+=1
            withheld_chars+=len(text)
            continue
        locator=block["locator"]
        ancestry=block["table_ancestry"]
        key=(locator["part"],_safe_json(ancestry))
        if key!=scope:
            emit()
            scope=key
        for start,end,piece in _exact_slices(text,budget,count):
            if not piece.strip():
                omitted_split_whitespace+=len(piece)
                withheld_chars+=len(piece)
                continue
            span={"block_id":block["block_id"],"locator":locator,
                  "char_start":start,"char_end":end,
                  "text_sha256":hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                  "table_ancestry":ancestry}
            if pending and count("\n".join([x[0] for x in pending]+[piece]))>budget:
                emit()
            pending.append((piece,span))
            indexed_chars+=len(piece)
    emit()
    if not prepared:
        raise ValueError("DOCX yielded no eligible logical text chunks")
    fingerprint=profile["_fingerprint"]
    ids=[hashlib.sha256((fingerprint+"\x00"+doc_id+"\x00"+str(i)).encode()).hexdigest()
         for i in range(len(prepared))]
    rows=[]
    doc_meta={"source_format":"docx","source_docx_sha256":source_sha,
              "source_capture_id":capture_id,"pagination":"NOT_RENDERED"}
    metadata_evidence={"source_format":{"method":"retained_docx_manifest"},
                       "omitted_optional_pdf_metadata_rules":omitted,
                       "source_text_status":"NATIVE_TOKENS_NOT_RENDERED"}
    for i,(text,spans) in enumerate(prepared):
        part=spans[0]["locator"]["part"]
        section_id=hashlib.sha256((capture_id+"\x00"+part).encode()).hexdigest()
        meta={"source_format":"docx","source_locator_kind":"docx_retained_token_path",
              "capture_id":capture_id,"logical_spans":spans,"section_id":section_id,
              "pagination":"NOT_RENDERED","source_text_status":"NATIVE_TOKENS_NOT_RENDERED",
              "source_facts_verified":False,"source_join_separator":"\n",
              "previous_chunk_id":ids[i-1] if i else None,
              "next_chunk_id":ids[i+1] if i+1<len(ids) else None}
        content_types=["table_cell_text" if spans[0]["table_ancestry"] else "text"]
        chunk=SimpleNamespace(chunk_text=text,section_title="",file_name=file_name,
                              file_path=source_uri,document_metadata=doc_meta,chunk_metadata=meta)
        composed=compose_embedding_text(chunk,fields,sources,max_tokens=max_input,tokenizer=tokenizer)
        rows.append((ids[i],doc_id,fingerprint,i,source_uri,file_name,text,composed,
                     None,None,"","[]",_safe_json(doc_meta),_safe_json(meta),
                     _safe_json(content_types),_safe_json(metadata_evidence)))
    report={"source_hash":source_hash,"prepared_chunks":len(rows),
            "indexed_source_characters":indexed_chars,"withheld_review_blocks":omitted_review,
            "withheld_source_characters":withheld_chars,"whitespace_only_blocks":omitted_whitespace,
            "withheld_split_whitespace_characters":omitted_split_whitespace,
            "omitted_optional_pdf_metadata_rules":omitted,
            "pagination":"NOT_RENDERED","chunk_contract":DOCX_CHUNK_CONTRACT,
            "overlap_tokens_applied":0,"purge_safe":False}
    quality=quality_summary(rows,policy)
    if quality["structural_errors"]:
        raise ValueError("Logical DOCX chunk structural integrity failed")
    return rows,{**report,"quality":quality}


def _docx_existing(doc_id,source_hash,profile,names,capture_id):
    """Check status and row counts in one read, refusing another format's ID."""
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT count(*),
                    count(*) FILTER (WHERE chunk_metadata->>'source_format'='docx'),
                    count(*) FILTER (WHERE chunk_metadata->>'capture_id'=%s AND profile_hash=%s)
                    FROM {names['table']} WHERE doc_id=%s""",
                    (capture_id,profile["_fingerprint"],doc_id))
            total,docx_rows,matching=cur.fetchone()
            if total!=docx_rows:
                raise ValueError("DOCX document ID collides with non-DOCX rows")
            cur.execute(f"""SELECT source_hash,profile_hash,chunk_count FROM {names['status']}
                            WHERE doc_id=%s""",(doc_id,))
            status=cur.fetchone()
            if status is None:
                if total:
                    raise ValueError("Existing DOCX rows lack a publication marker")
                return False
            if status[1]!=profile["_fingerprint"]:
                raise ValueError("DOCX document ID has a different profile")
            if status[0]==source_hash:
                if total!=status[2] or matching!=total or not total:
                    raise ValueError("DOCX publication reconciliation failed")
                return True
            return False
    finally:
        conn.close()


def run_docx(root,capture_id,profile,*,config,doc_id,file_name,allow_logical_locators=False,translator=None):
    """Explicit single-document publication using the existing DB transaction.

    Consumers must accept NULL physical page columns and logical span citations.
    The caller owns source-version selection; this is not a fleet scheduler.
    """
    if allow_logical_locators is not True:
        raise PermissionError("Explicit logical citation consumer approval required")
    if _english_contract(profile) and translator is None:
        raise PermissionError("English retrieval requires translation")
    from docx_evidence import verify
    from profile_store import profile_document_lock
    digest=_docx_identity(doc_id,file_name,capture_id)
    if translator is not None:
        digest=hashlib.sha256((digest+"\x00"+translator.identity).encode()).hexdigest()
    names=verify_output(profile,logical_locators=True)
    with profile_document_lock(doc_id,profile["_fingerprint"]) as acquired:
        if not acquired:
            return {"doc_id":doc_id,"status":"BUSY"}
        # Verify evidence even when the publication marker already exists.
        verify(root,capture_id,config=config)
        if _docx_existing(doc_id,digest,profile,names,capture_id):
            return {"doc_id":doc_id,"status":"SKIPPED"}
        rows,report=prepare_docx_document(root,capture_id,profile,config=config,
                                         doc_id=doc_id,file_name=file_name)
        if translator is not None:
            rows=translator.apply_rows(rows,profile,{"source_file_name":file_name,"source_format":"docx"})
        else:
            _reject_translation_loss(doc_id,names)
        save_document(doc_id,digest,profile,names,rows)
        return {"doc_id":doc_id,"status":"SUCCESS",**report}


def quality_summary(rows, policy):
    """Bounded, read-only quality indicators for an already prepared document.

    This is not a fidelity certification: original page geometry/cells stay
    authoritative. It catches unexpected oversized chunks, broken neighbor
    links and avoidable tiny-chunk prevalence before optional embeddings.
    """
    import collections
    import re
    import tiktoken
    if not rows:
        raise ValueError("No prepared chunks")
    encoding=tiktoken.get_encoding(policy.get("tokenizer","cl100k_base"))
    limit=policy["max_tokens"]
    floor=policy["min_tokens"]
    lengths=[]
    table_ids=set()
    multi_page_tables=set()
    semantic=policy.get("boundary_mode","v1")=="semantic_v2"
    packed_chunks=0
    packed_source_components=0
    exceptional=collections.Counter()
    short_reasons=collections.Counter()
    issues=[]
    ids=[row[0] for row in rows]
    token_count=lambda value:len(encoding.encode(value,disallowed_special=()))
    for index,row in enumerate(rows):
        chunk_id,doc_id,profile_hash,position=row[:4]
        text=row[6]
        start,end=row[8:10]
        meta=json.loads(row[13])
        size=token_count(text)
        lengths.append(size)
        if position!=index:
            issues.append(f"noncontiguous chunk_index {index}")
        if meta.get("source_format")=="docx":
            spans=meta.get("logical_spans")
            if (start is not None or end is not None or meta.get("pagination")!="NOT_RENDERED"
                    or not isinstance(spans,list) or not spans):
                issues.append(f"invalid logical source chunk {index}")
            else:
                for span in spans:
                    loc=span.get("locator",{})
                    path=loc.get("token_path")
                    if (loc.get("kind")!="docx_retained_token_path"
                            or not isinstance(loc.get("part"),str)
                            or not isinstance(path,list) or not path
                            or any(type(i) is not int or i<0 for i in path)
                            or type(span.get("char_start")) is not int
                            or type(span.get("char_end")) is not int
                            or not 0<=span["char_start"]<span["char_end"]
                            or not re.fullmatch(r"[0-9a-f]{64}",span.get("text_sha256",""))):
                        issues.append(f"invalid logical span chunk {index}")
        elif (not isinstance(start,int) or not isinstance(end,int)
                or start<1 or end<start):
            issues.append(f"invalid source pages chunk {index}")
        if not text.strip() or size>limit:
            issues.append(f"invalid token budget chunk {index}")
        prev=ids[index-1] if index else None
        nxt=ids[index+1] if index+1<len(ids) else None
        if semantic:
            if meta.get("previous_chunk_id")!=prev or meta.get("next_chunk_id")!=nxt:
                issues.append(f"broken neighbor links chunk {index}")
            if not meta.get("section_id"):
                issues.append(f"missing section_id chunk {index}")
        if "physical_table_body" in json.loads(row[14]):
            body=meta.get("physical_table_body")
            if (not isinstance(body,dict) or body.get("logical_rows_verified") is not False
                    or body.get("source_page")!=start or start!=end
                    or not re.fullmatch(r"[0-9a-f]{64}",body.get("physical_body_sha256",""))
                    or not re.fullmatch(r"[0-9a-f]{64}",body.get("source_header_sha256",""))
                    or meta.get("table_id") or meta.get("table_ids")
                    or not (body.get("physical_line_indices") or body.get("detected_header_row_indices"))):
                issues.append(f"invalid physical table layout provenance chunk {index}")
        for tid in meta.get("table_ids") or (
                [meta["table_id"]] if meta.get("table_id") else []):
            table_ids.add(tid)
            if meta.get("table_continuation_verified"):
                multi_page_tables.add(tid)
        for component in meta.get("table_components",[]):
            if component.get("table_id"):
                table_ids.add(component["table_id"])
                if component.get("continuation_verified"):
                    multi_page_tables.add(component["table_id"])
        components=meta.get("source_components",[])
        if components:
            packed_chunks+=1
            packed_source_components+=len(components)
            if len(components)<2 or meta.get("packing_policy")!="merge_forward":
                issues.append(f"invalid packing descriptor chunk {index}")
            referenced=set()
            for component in components:
                if (not isinstance(component,dict)
                        or type(component.get("page_start")) is not int
                        or type(component.get("page_end")) is not int
                        or component["page_start"]<start
                        or component["page_end"]>end
                        or not re.fullmatch(r"[0-9a-f]{64}",
                                            component.get("text_sha256",""))):
                    issues.append(f"invalid source component chunk {index}")
                    continue
                referenced.update(component.get("table_ids",[]))
            if referenced!=set(meta.get("table_ids",[])):
                issues.append(f"packed source table lineage mismatch chunk {index}")
        for reason in meta.get("exceptional_splits",[]):
            exceptional[reason]+=1
        if size<floor:
            if meta.get("table_id") or meta.get("table_ids"):
                short_reasons["table_boundary"]+=1
            elif meta.get("previous_chunk_id") is None or meta.get("next_chunk_id") is None:
                short_reasons["document_boundary"]+=1
            else:
                short_reasons["section_or_structure_boundary"]+=1
    ordered=sorted(lengths)
    def percentile(p):
        return ordered[max(0,(len(ordered)*p+99)//100-1)]
    return {
        "chunk_count":len(rows),
        "token_budget":limit,
        "min_tokens_soft_target":floor,
        "token_p50":percentile(50),
        "token_p90":percentile(90),
        "token_p95":percentile(95),
        "token_max":ordered[-1],
        "chunks_below_min_tokens":sum(x<floor for x in lengths),
        "chunks_above_budget":sum(x>limit for x in lengths),
        "short_chunk_categories":dict(short_reasons),
        "exceptional_split_categories":dict(exceptional),
        "packed_chunks":packed_chunks,
        "packed_source_components":packed_source_components,
        "estimated_original_chunks":len(rows)+packed_source_components-packed_chunks,
        "logical_tables":len(table_ids),
        "linked_multi_page_tables":len(multi_page_tables),
        "structural_errors":len(issues),
        "structural_error_examples":issues[:10],
    }


def _english_contract(profile):
    return profile.get("output",{}).get("text_contract")=="english_v1"


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
    native_column="native_text" if _english_contract(profile) else "source_chunk_text"
    native_constraint="" if _english_contract(profile) else " NOT NULL"
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
                  {native_column} text{native_constraint},
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


def verify_output(profile, *, logical_locators=False):
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
                        "file_name",("native_text" if _english_contract(profile) else "source_chunk_text"),"chunk_text","chunk_vector",
                        "embedding_processing_note","page_start","page_end",
                        "section_title","headers","document_metadata",
                        "chunk_metadata","content_types","metadata_evidence"}
            if not expected.issubset(columns):
                raise ValueError("Output table incompatible with materialization schema; use a new configured table for the English contract")
            if logical_locators:
                cur.execute("""SELECT column_name,is_nullable FROM information_schema.columns
                               WHERE table_schema=%s AND table_name=%s
                                 AND column_name IN ('page_start','page_end')""",
                            (profile["output"]["schema"],profile["output"]["table"]))
                if dict(cur.fetchall())!={"page_start":"YES","page_end":"YES"}:
                    raise ValueError("DOCX publication requires nullable physical page columns")
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


def _published_source_hash(doc_id, profile, names):
    """Return a reconciled published digest, or None for a new document."""
    from db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT source_hash,chunk_count FROM {names['status']}
                WHERE doc_id=%s AND profile_hash=%s""",
                (doc_id, profile["_fingerprint"]))
            state = cur.fetchone()
            if state is None:
                return None
            cur.execute(f"""SELECT count(*),count(*) FILTER (
                    WHERE profile_hash=%s) FROM {names['table']} WHERE doc_id=%s""",
                (profile["_fingerprint"], doc_id))
            total, matching = cur.fetchone()
            if total != state[1] or matching != state[1]:
                raise ValueError("Published chunk/status reconciliation failed")
            return state[0]
    finally:
        conn.close()


def _native_version(cur, doc_id, profile, *, lock=False):
    from page_stage import names as stage_names
    table = stage_names(profile)["docs"]
    lock_clause = " FOR SHARE" if lock else ""
    cur.execute(f"""SELECT source_hash,extraction_hash,completed_at FROM {table}
                    WHERE doc_id=%s{lock_clause}""", (doc_id,))
    return cur.fetchone()


def _validate_publication_row(row, doc_id, profile, index, *, pdf_identity):
    if not isinstance(row, (tuple, list)) or len(row) != 16:
        raise ValueError("Invalid materialized chunk row")
    if row[1] != doc_id or row[2] != profile["_fingerprint"] or row[3] != index:
        raise ValueError("Cannot publish missing, mixed or unordered document chunks")
    if pdf_identity:
        expected = hashlib.sha256(
            (profile["_fingerprint"] + "\x00" + doc_id + "\x00" + str(index)).encode("utf-8")
        ).hexdigest()
        if row[0] != expected:
            raise ValueError("Native chunk identity does not match publication order")
    if not _english_contract(profile):
        return
    meta = json.loads(row[13])
    trans = meta.get("translation", {})
    if (trans.get("target_language") != "en" or not isinstance(row[7], str)
            or not row[7].strip() or trans.get("status") not in
            ("ENGLISH_SOURCE", "MACHINE_TRANSLATION_UNVERIFIED", "MODEL_ENGLISH_UNVERIFIED")):
        raise ValueError("English contract requires validated English-target translation for every chunk")
    if meta.get("source_kind") == "visual_model":
        if row[6] is not None or trans["status"] != "MODEL_ENGLISH_UNVERIFIED":
            raise ValueError("Model observations must not be presented as native source text")
    elif (not isinstance(row[6], str) or not row[6].strip()
            or trans.get("source_text_sha256") != hashlib.sha256(row[6].encode()).hexdigest()):
        raise ValueError("Native text provenance mismatch")


def save_document_stream(doc_id, source_hash, profile, names, rows, *,
                         batch_size=200, pdf_identity=False, native_version=None):
    """Atomically replace one document while retaining only one row batch.

    PostgreSQL readers continue seeing the prior committed document until the
    final commit. Any generator, validation or insert failure rolls the delete
    and all inserted batches back together.
    """
    from db import get_conn
    from psycopg2.extras import execute_values
    if type(batch_size) is not int or not 1 <= batch_size <= 4096:
        raise ValueError("write batch_size must be 1..4096")
    iterator = iter(rows)
    try:
        first = next(iterator)
    except StopIteration:
        raise ValueError("Cannot publish missing document chunks") from None
    _validate_publication_row(first, doc_id, profile, 0, pdf_identity=pdf_identity)
    native_column = "native_text" if _english_contract(profile) else "source_chunk_text"
    insert_sql = f"""INSERT INTO {names['table']} (
        chunk_id,doc_id,profile_hash,chunk_index,file_path,file_name,
        {native_column},chunk_text,page_start,page_end,section_title,
        headers,document_metadata,chunk_metadata,content_types,metadata_evidence
        ) VALUES %s"""
    template = "(" + ",".join(["%s"] * 11 + ["%s::jsonb"] * 5) + ")"
    conn = get_conn()
    count = 0
    try:
        with conn.cursor() as cur:
            # Atomic replacement does not require retaining every prepared row:
            # an uncommitted delete is invisible to concurrent readers and is
            # restored automatically if a later row or batch fails.
            cur.execute(f"DELETE FROM {names['table']} WHERE doc_id=%s", (doc_id,))
            batch = []
            for row in chain((first,), iterator):
                _validate_publication_row(
                    row, doc_id, profile, count + len(batch), pdf_identity=pdf_identity,
                )
                batch.append(row)
                if len(batch) >= batch_size:
                    execute_values(cur, insert_sql, batch, template=template, page_size=batch_size)
                    count += len(batch)
                    batch.clear()
            if batch:
                execute_values(cur, insert_sql, batch, template=template, page_size=batch_size)
                count += len(batch)
            # Native publication updates this marker atomically with its page
            # swap. A shared row lock keeps the selected version current
            # through our commit without blocking native parsing or staging.
            if native_version is not None and _native_version(
                    cur, doc_id, profile, lock=True) != native_version:
                raise ValueError("Native version changed during materialization")
            cur.execute(f"""INSERT INTO {names['status']} (
                    doc_id,profile_hash,source_hash,chunk_count,updated_at)
                    VALUES (%s,%s,%s,%s,now())
                    ON CONFLICT (doc_id) DO UPDATE SET
                    profile_hash=EXCLUDED.profile_hash,
                    source_hash=EXCLUDED.source_hash,
                    chunk_count=EXCLUDED.chunk_count,updated_at=now()""",
                    (doc_id, profile["_fingerprint"], source_hash, count))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return count


def save_document(doc_id, source_hash, profile, names, rows):
    save_document_stream(doc_id, source_hash, profile, names, rows)


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


def append_enrichment_rows(struct,profile,rows,parts):
    """Same existing publication contract, independent source-linked text.

    OCR is transcription, LLM observations are explicitly unverified. No
    generated table cells or numeric interpretation. Native chunks unchanged.
    """
    from types import SimpleNamespace
    import tiktoken
    policy=profile["chunking"]
    tokenizer=policy.get("tokenizer","cl100k_base")
    encoder=tiktoken.get_encoding(tokenizer)
    count=lambda text:len(encoder.encode(text,disallowed_special=()))
    fingerprint=profile["_fingerprint"]
    if rows:
        meta=json.loads(rows[0][12]); evidence=json.loads(rows[0][15])
    else:
        from metadata_extractor import attach_metadata
        meta=attach_metadata(struct,[],profile["metadata"])
        evidence=getattr(struct,"metadata_evidence",{})
        for key,value in getattr(struct,"source_metadata",{}).items():
            if key in meta and str(meta[key])!=str(value):
                raise ValueError("Conflicting source metadata "+key)
            meta[key]=value
    sources=getattr(struct,"source_metadata",{})
    for part in parts:
        if part["kind"] not in ("ocr","visual_model") or not 1<=part["page"]<=struct.page_count:
            raise ValueError("Invalid enriched source")
        text=part["text"]
        for start,end,piece in _exact_slices(text,policy["max_tokens"],count):
            if not piece.strip():
                continue
            index=len(rows)
            cid=hashlib.sha256((fingerprint+"\x00"+struct.doc_id+"\x00"+str(index)).encode()).hexdigest()
            page=part["page"]
            cm={"source_format":"pdf","source_kind":part["kind"],"source_facts_verified":False,
                "section_id":hashlib.sha256((struct.doc_id+":"+str(page)).encode()).hexdigest(),
                "source_provenance":part["provenance"],"char_start":start,"char_end":end}
            if part["kind"]=="visual_model":
                # Label every split, including continuations. The input
                # model result is not a transcription of the original page.
                cm["source_text_status"]="MODEL_OBSERVATIONS_UNVERIFIED"
            else:
                cm["source_text_status"]="OCR_TRANSCRIPTION_UNVERIFIED"
            chunk=SimpleNamespace(chunk_text=piece,section_title="",file_name=struct.file_name,
                                  file_path=struct.file_path,document_metadata=meta,chunk_metadata=cm)
            composed=compose_embedding_text(chunk,profile["embedding"].get("input_fields",["chunk_text"]),
                       sources,max_tokens=profile["embedding"].get("input_max_tokens",6000),tokenizer=tokenizer)
            rows.append((cid,struct.doc_id,fingerprint,index,struct.file_path,struct.file_name,
                         piece,composed,page,page,"","[]",_safe_json(meta),_safe_json(cm),
                         _safe_json([part["kind"]]),_safe_json(evidence)))
    for i,row in enumerate(rows):
        cm=json.loads(row[13])
        cm["previous_chunk_id"]=rows[i-1][0] if i else None
        cm["next_chunk_id"]=rows[i+1][0] if i+1<len(rows) else None
        rows[i]=row[:13]+(_safe_json(cm),)+row[14:]
    if quality_summary(rows,policy)["structural_errors"]:
        raise ValueError("Enriched publication structural integrity failed")
    return rows


def _reject_translation_loss(doc_id,names):
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT 1 FROM {names['table']} WHERE doc_id=%s
                          AND chunk_metadata ? 'translation' LIMIT 1""",(doc_id,))
            if cur.fetchone():
                raise ValueError("Refusing to replace translated output without translation enabled")
    finally:
        conn.rollback()
        conn.close()


def run_one(doc_id, profile, names, *, enrichment=None, expected_source_hash=None,
            translator=None, source_batch_rows=128, write_batch_rows=200):
    from db import get_conn
    from profile_store import profile_document_lock
    from extracted_source import fingerprint_extracted_document, load_extracted_document
    if type(source_batch_rows) is not int or not 1 <= source_batch_rows <= 4096:
        raise ValueError("source_batch_rows must be 1..4096")
    if type(write_batch_rows) is not int or not 1 <= write_batch_rows <= 4096:
        raise ValueError("write_batch_rows must be 1..4096")
    published_doc_id = str(doc_id)

    def effective_digest(base):
        digest = base
        if enrichment is not None:
            digest = hashlib.sha256(
                (digest + "\x00enriched-v1\x00" + _safe_json(enrichment)).encode()
            ).hexdigest()
        if translator is not None:
            digest = hashlib.sha256(
                (digest + "\x00" + translator.identity).encode()
            ).hexdigest()
        return digest

    try:
        with profile_document_lock(published_doc_id, profile["_fingerprint"]) as acquired:
            if not acquired:
                return {"doc_id":published_doc_id,"status":"BUSY"}
            def read_native_version():
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        return _native_version(cur, published_doc_id, profile)
                finally:
                    conn.rollback()
                    conn.close()

            native_version = None
            if expected_source_hash is not None:
                native_version = read_native_version()
                if native_version is None or native_version[0] != expected_source_hash:
                    raise ValueError("Published native version does not match the pinned source")
            if _english_contract(profile) and translator is None:
                raise PermissionError("English retrieval requires translation")
            if enrichment is None:
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"""SELECT 1 FROM {names['table']} WHERE doc_id=%s
                              AND chunk_metadata->>'source_kind' IN ('ocr','visual_model') LIMIT 1""",
                            (published_doc_id,))
                        if cur.fetchone():
                            raise ValueError("Refusing to replace enriched output with native-only chunks")
                finally:
                    conn.rollback()
                    conn.close()
            if translator is None:
                _reject_translation_loss(published_doc_id,names)

            # Replays first compare the exact staged-source digest without
            # rebuilding thousands of page and element objects. New or changed
            # documents still receive the complete validating load below.
            published_digest = _published_source_hash(published_doc_id, profile, names)
            probed_digest = None
            if published_digest is not None:
                probed_digest = fingerprint_extracted_document(
                    profile, doc_id, batch_size=source_batch_rows,
                )
                if published_digest == effective_digest(probed_digest):
                    # The bounded scan can take seconds for a giant document.
                    # Recheck the capture selector before accepting the replay
                    # so a concurrently published native version is not
                    # reported as current against the caller's pinned source.
                    if native_version is not None and read_native_version() != native_version:
                        raise ValueError("Native version changed during materialization")
                    return {"doc_id":published_doc_id,"status":"SKIPPED"}

            struct, base_digest = load_extracted_document(
                profile, doc_id, batch_size=source_batch_rows,
            )
            if probed_digest is not None and probed_digest != base_digest:
                raise ValueError("Extracted source changed during materialization")
            digest = effective_digest(base_digest)

            if enrichment is None and translator is None:
                rows = iter_prepared_document_rows(struct, profile, release_source=True)
                count = save_document_stream(
                    published_doc_id, digest, profile, names, rows,
                    batch_size=write_batch_rows, pdf_identity=True,
                    native_version=native_version,
                )
                return {"doc_id":published_doc_id,"status":"SUCCESS","chunks":count}
            try:
                rows=prepare_document(struct,profile)
            except ValueError as exc:
                if not enrichment or str(exc)!="Document yielded no chunks; source remains unchanged":
                    raise
                rows=[]
            if enrichment is not None:
                rows=append_enrichment_rows(struct,profile,rows,enrichment)
            if translator is not None:
                rows=translator.apply_rows(rows,profile,getattr(struct,"source_metadata",{}))
            count = save_document_stream(
                published_doc_id, digest, profile, names, rows,
                batch_size=write_batch_rows, native_version=native_version,
            )
            return {"doc_id":published_doc_id,"status":"SUCCESS","chunks":count}
    except Exception as exc:
        # A broken document cannot poison other documents. A DB outage must
        # still surface: never claim durable error logging when it failed.
        record_failure(published_doc_id,profile,names,exc)
        LOG.exception("Document %s materialization failed (stored in failure_events)",doc_id)
        return {"doc_id":published_doc_id,"status":"FAILED","error":type(exc).__name__}


def run(profile, *, max_docs, workers, source_batch_rows=128, write_batch_rows=200):
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
            pending[pool.submit(
                run_one, doc, profile, names,
                source_batch_rows=source_batch_rows,
                write_batch_rows=write_batch_rows,
            )]=doc
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
    parser.add_argument("command",choices=("validate","init","run","preview","docx-preview","docx-run"))
    parser.add_argument("--profile",required=True)
    parser.add_argument("--apply",action="store_true",
                        help="Explicit DDL approval for a new output table")
    parser.add_argument("--max-docs",type=int,default=10)
    parser.add_argument("--doc-workers",type=int,default=2)
    parser.add_argument("--source-batch-rows",type=int,default=128)
    parser.add_argument("--write-batch-rows",type=int,default=200)
    parser.add_argument("--doc-id",help="Required for read-only document preview")
    parser.add_argument("--evidence-root")
    parser.add_argument("--capture-id")
    parser.add_argument("--docx-profile")
    parser.add_argument("--file-name")
    parser.add_argument("--allow-logical-locators",action="store_true")
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
    elif args.command in ("docx-preview","docx-run"):
        if not all((args.evidence_root,args.capture_id,args.docx_profile,args.doc_id,args.file_name)):
            parser.error("DOCX commands require evidence-root, capture-id, docx-profile, doc-id and file-name")
        from docx_evidence import _load_profile
        cfg=_load_profile(args.docx_profile)
        kw=dict(config=cfg,doc_id=args.doc_id,file_name=args.file_name)
        if args.command=="docx-preview":
            _,info=prepare_docx_document(args.evidence_root,args.capture_id,profile,**kw)
        else:
            if not args.apply or not args.allow_logical_locators:
                parser.error("docx-run requires --apply and --allow-logical-locators")
            info=run_docx(args.evidence_root,args.capture_id,profile,
                          allow_logical_locators=True,**kw)
    elif args.command=="preview":
        if not args.doc_id:
            parser.error("--doc-id required for preview")
        from extracted_source import load_extracted_document
        struct,source_hash=load_extracted_document(
            profile,args.doc_id,batch_size=args.source_batch_rows,
        )
        rows=prepare_document(struct,profile)
        summary=quality_summary(rows,profile["chunking"])
        info={"document_id":str(args.doc_id),"source_hash":source_hash,
              "prepared_chunks":len(rows),
              "first_chunk_input_characters":len(rows[0][7]),
              "first_chunk_metadata_fields":list(json.loads(rows[0][12])),
              "first_chunk_document_metadata_fields":list(json.loads(rows[0][12])),
              "first_chunk_structural_metadata_fields":list(json.loads(rows[0][13])),
              "quality":summary,
              "note":"Preview only; no document text or private data printed"}
        if summary["structural_errors"]:
            raise ValueError("Chunk preview structural integrity failed")
    else:
        info=run(
            profile,max_docs=args.max_docs,workers=args.doc_workers,
            source_batch_rows=args.source_batch_rows,
            write_batch_rows=args.write_batch_rows,
        )
    print(json.dumps(info,indent=2))
    return 0 if not info.get("FAILED") else 1


if __name__=="__main__":
    raise SystemExit(main())
