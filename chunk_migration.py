"""Migrate existing chunk text into isolated profiled output without reopening PDFs.

Source rows are immutable. Provenance reflects available source ranges, not invented
precise positions. Documents are bounded and deterministic; failures leave prior
profile output intact and are eligible for retry.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace

from chunker import Chunk, _make_chunk_id
from config import EMBEDDING_DIM
from db import get_conn
from embedder import embed_chunks, embedding_stats
from logger import RunSummary, get_logger
from metadata_extractor import attach_metadata
from pdf_processor import BBox, ElementType, PageContent, PageElement
from profile_store import (
    _safe_identifier, init_profile_store, profile_document_lock,
    profile_is_current, save_profile_result,
)

log = get_logger("epod.chunk_migration")

_FIELDS = {
    "doc_id_column": "doc_id",
    "text_column": "chunk_text",
    "chunk_id_column": "chunk_id",
    "chunk_index_column": "chunk_index",
    "file_name_column": "file_name",
    "file_path_column": "file_path",
    "section_title_column": "section_title",
    "page_start_column": "page_start",
    "page_end_column": "page_end",
    "content_types_column": "content_types",
}


def source_mapping(profile: dict) -> dict:
    source = profile.get("source_chunks") or {}
    for field in ("schema", "table"):
        if not source.get(field):
            raise ValueError("source_chunks." + field + " is required")
    mapped = {"schema": source["schema"], "table": source["table"]}
    mapped.update({k: source.get(k, default) for k, default in _FIELDS.items()})
    for name in mapped.values():
        _safe_identifier(name)
    return mapped


def source_table(mapping: dict) -> str:
    return _safe_identifier(mapping["schema"]) + "." + _safe_identifier(mapping["table"])


def validate_source_schema(mapping: dict) -> None:
    """Fail fast on incorrect mappings, missing columns and permissions."""
    table = source_table(mapping)
    columns = ", ".join(_safe_identifier(mapping[key]) for key in _FIELDS)
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT {columns} FROM {table} LIMIT 0")
    finally:
        conn.close()


def iter_document_ids(profile: dict, limit: int | None = None, batch_size: int = 500):
    """Keyset scan. Requires an indexed, non-null doc_id for large migrations."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    mapping = source_mapping(profile)
    table = source_table(mapping)
    doc = _safe_identifier(mapping["doc_id_column"])
    last = None
    produced = 0
    conn = get_conn()
    try:
        while limit is None or produced < limit:
            count = min(batch_size, limit - produced) if limit else batch_size
            condition = f" AND {doc} > %s" if last is not None else ""
            params = (last, count) if last is not None else (count,)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT {doc} FROM {table} WHERE {doc} IS NOT NULL"
                    f"{condition} ORDER BY {doc} LIMIT %s", params
                )
                keys = [r[0] for r in cur.fetchall()]
            if not keys:
                break
            for key in keys:
                yield key
                produced += 1
            last = keys[-1]
    finally:
        conn.close()


def load_source_document(profile: dict, document_key, *,
                         max_rows: int = 10000, max_bytes: int = 32 * 1024 * 1024):
    """Read one document, enforcing memory caps before collecting all rows."""
    mapping = source_mapping(profile)
    table = source_table(mapping)
    fields = list(_FIELDS)
    cols = ", ".join(_safe_identifier(mapping[key]) for key in fields)
    doc = _safe_identifier(mapping["doc_id_column"])
    idx = _safe_identifier(mapping["chunk_index_column"])
    cid = _safe_identifier(mapping["chunk_id_column"])
    conn = get_conn()
    rows = []
    size = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM {table} WHERE {doc}=%s "
                f"ORDER BY {idx} NULLS LAST, {cid}", (document_key,)
            )
            while True:
                batch = cur.fetchmany(256)
                if not batch:
                    break
                for values in batch:
                    if len(rows) >= max_rows:
                        raise ValueError("Document exceeds max_source_rows; increase limit deliberately")
                    row = dict(zip(fields, values))
                    chunk_text = row["text_column"]
                    if chunk_text is not None and not isinstance(chunk_text, str):
                        raise ValueError("Source chunk_text must be text")
                    size += len((chunk_text or "").encode("utf-8"))
                    if size > max_bytes:
                        raise ValueError("Document exceeds max_source_bytes; increase limit deliberately")
                    rows.append(row)
    finally:
        conn.close()
    if not rows:
        raise ValueError("No source rows")
    ids = [row["chunk_id_column"] for row in rows]
    if any(key is None for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("chunk_id must be non-null and unique within each document")
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return rows, digest.hexdigest()


def _text_pieces(text: str, budget: int, encoder) -> list[str]:
    """Prefer paragraph/sentence boundaries; split token-wise as a last resort."""
    def count(value):
        return len(encoder.encode(value, disallowed_special=()))
    units = re.split(r"(?<=\n)\n+|(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    pieces = []
    pending = ""
    for unit in units:
        if not unit:
            continue
        candidate = (pending + "\n" + unit).strip() if pending else unit.strip()
        if count(candidate) <= budget:
            pending = candidate
            continue
        if pending:
            pieces.append(pending)
            pending = ""
        if count(unit) <= budget:
            pending = unit.strip()
        else:
            # Split on Python character boundaries, not token IDs: slicing
            # encoded tokens can corrupt multi-byte Unicode characters.
            offset = 0
            while offset < len(unit):
                lo, hi, best = 1, len(unit) - offset, 0
                while lo <= hi:
                    mid = (lo + hi) // 2
                    segment = unit[offset:offset + mid]
                    if count(segment) <= budget:
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best < 1:
                    raise ValueError("Cannot encode a single character within budget")
                pieces.append(unit[offset:offset + best])
                offset += best
    if pending:
        pieces.append(pending)
    return [part for part in pieces if part.strip()]


def rebuild_chunks(rows: list[dict], profile: dict) -> tuple[object, list[Chunk]]:
    """Rechunk only available source text, retaining conservative page spans.

    Does not claim to reconstruct the original TOC or eliminate overlapping
    text that may already exist in the source.
    """
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError("Install tiktoken for source-chunk migration") from exc
    policy = profile["chunking"]
    encoder = tiktoken.get_encoding(policy.get("tokenizer", "cl100k_base"))
    maximum = policy["max_tokens"]
    minimum = policy["min_tokens"]
    overlap = policy["overlap_tokens"]
    preserve = policy.get("preserve_headers", True)
    doc_id = str(rows[0]["doc_id_column"])
    filepath = str(rows[0].get("file_path_column") or f"db://{doc_id}")
    filename = str(rows[0].get("file_name_column") or "unknown.pdf")
    chunks = []
    pages = []
    pending = []
    # item = (text, page_start, page_end, title, content_types)
    def count(value):
        return len(encoder.encode(value, disallowed_special=()))

    def render(parts):
        body = "\n".join(p[0] for p in parts)
        title = parts[0][3] if preserve else ""
        return title + "\n\n" + body if title and not body.startswith(title) else body

    def flush():
        nonlocal pending
        if not pending:
            return
        value = render(pending)
        if count(value) > maximum:
            raise ValueError("Rechunked text exceeded maximum tokens")
        first, last = pending[0], pending[-1]
        title = first[3] or ""
        ch = Chunk(
            chunk_id=_make_chunk_id(doc_id, len(chunks)), doc_id=doc_id,
            file_name=filename, file_path=filepath,
            chunk_index=len(chunks), chunk_total=0, chunk_level="section" if title else "page",
            section_title=title,
            page_start=min(p[1] for p in pending if p[1] is not None) if any(p[1] is not None for p in pending) else None,
            page_end=max(p[2] for p in pending if p[2] is not None) if any(p[2] is not None for p in pending) else None,
            content_types=sorted({t for p in pending for t in p[4]}),
            chunk_text=value,
        )
        inherited_headers = [p[3] for p in pending if p[3]]
        inline_headers = []
        for source_part in pending:
            for line in source_part[0].splitlines():
                clean = line.strip()
                # Capture numbered subsection headings, but do not infer a TOC.
                if re.fullmatch(r"\d+(?:\.\d+){1,6}\s+[A-Za-z][^\n]{2,100}", clean):
                    inline_headers.append(clean)
        ch.headers = list(dict.fromkeys(inherited_headers + inline_headers))
        ch.source_chunk_ids = list(dict.fromkeys(p[5] for p in pending))
        chunks.append(ch)
        pending = []

    for row in rows:
        text = (row.get("text_column") or "").strip()
        if not text:
            continue
        title = str(row.get("section_title_column") or "").strip()
        start = row.get("page_start_column")
        end = row.get("page_end_column")
        if start is not None and end is not None and end < start:
            raise ValueError("Source has reversed page range")
        kind = row.get("content_types_column") or ["text"]
        if not isinstance(kind, (list, tuple)):
            raise ValueError("content_types must be a list")
        if start is not None:
            pages.append(PageContent(
                page_number=int(start), width=0, height=0,
                raw_text=text, elements=[PageElement(ElementType.TEXT, BBox(0, 0, 0, 0), text=text)],
            ))
        title_tokens = count(title + "\n\n") if preserve and title else 0
        budget = maximum - title_tokens - 4
        if budget < 8:
            raise ValueError("Title cannot fit within token budget")
        pieces = _text_pieces(text, budget, encoder)
        for i, piece in enumerate(pieces):
            item = (piece, start, end, title, list(kind), row["chunk_id_column"])
            if pending:
                boundary = title != pending[-1][3]
                overflow = count(render(pending + [item])) > maximum
                if overflow or (boundary and count(render(pending)) >= minimum):
                    previous = pending[-1]
                    flush()
                    if overflow and not boundary and overlap:
                        # Select overlap from the original Unicode text.
                        # Decoding a sliced token tail can insert replacement
                        # characters when it starts inside a UTF-8 sequence.
                        tail = previous[0]
                        while len(tail) > 1 and count(tail) > overlap:
                            tail = tail[1:]
                        if count(tail) > overlap:
                            tail = ""
                        carry = (tail, previous[1], previous[2],
                                 previous[3], previous[4], previous[5])
                        if tail and count(render([carry, item])) <= maximum:
                            pending.append(carry)
            pending.append(item)
            if count(render(pending)) > maximum:
                raise ValueError("Unexpected oversized output chunk")
    flush()
    for ch in chunks:
        ch.chunk_total = len(chunks)
    if not chunks:
        raise ValueError("Source document has no nonempty text")
    # The source may have multi-page chunks. Page-number metadata is approximate.
    struct = SimpleNamespace(doc_id=doc_id, pages=pages, file_name=filename, file_path=filepath)
    attach_metadata(struct, chunks, profile["metadata"])
    for ch in chunks:
        ch.chunk_metadata["source_chunk_ids"] = ch.source_chunk_ids
        ch.chunk_metadata["page_range_precision"] = "source_chunk_range"
    return struct, chunks


def run_migration(profile: dict, *, dry_run: bool = False, max_docs: int | None = None,
                  workers: int = 2, max_source_rows: int = 10000,
                  max_source_bytes: int = 32 * 1024 * 1024, force: bool = False) -> RunSummary:
    """Bounded in-flight processing. Profile results are committed atomically."""
    if workers < 1 or workers > 64:
        raise ValueError("workers must be between 1 and 64")
    if profile["chunking"]["strategy"] != "toc_then_semantic":
        raise ValueError("Migration requires chunking.strategy=toc_then_semantic")
    if max_source_rows < 1 or max_source_bytes < 1:
        raise ValueError("Document caps must be positive")
    if profile["embedding"].get("dimensions", EMBEDDING_DIM) != EMBEDDING_DIM:
        raise ValueError("Profile output storage requires configured 1536 dimensions")
    source_mapping(profile)
    validate_source_schema(source_mapping(profile))
    summary = RunSummary(max_rows=1000)
    if not dry_run:
        init_profile_store()

    def process(key):
        start = time.monotonic()
        try:
            with profile_document_lock(str(key), profile["_fingerprint"]) as acquired:
                if not acquired:
                    return "SKIPPED", 0, "profile_busy", time.monotonic() - start
                rows, source_hash = load_source_document(
                    profile, key, max_rows=max_source_rows, max_bytes=max_source_bytes
                )
                doc_id = str(key)
                if not force and profile_is_current(doc_id, source_hash, profile["_fingerprint"]):
                    return "SKIPPED", 0, "profile_current", time.monotonic() - start
                _, chunks = rebuild_chunks(rows, profile)
                if profile["embedding"]["enabled"]:
                    options = {
                        k: profile["embedding"][k]
                        for k in ("dimensions", "deployment", "model")
                        if k in profile["embedding"]
                    }
                    chunks = embed_chunks(
                        chunks, batch_size=profile["embedding"]["batch_size"], **options
                    )
                    stats = embedding_stats(chunks)
                    if stats["with_vector"] != len(chunks):
                        raise RuntimeError("Partial embedding result; output left unchanged")
                save_profile_result(doc_id, source_hash, profile["_fingerprint"], chunks)
                return "SUCCESS", len(chunks), None, time.monotonic() - start
        except Exception as exc:
            log.exception("Migration failed for document %s", key)
            return "ERROR", 0, str(exc), time.monotonic() - start

    if dry_run:
        for key in iter_document_ids(profile, limit=max_docs):
            summary.add("WOULD_PROCESS", str(key))
        summary.print()
        return summary
    pending = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        def drain(done):
            for future in done:
                key = pending.pop(future)
                status, count, err, elapsed = future.result()
                summary.add(status, str(key), error=err, chunks=count, elapsed=elapsed)
                if summary._total % 100 == 0:
                    log.info("Migration progress processed=%s counts=%s",
                             summary._total, summary.counts())
        for key in iter_document_ids(profile, limit=max_docs):
            pending[pool.submit(process, key)] = key
            if len(pending) >= workers * 2:
                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                drain(done)
        while pending:
            done, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            drain(done)
    summary.print()
    return summary
