"""
crawler.py
==========
Crawls the EPOD document root (including all sub-folders) and decides
which files need ingesting, re-ingesting, or can be skipped.

  - Walks all sub-folders recursively
  - Filters by supported extensions (.pdf)
  - FAST RESUME: DB lookup first using only path + mtime (no file read)
  - Only reads file bytes to hash when the DB record is absent or mtime changed
  - Handles UNC paths correctly on Windows
  - Yields CrawlResult objects consumed by main.py orchestrator
  - Never raises — all errors are caught, logged, yielded as ERROR status

Resume behaviour
----------------
When resuming after a stop (e.g. at 1M of 2M docs):

  Already-ingested docs (status=SUCCESS, mtime unchanged):
    → Skipped with ZERO file I/O — only a DB PK lookup
    → Cost: ~0.1ms per file (B-tree index lookup)
    → 1M done docs ≈ 2–3 minutes of DB lookups, no network reads

  Modified docs (mtime changed since last ingest):
    → File is re-hashed to confirm actual content change
    → Only then re-ingested

  RUNNING docs (crashed mid-run):
    → Skipped as "already_running"
    → Fix before resume:
         UPDATE epod.ingestion_log SET status='PENDING', retry_count=0
         WHERE status='RUNNING';

  ERROR docs (< 3 retries):
    → Automatically retried on next run

  ERROR docs (>= 3 retries):
    → Permanently skipped (use --retry-errors or manual SQL reset)

Usage:
    from crawler import Crawler
    for result in Crawler().crawl():
        if result.should_process:
            # hand off to pdf_processor
        else:
            # log skip reason
"""

import os
import hashlib
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import DOCS_ROOT, SUPPORTED_EXTENSIONS
from db import (
    hash_file_path,
    hash_file_bytes,
    should_ingest,
    get_ingestion_record,
    upsert_ingestion_log,
)
from logger import get_logger, DocLogger

logger = get_logger("epod.crawler")


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CrawlResult:
    """
    One entry produced by the crawler for each file found.
    main.py inspects should_process to decide whether to send it downstream.

    local_path
        Set by S3Crawler when the file has been downloaded to a local temp
        path for processing.  None means file_path itself is the local path
        (NAS / on-disk mode).  Callers must delete local_path after use.
    """
    file_path:      str
    file_name:      str
    doc_id:         str
    file_hash:      str
    file_size:      int
    last_modified:  datetime
    should_process: bool        # True  → send to pdf_processor
    skip_reason:    str = ""    # non-empty when should_process=False
    error:          str = ""    # non-empty when crawl itself failed
    local_path:     Optional[str] = None  # temp download path for S3 files


@dataclass
class CrawlStats:
    """Aggregate counts across the full crawl — printed at end of crawl()."""
    total_found:      int   = 0
    to_process:       int   = 0
    skipped_unchanged: int  = 0   # fast-skipped via mtime (no file read)
    skipped_other:    int   = 0   # running / max-retries / empty / unreadable
    errors:           int   = 0
    total_size_mb:    float = 0.0
    files_by_ext:     dict  = field(default_factory=dict)
    bytes_avoided:    int   = 0   # bytes NOT read because of fast-skip


# ─────────────────────────────────────────────────────────────────────────────
# UNC / Windows path helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_path(p: Path) -> str:
    """
    Return a clean, consistent string form of any path.
    Converts Windows backslashes to forward slashes for DB storage.
    """
    return str(p).replace("\\", "/")


def _accessible(p: Path) -> bool:
    """Check if a path is readable without raising."""
    try:
        return os.access(str(p), os.R_OK)
    except (OSError, PermissionError):
        return False


def _file_mtime(p: Path) -> datetime:
    """Return last-modified time as UTC datetime."""
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except OSError:
        return datetime.now(tz=timezone.utc)


def _file_size(p: Path) -> int:
    """Return file size in bytes, 0 on error."""
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _mtime_matches_record(mtime: datetime, record: dict) -> bool:
    """
    True if the file's current mtime matches what was recorded at ingest time.
    Used as the fast-skip gate — if mtime is identical, content almost certainly
    hasn't changed, so we skip the expensive file hash entirely.
    We allow 2 seconds of tolerance for filesystem clock drift on UNC shares.
    """
    recorded = record.get("last_modified")
    if recorded is None:
        return False
    # Ensure both are timezone-aware for comparison
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    diff = abs((mtime - recorded).total_seconds())
    return diff <= 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Crawler
# ─────────────────────────────────────────────────────────────────────────────

class Crawler:
    """
    Recursively walks DOCS_ROOT and yields CrawlResult for every supported file.

    Resume optimisation
    -------------------
    For files already marked SUCCESS in the DB whose mtime hasn't changed,
    the crawler skips them with ZERO file I/O — only a DB PK lookup.
    This means resuming after ingesting 1M of 2M docs costs ~2–3 minutes
    of DB lookups rather than hours of network file reads.

    Parameters
    ----------
    root           : Override the root folder from config (useful for testing).
    extensions     : Override supported file extensions.
    force_reingest : If True, ignore all checks and process every file.
    max_files      : Cap the number of files yielded (useful for testing).
    """

    def __init__(
        self,
        root:           Path = None,
        extensions:     set  = None,
        force_reingest: bool = False,
        max_files:      int  = None,
    ):
        self.root           = Path(root) if root else DOCS_ROOT
        self.extensions     = extensions or SUPPORTED_EXTENSIONS
        self.force_reingest = force_reingest
        self.max_files      = max_files
        self.stats          = CrawlStats()

    # ── Public ───────────────────────────────────────────────────────────────

    def crawl(self) -> Iterator[CrawlResult]:
        """
        Main entry point. Yields one CrawlResult per supported file found.
        Always yields — never raises.
        """
        logger.info(f"Starting crawl: {self.root}")
        logger.info(f"Extensions: {self.extensions}")
        if self.force_reingest:
            logger.warning("force_reingest=True — all files will be re-processed")

        if not self._check_root():
            return

        yielded = 0
        for file_path in self._walk():
            if self.max_files and yielded >= self.max_files:
                logger.info(f"max_files={self.max_files} reached, stopping crawl.")
                break

            result = self._evaluate(file_path)
            self._update_stats(result, file_path)

            yield result
            yielded += 1

        self._log_stats()

    def dry_run(self) -> list[CrawlResult]:
        """Returns all CrawlResults without side effects (no DB writes)."""
        logger.info("DRY RUN — no ingestion will occur")
        return list(self.crawl())

    # ── Private helpers ──────────────────────────────────────────────────────

    def _check_root(self) -> bool:
        if not self.root.exists():
            logger.error(f"DOCS_ROOT does not exist: {self.root}")
            return False
        if not self.root.is_dir():
            logger.error(f"DOCS_ROOT is not a directory: {self.root}")
            return False
        if not _accessible(self.root):
            logger.error(f"DOCS_ROOT is not readable: {self.root}")
            return False
        logger.info(f"Root folder confirmed: {self.root}")
        return True

    def _walk(self) -> Iterator[Path]:
        """
        Recursively yield all files matching the extension filter.
        Skips inaccessible sub-directories (logs warning, continues).
        """
        try:
            for dirpath, dirnames, filenames in os.walk(str(self.root)):
                dirnames[:] = [
                    d for d in dirnames
                    if _accessible(Path(dirpath) / d)
                ]
                for fname in sorted(filenames):   # sorted for deterministic order
                    ext = Path(fname).suffix.lower()
                    if ext in self.extensions:
                        yield Path(dirpath) / fname
        except (PermissionError, OSError) as e:
            logger.error(f"Walk error at {self.root}: {e}")

    def _evaluate(self, file_path: Path) -> CrawlResult:
        """
        Decide whether a file needs ingesting.

        Fast-resume order of operations
        --------------------------------
        1. stat() — get size + mtime          (always, very fast)
        2. DB PK lookup                        (always, sub-ms)
        3. mtime comparison                    (for SUCCESS records)
           → if unchanged: SKIP with zero file I/O   ← the key optimisation
        4. hash_file_bytes()                   (ONLY if DB says we may process)
        5. should_ingest() full decision       (with hash)
        6. Mark RUNNING + yield
        """
        path_str  = _normalise_path(file_path)
        file_name = file_path.name
        doc_id    = hash_file_path(path_str)
        dlog      = DocLogger(file_name, doc_id)

        # ── 1. Accessibility check ───────────────────────────────────────────
        if not _accessible(file_path):
            dlog.warning(f"File not readable — skipping: {path_str}")
            return CrawlResult(
                file_path=path_str, file_name=file_name, doc_id=doc_id,
                file_hash="", file_size=0,
                last_modified=datetime.now(tz=timezone.utc),
                should_process=False, skip_reason="not_readable",
                error="File not accessible",
            )

        # ── 2. stat() — size + mtime (no file content read) ─────────────────
        file_size     = _file_size(file_path)
        last_modified = _file_mtime(file_path)

        if file_size == 0:
            dlog.warning(f"Zero-byte file — skipping: {file_name}")
            return CrawlResult(
                file_path=path_str, file_name=file_name, doc_id=doc_id,
                file_hash="", file_size=0, last_modified=last_modified,
                should_process=False, skip_reason="empty_file",
            )

        # ── 3. DB lookup (PK index — sub-millisecond) ────────────────────────
        if self.force_reingest:
            # Skip all DB checks — hash the file and queue it
            record = None
        else:
            record = get_ingestion_record(path_str)

        # ── 4. FAST SKIP — zero file I/O ─────────────────────────────────────
        #
        # If the DB shows SUCCESS and the file mtime hasn't changed,
        # we are certain the content is unchanged. No need to read the file.
        #
        # This is the core resume optimisation. For 1M already-ingested docs
        # this saves reading potentially terabytes of data from the UNC share.
        #
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "SUCCESS"
            and _mtime_matches_record(last_modified, record)
        ):
            dlog.debug(f"FAST-SKIP {file_name}  (SUCCESS + mtime unchanged)")
            self.stats.bytes_avoided += file_size
            return CrawlResult(
                file_path=path_str,
                file_name=file_name,
                doc_id=doc_id,
                file_hash=record["file_hash"],   # reuse stored hash
                file_size=file_size,
                last_modified=last_modified,
                should_process=False,
                skip_reason="unchanged",
            )

        # ── 5. Already running on another machine / process ──────────────────
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "RUNNING"
        ):
            dlog.debug(f"SKIP {file_name}  (RUNNING on another worker)")
            return CrawlResult(
                file_path=path_str, file_name=file_name, doc_id=doc_id,
                file_hash=record.get("file_hash", ""),
                file_size=file_size, last_modified=last_modified,
                should_process=False, skip_reason="already_running",
            )

        # ── 6. Permanently failed (max retries exceeded) ─────────────────────
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "ERROR"
            and record.get("retry_count", 0) >= 3
        ):
            dlog.warning(
                f"SKIP {file_name}  "
                f"(ERROR × {record['retry_count']} retries — permanently skipped. "
                f"Reset manually: UPDATE ingestion_log SET status='PENDING', "
                f"retry_count=0 WHERE file_name='{file_name}')"
            )
            return CrawlResult(
                file_path=path_str, file_name=file_name, doc_id=doc_id,
                file_hash=record.get("file_hash", ""),
                file_size=file_size, last_modified=last_modified,
                should_process=False,
                skip_reason=f"max_retries_exceeded ({record['retry_count']})",
            )

        # ── 7. Hash the file — only reached for new / modified / retry docs ──
        #
        # At this point we know the file actually needs consideration:
        #   - Never seen before (record is None)
        #   - mtime changed since last SUCCESS (file may have been modified)
        #   - Was ERROR with retries remaining
        #   - Was PENDING
        #   - force_reingest=True
        #
        try:
            file_hash = hash_file_bytes(str(file_path))
        except (OSError, PermissionError) as e:
            dlog.error(f"Cannot hash file: {e}")
            return CrawlResult(
                file_path=path_str, file_name=file_name, doc_id=doc_id,
                file_hash="", file_size=file_size, last_modified=last_modified,
                should_process=False, skip_reason="hash_error",
                error=str(e),
            )

        # ── 8. Full skip/ingest decision with hash ───────────────────────────
        if self.force_reingest:
            process, reason = True, "force_reingest"
        else:
            process, reason = should_ingest(path_str, file_hash)

        # ── 9. Queue + mark RUNNING ──────────────────────────────────────────
        if process:
            dlog.info(
                f"QUEUE  {file_name}  "
                f"({file_size / 1024 / 1024:.1f} MB)  reason={reason}"
            )
            try:
                upsert_ingestion_log(
                    file_path=path_str,
                    file_name=file_name,
                    file_hash=file_hash,
                    file_size=file_size,
                    last_modified=last_modified,
                    status="RUNNING",
                )
            except Exception as e:
                dlog.error(f"Could not mark RUNNING: {e}")
        else:
            dlog.debug(f"SKIP   {file_name}  reason={reason}")

        return CrawlResult(
            file_path=path_str,
            file_name=file_name,
            doc_id=doc_id,
            file_hash=file_hash,
            file_size=file_size,
            last_modified=last_modified,
            should_process=process,
            skip_reason="" if process else reason,
        )

    def _update_stats(self, result: CrawlResult, file_path: Path):
        self.stats.total_found += 1
        self.stats.total_size_mb += result.file_size / 1024 / 1024

        ext = file_path.suffix.lower()
        self.stats.files_by_ext[ext] = self.stats.files_by_ext.get(ext, 0) + 1

        if result.error:
            self.stats.errors += 1
        elif result.should_process:
            self.stats.to_process += 1
        elif result.skip_reason == "unchanged":
            self.stats.skipped_unchanged += 1
        else:
            self.stats.skipped_other += 1

    def _log_stats(self):
        s = self.stats
        avoided_gb = s.bytes_avoided / 1024 / 1024 / 1024
        logger.info("─" * 60)
        logger.info(f"  Crawl complete")
        logger.info(f"  Root               : {self.root}")
        logger.info(f"  Total files found  : {s.total_found:,}")
        logger.info(f"  To process         : {s.to_process:,}")
        logger.info(f"  Fast-skipped       : {s.skipped_unchanged:,}  "
                    f"(SUCCESS + mtime match — zero file I/O)")
        logger.info(f"  Network I/O avoided: {avoided_gb:.2f} GB  "
                    f"({s.bytes_avoided / 1024 / 1024:.0f} MB)")
        logger.info(f"  Other skipped      : {s.skipped_other:,}  "
                    f"(running / max-retries / empty / unreadable)")
        logger.info(f"  Errors             : {s.errors:,}  "
                    f"(inaccessible / hash fail)")
        logger.info(f"  Total size on disk : {s.total_size_mb:.1f} MB")
        for ext, count in sorted(s.files_by_ext.items()):
            logger.info(f"    {ext:<8} {count:>8,} files")
        logger.info("─" * 60)

        if s.skipped_unchanged > 0:
            logger.info(
                f"  Resume tip: {s.skipped_unchanged:,} files were fast-skipped "
                f"(no file reads). To force re-ingest use --force."
            )


# ─────────────────────────────────────────────────────────────────────────────
# CLI helper  (python crawler.py --dry-run  to preview without ingesting)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="EPOD document crawler")
    ap.add_argument("--root",      type=str, help="Override DOCS_ROOT")
    ap.add_argument("--dry-run",   action="store_true", help="Preview only, no DB writes")
    ap.add_argument("--force",     action="store_true", help="Re-ingest all files")
    ap.add_argument("--max-files", type=int, help="Limit files (for testing)")
    args = ap.parse_args()

    crawler = Crawler(
        root           = Path(args.root) if args.root else None,
        force_reingest = args.force,
        max_files      = args.max_files,
    )

    if args.dry_run:
        results = crawler.dry_run()
        to_do    = [r for r in results if r.should_process]
        skipped  = [r for r in results if not r.should_process and not r.error]
        errors   = [r for r in results if r.error]
        print(f"\nWould process : {len(to_do):,} files")
        print(f"Would skip    : {len(skipped):,} files  (unchanged / running / max-retries)")
        print(f"Errors        : {len(errors):,} files  (unreadable / hash fail)")
        if to_do:
            print("\nFiles queued for ingestion:")
            for r in to_do[:20]:
                print(f"  {r.file_name}  ({r.file_size/1024/1024:.1f} MB)")
            if len(to_do) > 20:
                print(f"  … and {len(to_do)-20:,} more")
    else:
        print("Run main.py to start ingestion. Use --dry-run to preview.")
