# main.py  —  EPOD Ingestion Pipeline  (High-Throughput Edition)
from __future__ import annotations

import argparse
import concurrent.futures
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import psycopg2.pool

# ── Pipeline imports ──────────────────────────────────────────────────────────
from config import DOCS_ROOT, PAGE_WORKERS, EMBEDDING_BATCH_SIZE, SOURCE_TYPE, ENABLE_EMBEDDINGS
from logger import get_logger, DocLogger, RunSummary
from crawler import Crawler, CrawlResult
from pdf_processor import process_pdf, DocumentStructure, PageContent, ElementType
from vision_ocr import ocr_page_elements
from table_extractor import extract_tables_for_page, inject_table_text_into_elements
from chunker import chunk_document, Chunk
from embedder import embed_chunks, embedding_stats
from db import (
    init_db,
    reset_stale_running,
    hash_file_path,
    hash_file_bytes,
    upsert_ingestion_log,
    delete_chunks_for_doc,
    insert_chunks_batch,
    get_pipeline_stats,
    get_failed_docs,
    get_ingestion_record,
    should_ingest,
)

logger  = get_logger("epod.main")
summary = RunSummary()

# ─────────────────────────────────────────────────────────────────────────────
# Global API rate-limiting semaphores
# Shared across ALL document worker threads — prevents API 429 storms
# ─────────────────────────────────────────────────────────────────────────────

# Google Cloud Vision — safe up to ~20 concurrent, keep at 16
_SEM_VISION    = threading.BoundedSemaphore(16)

# Azure OpenAI GPT-4o (table/chart summaries) — heavier, keep at 6
_SEM_GPT4O     = threading.BoundedSemaphore(6)

# Azure OpenAI Embeddings — text-embedding-3-small, batch calls, keep at 12
_SEM_EMBED     = threading.BoundedSemaphore(12)

# pdfplumber is NOT thread-safe per file handle — use one lock per file path
# (different files are fine in parallel, same file needs lock)
_pdfplumber_locks: dict[str, threading.Lock] = {}
_pdfplumber_locks_guard = threading.Lock()

def _get_pdfplumber_lock(file_path: str) -> threading.Lock:
    with _pdfplumber_locks_guard:
        if file_path not in _pdfplumber_locks:
            _pdfplumber_locks[file_path] = threading.Lock()
        return _pdfplumber_locks[file_path]


# ─────────────────────────────────────────────────────────────────────────────
# Crawler factory — returns NAS or S3 crawler based on source_type
# ─────────────────────────────────────────────────────────────────────────────

def _make_crawler(
    source_type:    str,
    root:           Optional[Path],
    force_reingest: bool,
    max_files:      Optional[int],
) -> Crawler:
    if source_type == "s3":
        from s3_crawler import S3Crawler
        logger.info("Source: Amazon S3")
        return S3Crawler(force_reingest=force_reingest, max_files=max_files)
    else:
        logger.info(f"Source: NAS/local  ({root or DOCS_ROOT})")
        return Crawler(root=root, force_reingest=force_reingest, max_files=max_files)


# ─────────────────────────────────────────────────────────────────────────────
# Shared PostgreSQL connection pool
# ThreadedConnectionPool: safe across threads, avoids per-doc reconnect cost
# ─────────────────────────────────────────────────────────────────────────────

_db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_db_pool_lock = threading.Lock()


def _init_pool(doc_workers: int) -> None:
    """Create the shared connection pool. Call once before spawning workers."""
    global _db_pool
    from configparser import ConfigParser
    cfg = ConfigParser()
    cfg.read("config.ini")
    c = cfg["POSTGRES"]

    min_conn = 2
    max_conn = doc_workers + 4   # headroom above worker count

    _db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=min_conn,
        maxconn=max_conn,
        host=c["host"],
        port=c["port"],
        dbname=c["database"],
        user=c["user"],
        password=c["password"],
    )
    logger.info(f"DB pool initialised: min={min_conn} max={max_conn} connections")


def _pool_conn():
    """Borrow a connection from the pool. Caller must call _pool_put() when done."""
    return _db_pool.getconn()


def _pool_put(conn) -> None:
    """Return a borrowed connection to the pool."""
    _db_pool.putconn(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Progress tracker — live throughput + ETA for 2M docs
# ─────────────────────────────────────────────────────────────────────────────

class ProgressTracker:
    """Thread-safe progress tracker. Prints a status line every N docs."""

    def __init__(self, total: Optional[int] = None, report_every: int = 10):
        self._lock        = threading.Lock()
        self._done        = 0
        self._success     = 0
        self._error       = 0
        self._total       = total
        self._report_every = report_every
        self._t_start     = time.time()

    def record(self, status: str) -> None:
        with self._lock:
            self._done += 1
            if status == "SUCCESS":
                self._success += 1
            else:
                self._error += 1
            if self._done % self._report_every == 0:
                self._print()

    def _print(self) -> None:
        elapsed  = time.time() - self._t_start
        rate     = self._done / elapsed if elapsed > 0 else 0
        eta_s    = (self._total - self._done) / rate if (self._total and rate > 0) else None
        eta_str  = _fmt_duration(eta_s) if eta_s else "?"
        pct_str  = f"{self._done/self._total*100:.1f}%" if self._total else ""

        logger.info(
            f"── Progress: {self._done:,}/{self._total or '?':,} {pct_str}  "
            f"✅{self._success:,} ❌{self._error:,}  "
            f"rate={rate:.2f} doc/s  ETA={eta_str}"
        )

    def final(self) -> None:
        elapsed = time.time() - self._t_start
        rate    = self._done / elapsed if elapsed > 0 else 0
        logger.info(
            f"── DONE: {self._done:,} docs in {_fmt_duration(elapsed)}  "
            f"✅{self._success:,} ❌{self._error:,}  "
            f"avg={rate:.2f} doc/s"
        )


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}m"


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_sigint(sig, frame):
    global _shutdown_requested
    if not _shutdown_requested:
        logger.warning("Ctrl-C received — finishing in-flight documents then stopping…")
        _shutdown_requested = True
    else:
        logger.warning("Second Ctrl-C — force exit.")
        sys.exit(1)


signal.signal(signal.SIGINT, _handle_sigint)


# ─────────────────────────────────────────────────────────────────────────────
# Step workers  (semaphore-guarded)
# ─────────────────────────────────────────────────────────────────────────────

def _ocr_page(args: tuple) -> PageContent:
    """
    Per-page OCR worker — runs inside a per-doc ThreadPoolExecutor.
    Acquires Vision semaphore before each API call so global concurrency
    across all doc threads stays within the safe limit.
    """
    page, file_path, file_name = args

    if page.is_blank or page.error:
        return page

    needs_full_ocr = page.needs_ocr
    has_images     = any(
        e.element_type in (ElementType.IMAGE, ElementType.CHART)
        for e in page.elements
    )

    if not needs_full_ocr and not has_images:
        return page

    with _SEM_VISION:
        ocr_page_elements(page.elements, file_name, page.page_number)

    if needs_full_ocr:
        page.raw_text = "\n".join(
            e.text for e in page.elements if e.text
        ).strip()
        page.needs_ocr = False

    return page


def _extract_tables_for_page_guarded(
    file_path: str,
    page_number: int,
    context_text: str,
    summarise: bool = True,
) -> list:
    """
    Table extraction with:
      - per-file pdfplumber lock   (thread safety)
      - GPT-4o semaphore           (rate limit on summarise=True calls)
    """
    lock = _get_pdfplumber_lock(file_path)
    with lock:
        results = extract_tables_for_page(
            file_path, page_number,
            context_text=context_text,
            summarise=False,          # extract first without the API call
        )

    if summarise and results:
        # Summarise outside the pdfplumber lock — GPT-4o call is pure network
        with _SEM_GPT4O:
            results = extract_tables_for_page(
                file_path, page_number,
                context_text=context_text,
                summarise=True,
            )
    return results


def _embed_chunks_guarded(chunks: list, log_every_n: int = 20) -> list:
    """Embedding with semaphore so concurrent doc threads don't flood the API."""
    with _SEM_EMBED:
        return embed_chunks(chunks, log_every_n=log_every_n)


# ─────────────────────────────────────────────────────────────────────────────
# Core: process ONE document  (called from each doc-worker thread)
# ─────────────────────────────────────────────────────────────────────────────

def process_document(
    crawl_result:      CrawlResult,
    workers:           int  = PAGE_WORKERS,
    enable_embeddings: bool = True,
) -> dict:
    """
    Full pipeline for one document. Thread-safe — can run concurrently with
    other calls to process_document in separate threads.

    Parameters
    ----------
    crawl_result      : from Crawler or S3Crawler
    workers           : OCR page threads
    enable_embeddings : when False, skip Azure embedding; vectors stored as NULL

    Returns dict: status, chunks, pages, elapsed, error
    """
    t_start   = time.time()
    file_path = crawl_result.file_path          # canonical DB path (may be s3://)
    file_name = crawl_result.file_name
    doc_id    = crawl_result.doc_id
    dlog      = DocLogger(file_name, doc_id)

    # local_path is the on-disk path for PDF processing (temp file for S3)
    local_file = crawl_result.local_path or crawl_result.file_path

    dlog.info(f"── Processing: {file_name}  ({crawl_result.file_size/1024/1024:.1f} MB)")

    try:
        # ── 1. Delete stale chunks if re-ingesting ───────────────────────────
        existing = get_ingestion_record(file_path)
        if existing and existing["status"] == "SUCCESS":
            dlog.info("Modified doc — deleting existing chunks")
            delete_chunks_for_doc(doc_id)

        # ── 2. PDF layout analysis ───────────────────────────────────────────
        dlog.info("Step 1/5: PDF layout analysis")
        struct = process_pdf(local_file, doc_id, file_name)
        if struct.error:
            raise RuntimeError(f"PDF processing failed: {struct.error}")
        struct.file_path = file_path  # replace temp path with canonical S3/NAS URI

        dlog.info(
            f"  Pages: {struct.page_count}  "
            f"TOC entries: {len(struct.toc)}  "
            f"Has TOC: {struct.has_toc}"
        )

        # ── 3. Parallel OCR  (per-page threads, Vision semaphore) ───────────
        dlog.info(f"Step 2/5: OCR + table extraction ({workers} page workers)")

        pages_needing_work = [
            p for p in struct.pages
            if not p.is_blank and not p.error
        ]

        ocr_args = [(page, local_file, file_name) for page in pages_needing_work]

        ocr_results: dict[int, PageContent] = {}
        if ocr_args:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_ocr_page, arg): arg[0].page_number
                    for arg in ocr_args
                }
                for fut in concurrent.futures.as_completed(futures):
                    pn = futures[fut]
                    try:
                        ocr_results[pn] = fut.result()
                    except Exception as e:
                        dlog.error(f"OCR worker failed page {pn}: {e}")

        # Merge OCR back
        for page in struct.pages:
            if page.page_number in ocr_results:
                updated = ocr_results[page.page_number]
                page.elements  = updated.elements
                page.raw_text  = updated.raw_text
                page.needs_ocr = False

        # ── 4. Table extraction  (pdfplumber lock + GPT-4o semaphore) ───────
        pages_with_tables = [
            p for p in pages_needing_work if p.has_tables
        ]
        page_text_map = {p.page_number: p.raw_text or "" for p in pages_needing_work}

        dlog.info(
            f"  OCR pages: {len(pages_needing_work)}  "
            f"Table pages: {len(pages_with_tables)}"
        )

        # Table extraction: run pages in a small thread pool.
        # Each call uses the pdfplumber file-lock + GPT-4o semaphore inside.
        def _table_worker(pg):
            return pg.page_number, _extract_tables_for_page_guarded(
                local_file,
                pg.page_number,
                context_text=page_text_map.get(pg.page_number, ""),
                summarise=True,     # ← summaries always ON
            )

        table_workers = min(workers, len(pages_with_tables)) if pages_with_tables else 1
        if pages_with_tables:
            with concurrent.futures.ThreadPoolExecutor(max_workers=table_workers) as pool:
                table_futures = {pool.submit(_table_worker, pg): pg for pg in pages_with_tables}
                for fut in concurrent.futures.as_completed(table_futures):
                    try:
                        page_number, table_results = fut.result()
                        if table_results:
                            for page in struct.pages:
                                if page.page_number == page_number:
                                    inject_table_text_into_elements(
                                        page.elements, table_results
                                    )
                                    break
                    except Exception as e:
                        dlog.error(f"Table extraction failed: {e}")

        # ── 5. Chunking ──────────────────────────────────────────────────────
        dlog.info("Step 3/5: Chunking")
        chunks = chunk_document(struct)
        if not chunks:
            raise RuntimeError("No chunks produced — document may be empty or corrupt")

        dlog.info(
            f"  Chunks: {len(chunks)}  "
            f"Levels: {set(c.chunk_level for c in chunks)}"
        )

        # ── 6. Embedding  (embedding semaphore) ─────────────────────────────
        if enable_embeddings:
            dlog.info("Step 4/5: Embedding")
            chunks = _embed_chunks_guarded(chunks, log_every_n=10)

            stats = embedding_stats(chunks)
            dlog.info(
                f"  Embedded: {stats['with_vector']}/{stats['total_chunks']}  "
                f"Avg tokens: {stats['avg_tokens']}  "
                f"Failed: {stats['zero_vector']}"
            )

            if stats["with_vector"] == 0:
                raise RuntimeError("All embeddings failed — no vectors to store")
        else:
            dlog.info("Step 4/5: Embedding SKIPPED (enable_embeddings=false)")
            # chunk_vector stays as [] (default); db.insert_chunks_batch stores NULL

        # ── 7. DB write ──────────────────────────────────────────────────────
        dlog.info("Step 5/5: Saving to database")
        chunk_dicts = [c.to_db_dict() for c in chunks]
        insert_chunks_batch(chunk_dicts)

        upsert_ingestion_log(
            file_path=file_path,
            file_name=file_name,
            file_hash=crawl_result.file_hash,
            file_size=crawl_result.file_size,
            last_modified=crawl_result.last_modified,
            status="SUCCESS",
            chunk_count=len(chunks),
            pages_total=struct.page_count,
        )

        elapsed = time.time() - t_start
        dlog.info(
            f"✅ SUCCESS — {len(chunks)} chunks  "
            f"{struct.page_count} pages  {elapsed:.1f}s"
        )
        return {
            "status":  "SUCCESS",
            "chunks":  len(chunks),
            "pages":   struct.page_count,
            "elapsed": elapsed,
            "error":   "",
        }

    except Exception as e:
        elapsed   = time.time() - t_start
        error_msg = str(e)
        dlog.error(f"❌ FAILED: {error_msg}", exc_info=True)
        try:
            upsert_ingestion_log(
                file_path=file_path,
                file_name=file_name,
                file_hash=crawl_result.file_hash,
                file_size=crawl_result.file_size,
                last_modified=crawl_result.last_modified,
                status="ERROR",
                error_message=error_msg[:2000],
                increment_retry=True,
            )
        except Exception as db_e:
            dlog.error(f"Could not write ERROR to DB: {db_e}")
        return {
            "status":  "ERROR",
            "chunks":  0,
            "pages":   0,
            "elapsed": elapsed,
            "error":   error_msg,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Thread wrapper — catches all exceptions, returns safely
# ─────────────────────────────────────────────────────────────────────────────

def _process_doc_thread(args: tuple) -> tuple[str, dict]:
    """
    Wrapper for ThreadPoolExecutor.  Returns (file_name, result_dict).

    S3 download happens HERE, inside the worker thread, so that:
      - At most doc_workers temp files exist on disk simultaneously.
      - Each thread uses its own boto3 client (thread-local in s3_crawler).
      - The crawl phase stays fast (list-only, no downloads).
    """
    crawl_result, workers, enable_embeddings = args
    downloaded_temp = None   # temp file we created in this thread (S3 only)

    try:
        # ── S3: download the file now, just before processing ────────────────
        if (
            crawl_result.file_path.startswith("s3://")
            and crawl_result.local_path is None
        ):
            from s3_crawler import download_s3_uri
            downloaded_temp = download_s3_uri(crawl_result.file_path)
            if downloaded_temp is None:
                # Mark failed in DB so the retry counter advances
                try:
                    upsert_ingestion_log(
                        file_path=crawl_result.file_path,
                        file_name=crawl_result.file_name,
                        file_hash=crawl_result.file_hash,
                        file_size=crawl_result.file_size,
                        last_modified=crawl_result.last_modified,
                        status="ERROR",
                        error_message="S3 download failed",
                        increment_retry=True,
                    )
                except Exception:
                    pass
                return crawl_result.file_name, {
                    "status": "ERROR", "chunks": 0, "pages": 0,
                    "elapsed": 0.0, "error": "S3 download failed",
                }
            crawl_result.local_path = downloaded_temp

        result = process_document(
            crawl_result,
            workers=workers,
            enable_embeddings=enable_embeddings,
        )
    except Exception as e:
        result = {
            "status":  "ERROR",
            "chunks":  0,
            "pages":   0,
            "elapsed": 0.0,
            "error":   str(e),
        }
    finally:
        # Delete the S3 temp file (either pre-existing local_path or one we just downloaded)
        lp = downloaded_temp or crawl_result.local_path
        if lp and os.path.exists(lp):
            try:
                os.unlink(lp)
            except OSError:
                pass

    return crawl_result.file_name, result


# ─────────────────────────────────────────────────────────────────────────────
# Queue builder — sort by size so small docs finish fast and give early feedback
# ─────────────────────────────────────────────────────────────────────────────

def _build_queue(
    crawler: Crawler,
    dry_run: bool,
    summary: RunSummary,
) -> list[CrawlResult]:
    """
    Collect all processable files from the crawler into a list,
    sorted smallest-first so fast wins appear early and the progress
    ETA is more accurate.
    """
    queue_list: list[CrawlResult] = []
    skipped = 0

    logger.info("Scanning archive — building work queue…")
    t0 = time.time()

    for cr in crawler.crawl():
        if _shutdown_requested:
            break

        if cr.error:
            summary.add("ERROR", cr.file_name, error=cr.error)
            continue

        if not cr.should_process:
            summary.add("SKIPPED", cr.file_name, error=cr.skip_reason)
            skipped += 1
            if skipped % 10_000 == 0:
                logger.info(f"  … skipped {skipped:,} unchanged files so far")
            continue

        if dry_run:
            summary.add("WOULD_PROCESS", cr.file_name)
            logger.info(f"[DRY RUN] {cr.file_name}")
            continue

        queue_list.append(cr)

    # Sort: smallest files first (faster OCR, gives early pipeline feedback)
    queue_list.sort(key=lambda c: c.file_size)

    elapsed = time.time() - t0
    logger.info(
        f"Queue built in {_fmt_duration(elapsed)}: "
        f"{len(queue_list):,} to process, "
        f"{skipped:,} skipped (unchanged)"
    )
    return queue_list


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    root:              Optional[Path] = None,
    dry_run:           bool           = False,
    force_reingest:    bool           = False,
    single_file:       Optional[str]  = None,
    workers:           int            = PAGE_WORKERS,
    doc_workers:       int            = 1,
    max_files:         Optional[int]  = None,
    retry_errors:      bool           = False,
    source_type:       str            = None,     # "nas" | "s3" (default: from config)
    enable_embeddings: bool           = None,     # True/False (default: from config)
) -> RunSummary:
    """
    Main pipeline runner.

    Parameters
    ----------
    root           : folder to crawl (defaults to config.DOCS_ROOT)
    dry_run        : crawl only, no ingestion
    force_reingest : ignore hash check, re-process everything
    single_file    : process one specific file path only
    workers        : OCR page threads per document
    doc_workers    : documents processed in parallel (default 1)
    max_files      : cap total files (for testing)
    retry_errors   : only process docs currently in ERROR state

    Speed formula
    -------------
    total_vision_threads = doc_workers × workers
    Keep total_vision_threads ≤ 16 to stay within Vision API limits.

    Examples
    --------
    doc_workers=4,  workers=4   → 16 Vision threads  (recommended)
    doc_workers=8,  workers=2   → 16 Vision threads  (more docs, less pages/doc)
    doc_workers=16, workers=1   → 16 Vision threads  (max docs, serial OCR)
    """
    global summary
    summary = RunSummary()

    # Apply config defaults for optional params
    if source_type is None:
        source_type = SOURCE_TYPE
    if enable_embeddings is None:
        enable_embeddings = ENABLE_EMBEDDINGS

    # ── Warn if Vision concurrency is high ───────────────────────────────────
    total_vision = doc_workers * workers
    if total_vision > 20:
        logger.warning(
            f"doc-workers({doc_workers}) × workers({workers}) = {total_vision} "
            f"concurrent Vision calls — risk of 429 errors. "
            f"Recommend keeping ≤ 16."
        )

    logger.info("=" * 68)
    logger.info("  EPOD INGESTION PIPELINE — HIGH THROUGHPUT")
    logger.info(f"  Source       : {source_type.upper()}")
    if source_type == "s3":
        from config import S3_BUCKET, S3_PREFIX, S3_REGION
        logger.info(f"  S3 Bucket    : s3://{S3_BUCKET}/{S3_PREFIX}")
        logger.info(f"  S3 Region    : {S3_REGION}")
    else:
        logger.info(f"  Root         : {root or DOCS_ROOT}")
    logger.info(f"  Dry run      : {dry_run}")
    logger.info(f"  Force        : {force_reingest}")
    logger.info(f"  Embeddings   : {'ON' if enable_embeddings else 'OFF'}")
    logger.info(f"  Doc workers  : {doc_workers}  (parallel documents)")
    logger.info(f"  Page workers : {workers}  (OCR threads per doc)")
    logger.info(f"  Vision slots : {_SEM_VISION._value}  (global cap)")
    logger.info(f"  GPT-4o slots : {_SEM_GPT4O._value}  (global cap, summaries)")
    logger.info(f"  Embed slots  : {_SEM_EMBED._value}  (global cap)")
    logger.info(f"  Single file  : {single_file or 'None'}")
    logger.info(f"  Retry errors : {retry_errors}")
    logger.info("=" * 68)

    # ── DB init — always runs (idempotent: CREATE TABLE IF NOT EXISTS) ────────
    # Must run even in dry-run mode because the crawler queries the DB
    # to decide what to skip (get_ingestion_record calls happen during crawl).
    logger.info("Initialising database schema…")
    init_db()

    # ── Connection pool + crash recovery — skip in dry-run ───────────────────
    if not dry_run:
        _init_pool(doc_workers)
        # Reset any RUNNING records left over from a previous crashed run.
        reset_stale_running(max_age_minutes=60)

    # ── Single-file mode ─────────────────────────────────────────────────────
    if single_file:
        return _run_single_file(
            single_file, dry_run, force_reingest, workers, enable_embeddings
        )

    # ── Crawler ──────────────────────────────────────────────────────────────
    crawler = _make_crawler(
        source_type=source_type,
        root=root,
        force_reingest=force_reingest or retry_errors,
        max_files=max_files,
    )

    # If retrying errors only, override crawler to only return ERROR docs
    if retry_errors and not dry_run:
        logger.info("--retry-errors: loading ERROR docs from DB")
        error_docs = get_failed_docs()
        logger.info(f"  Found {len(error_docs)} docs in ERROR state")
        # Build synthetic CrawlResults from DB records
        work_queue = _build_error_retry_queue(error_docs)
    else:
        # Normal path — scan the archive
        work_queue = _build_queue(crawler, dry_run, summary)

    if dry_run or not work_queue:
        summary.print()
        return summary

    # ── Progress tracker ─────────────────────────────────────────────────────
    progress = ProgressTracker(
        total=len(work_queue),
        report_every=max(1, min(50, len(work_queue) // 20)),  # report ~20 times
    )

    # ── Parallel document processing ─────────────────────────────────────────
    doc_args = [(cr, workers, enable_embeddings) for cr in work_queue]

    logger.info(
        f"Starting ingestion: {len(work_queue):,} documents, "
        f"{doc_workers} parallel workers"
    )

    if doc_workers == 1:
        # Serial mode — simpler, easier to debug
        for cr, w, emb in doc_args:
            if _shutdown_requested:
                break
            _, result = _process_doc_thread((cr, w, emb))
            summary.add(
                status    = result["status"],
                file_name = cr.file_name,
                error     = result.get("error"),
                chunks    = result.get("chunks", 0),
                pages     = result.get("pages", 0),
                elapsed   = result.get("elapsed", 0),
            )
            progress.record(result["status"])
    else:
        # Parallel mode — ThreadPoolExecutor across documents
        with concurrent.futures.ThreadPoolExecutor(max_workers=doc_workers) as pool:
            futures = {
                pool.submit(_process_doc_thread, arg): arg[0]
                for arg in doc_args
            }
            for fut in concurrent.futures.as_completed(futures):
                if _shutdown_requested:
                    # Cancel pending (not yet started) futures
                    for f in futures:
                        f.cancel()
                    break

                cr = futures[fut]
                try:
                    file_name, result = fut.result()
                except Exception as e:
                    file_name = cr.file_name
                    result = {
                        "status": "ERROR", "chunks": 0, "pages": 0,
                        "elapsed": 0.0, "error": str(e),
                    }

                summary.add(
                    status    = result["status"],
                    file_name = file_name,
                    error     = result.get("error"),
                    chunks    = result.get("chunks", 0),
                    pages     = result.get("pages", 0),
                    elapsed   = result.get("elapsed", 0),
                )
                progress.record(result["status"])

    progress.final()
    summary.print()

    if not dry_run:
        _print_db_stats()

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Error retry queue builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_error_retry_queue(error_docs: list[dict]) -> list[CrawlResult]:
    """Convert DB ERROR records back into CrawlResult objects for reprocessing."""
    import datetime
    results = []
    for doc in error_docs:
        fp = doc["file_path"]
        try:
            if fp.startswith("s3://"):
                # S3 path — download to temp file
                from s3_crawler import download_s3_uri
                local_path = download_s3_uri(fp)
                if local_path is None:
                    logger.warning(f"  Skipping unreachable S3 file: {fp}")
                    continue
                file_name = Path(fp).name
                file_size = os.path.getsize(local_path)
                file_hash = doc.get("file_hash", "")
                mtime     = datetime.datetime.now(tz=datetime.timezone.utc)
                cr = CrawlResult(
                    file_path=fp,
                    file_name=file_name,
                    doc_id=hash_file_path(fp),
                    file_hash=file_hash,
                    file_size=file_size,
                    last_modified=mtime,
                    should_process=True,
                    local_path=local_path,
                )
            else:
                # NAS / local path
                p = Path(fp)
                if not p.exists():
                    logger.warning(f"  Skipping missing file: {fp}")
                    continue
                file_hash = hash_file_bytes(fp)
                file_size = p.stat().st_size
                mtime     = datetime.datetime.fromtimestamp(
                    p.stat().st_mtime, tz=datetime.timezone.utc
                )
                cr = CrawlResult(
                    file_path=fp,
                    file_name=p.name,
                    doc_id=hash_file_path(fp.replace("\\", "/")),
                    file_hash=file_hash,
                    file_size=file_size,
                    last_modified=mtime,
                    should_process=True,
                )
            results.append(cr)
        except Exception as e:
            logger.warning(f"  Could not build CrawlResult for {fp}: {e}")
    logger.info(f"  Retry queue: {len(results)} reachable ERROR docs")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Single-file mode
# ─────────────────────────────────────────────────────────────────────────────

def _run_single_file(
    file_path:         str,
    dry_run:           bool,
    force_reingest:    bool,
    workers:           int,
    enable_embeddings: bool = True,
) -> RunSummary:
    import datetime

    is_s3    = file_path.startswith("s3://")
    file_name = Path(file_path).name

    if dry_run:
        logger.info(f"[DRY RUN] Would process: {file_name}")
        summary.add("WOULD_PROCESS", file_name)
        summary.print()
        return summary

    local_path = None

    if is_s3:
        from s3_crawler import download_s3_uri
        logger.info(f"Downloading S3 file: {file_path}")
        local_path = download_s3_uri(file_path)
        if local_path is None:
            logger.error(f"Failed to download: {file_path}")
            summary.add("ERROR", file_name, error="S3 download failed")
            summary.print()
            return summary
        path_str  = file_path
        file_hash = ""   # no local hash for S3 single-file mode
        file_size = os.path.getsize(local_path)
        mtime     = datetime.datetime.now(tz=datetime.timezone.utc)
    else:
        fp = Path(file_path)
        if not fp.exists():
            logger.error(f"File not found: {file_path}")
            summary.add("ERROR", file_name, error="File not found")
            summary.print()
            return summary
        if fp.suffix.lower() != ".pdf":
            logger.error(f"Unsupported file type: {fp.suffix}")
            summary.add("ERROR", file_name, error=f"Unsupported: {fp.suffix}")
            summary.print()
            return summary
        path_str  = str(fp).replace("\\", "/")
        file_hash = hash_file_bytes(str(fp))
        file_size = fp.stat().st_size
        mtime     = datetime.datetime.fromtimestamp(
            fp.stat().st_mtime, tz=datetime.timezone.utc
        )

    doc_id = hash_file_path(path_str)

    if not force_reingest and not is_s3:
        process, reason = should_ingest(path_str, file_hash)
        if not process:
            logger.info(f"Skipping {file_name}: {reason}")
            summary.add("SKIPPED", file_name, error=reason)
            summary.print()
            return summary

    upsert_ingestion_log(
        file_path=path_str, file_name=file_name, file_hash=file_hash,
        file_size=file_size, last_modified=mtime, status="RUNNING",
    )

    cr = CrawlResult(
        file_path=path_str, file_name=file_name, doc_id=doc_id,
        file_hash=file_hash, file_size=file_size, last_modified=mtime,
        should_process=True, local_path=local_path,
    )

    try:
        result = process_document(cr, workers=workers, enable_embeddings=enable_embeddings)
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.unlink(local_path)
            except OSError:
                pass

    summary.add(
        status=result["status"], file_name=file_name,
        error=result.get("error"), chunks=result.get("chunks", 0),
        pages=result.get("pages", 0), elapsed=result.get("elapsed", 0),
    )
    summary.print()
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# DB stats printer
# ─────────────────────────────────────────────────────────────────────────────

def _print_db_stats():
    try:
        stats = get_pipeline_stats()
        logger.info("── Database state ──────────────────────────────────")
        for row in stats.get("by_status", []):
            logger.info(
                f"  {row['status']:<12} "
                f"docs={row['doc_count']:>8,}  "
                f"chunks={row['total_chunks'] or 0:>10,}  "
                f"pages={row['total_pages'] or 0:>10,}"
            )
        failed = get_failed_docs()
        if failed:
            logger.warning(f"  {len(failed)} document(s) still in ERROR state")
            logger.warning(f"  Run with --retry-errors to reprocess them")
            for doc in failed[:5]:
                logger.warning(f"    • {doc['file_name']}: {doc['error_message'][:80]}")
    except Exception as e:
        logger.warning(f"Could not fetch DB stats: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    ap = argparse.ArgumentParser(
        description="EPOD Document Ingestion Pipeline — High Throughput Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Speed guide (Vision API stays safe at ≤ 16 concurrent calls):
  doc-workers × workers ≤ 16

Recommended settings:
  --doc-workers 4  --workers 4    # balanced  (16 Vision threads)
  --doc-workers 8  --workers 2    # more docs (16 Vision threads)
  --doc-workers 16 --workers 1    # max docs  (16 Vision threads)

Examples:
  python main.py --doc-workers 8 --workers 2
  python main.py --dry-run
  python main.py --force --doc-workers 8 --workers 2
  python main.py --file path/to/doc.pdf
  python main.py --max-files 100 --doc-workers 4 --workers 4
  python main.py --retry-errors --doc-workers 8 --workers 2
  python main.py --stats
        """,
    )
    ap.add_argument("--root",         type=str,  help="Override DOCS_ROOT from config (NAS mode)")
    ap.add_argument("--file",         type=str,  help="Process a single file (local path or s3://bucket/key)")
    ap.add_argument("--dry-run",      action="store_true", help="Preview only, no DB writes")
    ap.add_argument("--force",        action="store_true", help="Re-ingest even unchanged files")
    ap.add_argument("--retry-errors", action="store_true", help="Reprocess only ERROR-state docs")
    ap.add_argument("--stats",        action="store_true", help="Show DB stats and exit")
    ap.add_argument(
        "--source", type=str, choices=["nas", "s3"],
        help="Input source: 'nas' (local/UNC path) or 's3' (Amazon S3). Overrides config.ini.",
    )
    ap.add_argument(
        "--no-embed", action="store_true",
        help="Skip vector embedding; chunks stored without vectors (NULL). Overrides config.ini.",
    )
    ap.add_argument(
        "--doc-workers", type=int, default=1,
        help=(
            "Documents to process in parallel (default: 1). "
            "Recommended: 4-16. "
            "Keep doc-workers × workers ≤ 16."
        ),
    )
    ap.add_argument(
        "--workers", type=int, default=PAGE_WORKERS,
        help=(
            "OCR page threads per document (default: from config). "
            "Keep doc-workers × workers ≤ 16."
        ),
    )
    ap.add_argument("--max-files",    type=int,  help="Cap total files (for testing)")
    return ap.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = _parse_args()

    if args.stats:
        init_db()
        _print_db_stats()
        failed = get_failed_docs()
        if failed:
            print(f"\n{len(failed):,} failed document(s):")
            for doc in failed[:20]:
                print(f"  [{doc['retry_count']} retries] {doc['file_name']}")
                print(f"    {doc['error_message'][:120]}")
        sys.exit(0)

    run_pipeline(
        root              = Path(args.root) if args.root else None,
        dry_run           = args.dry_run,
        force_reingest    = args.force,
        single_file       = args.file,
        workers           = args.workers,
        doc_workers       = args.doc_workers,
        max_files         = args.max_files,
        retry_errors      = args.retry_errors,
        source_type       = args.source,           # None → use config.ini value
        enable_embeddings = not args.no_embed if args.no_embed else None,
    )
