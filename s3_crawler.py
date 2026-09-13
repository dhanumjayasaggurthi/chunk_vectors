"""
s3_crawler.py
=============
S3-backed drop-in replacement for crawler.Crawler.

Differences from the NAS crawler:
  - Walks an S3 bucket/prefix instead of a local filesystem.
  - Uses S3 ETag as the change-detection hash — no download needed to check
    whether a file has changed.
  - Does NOT download files during the crawl phase.  local_path is always
    None when CrawlResult leaves this module.  The actual download happens
    inside the worker thread in main._process_doc_thread(), so at most
    doc_workers temp files exist on disk at any moment.
  - Stores "s3://bucket/key" as the canonical file_path in the DB.
  - Thread-local boto3 clients: each thread gets its own session so that
    parallel download_file calls from multiple doc_workers are fully safe.

Resume behaviour mirrors the NAS crawler:
  SUCCESS + ETag unchanged → fast-skip (no download, no file read)
  LastModified used as a secondary fast-skip gate (2-second tolerance)
  ERROR with retries remaining → re-queued
  RUNNING → skip (another worker is handling it)
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import (
    S3_BUCKET,
    S3_PREFIX,
    S3_REGION,
    S3_PROFILE,
    S3_ENDPOINT_URL,
    S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY,
    S3_SESSION_TOKEN,
    S3_TEMP_DIR,
    SUPPORTED_EXTENSIONS,
)
from crawler import CrawlResult, CrawlStats, _mtime_matches_record
from db import (
    hash_file_path,
    should_ingest,
    get_ingestion_record,
    upsert_ingestion_log,
)
from logger import get_logger, DocLogger

logger = get_logger("epod.s3_crawler")


# ─────────────────────────────────────────────────────────────────────────────
# Thread-local boto3 client
# Each worker thread gets its own boto3 Session + client.
# boto3 Sessions and the Clients they create are NOT safe to share across
# threads for concurrent calls; thread-local instances sidestep this entirely.
# ─────────────────────────────────────────────────────────────────────────────

_tls = threading.local()


def _make_session() -> "boto3.Session":
    """
    Build a boto3 Session using the same priority order as the working
    rimdocs_s3_extracts project:
      1. Named profile  (S3_PROFILE in config.ini [S3])   ← preferred for JNJ
      2. Explicit keys  (S3_ACCESS_KEY_ID / SECRET)        ← fallback
      3. IAM role / env vars                               ← boto3 default
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            "boto3 is required for S3 mode.  Install it: pip install boto3"
        )

    session_kwargs: dict = {"region_name": S3_REGION}
    if S3_PROFILE:
        session_kwargs["profile_name"] = S3_PROFILE
    elif S3_ACCESS_KEY_ID:
        session_kwargs["aws_access_key_id"]     = S3_ACCESS_KEY_ID
        session_kwargs["aws_secret_access_key"] = S3_SECRET_ACCESS_KEY
        if S3_SESSION_TOKEN:
            session_kwargs["aws_session_token"] = S3_SESSION_TOKEN

    return boto3.Session(**session_kwargs)


def _make_client(session: "boto3.Session"):
    """
    Create an S3 client from a Session, mirroring the working project:
      - adaptive retry (10 attempts)
      - optional custom endpoint_url (VPC endpoint / proxy)
    """
    from botocore.config import Config as BotoConfig
    client_kwargs: dict = {
        "config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})
    }
    if S3_ENDPOINT_URL:
        client_kwargs["endpoint_url"] = S3_ENDPOINT_URL
    return session.client("s3", **client_kwargs)


def _thread_client():
    """Return this thread's dedicated boto3 S3 client (created on first call)."""
    if not hasattr(_tls, "s3_client"):
        _tls.s3_client = _make_client(_make_session())
    return _tls.s3_client


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_etag(etag: str) -> str:
    """S3 ETags are wrapped in quotes; strip them for consistent storage."""
    return etag.strip('"')


def _s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


# ─────────────────────────────────────────────────────────────────────────────
# S3 download helper  (called from worker threads in main.py)
# ─────────────────────────────────────────────────────────────────────────────

def download_s3_uri(s3_uri: str) -> Optional[str]:
    """
    Download a single 's3://bucket/key' URI to a local temp file.
    Returns the temp file path, or None on failure.

    Thread-safe: uses a per-thread boto3 client.
    Called by main._process_doc_thread() just before PDF processing begins,
    and also by _build_error_retry_queue() for ERROR-state retries.

    The caller is responsible for deleting the temp file after use.
    """
    if not s3_uri.startswith("s3://"):
        return None

    rest   = s3_uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        logger.error(f"Malformed S3 URI: {s3_uri}")
        return None

    file_name = Path(key).name
    suffix    = Path(file_name).suffix or ".pdf"
    tmp_dir = S3_TEMP_DIR or None   # None → system /tmp (or %TEMP% on Windows)
    if tmp_dir:
        os.makedirs(tmp_dir, exist_ok=True)

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=tmp_dir)
        os.close(fd)
        logger.debug(f"Downloading {s3_uri} → {tmp_path}")
        _thread_client().download_file(bucket, key, tmp_path)
        return tmp_path
    except Exception as e:
        logger.error(f"S3 download failed for {s3_uri}: {e}")
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None


# ─────────────────────────────────────────────────────────────────────────────
# S3 Crawler
# ─────────────────────────────────────────────────────────────────────────────

class S3Crawler:
    """
    Crawls an S3 bucket and yields CrawlResult objects compatible with
    the NAS Crawler interface consumed by main.py.

    Key design: the crawl phase only lists S3 objects and checks the DB.
    No files are downloaded here.  local_path is always None on returned
    CrawlResults.  Downloads happen in main._process_doc_thread() bounded
    by doc_workers, so at most doc_workers temp files exist at once.

    Parameters
    ----------
    bucket         : S3 bucket name (defaults to config.S3_BUCKET)
    prefix         : Key prefix / folder path within the bucket
    extensions     : File extensions to process (default: {".pdf"})
    force_reingest : Ignore all checks, re-process every file
    max_files      : Cap total files yielded (for testing)
    """

    def __init__(
        self,
        bucket:         str  = None,
        prefix:         str  = None,
        extensions:     set  = None,
        force_reingest: bool = False,
        max_files:      int  = None,
    ):
        self.bucket         = bucket or S3_BUCKET
        self.prefix         = prefix if prefix is not None else S3_PREFIX
        self.extensions     = extensions or SUPPORTED_EXTENSIONS
        self.force_reingest = force_reingest
        self.max_files      = max_files
        self.stats          = CrawlStats()

    # ── Public ───────────────────────────────────────────────────────────────

    def crawl(self) -> Iterator[CrawlResult]:
        """
        Main entry point.  Yields one CrawlResult per supported S3 object.
        local_path is always None — downloads are deferred to worker threads.
        Never raises — all errors are caught and yielded as error results.
        """
        logger.info(f"Starting S3 crawl: s3://{self.bucket}/{self.prefix}")
        logger.info(f"Extensions: {self.extensions}")
        if self.force_reingest:
            logger.warning("force_reingest=True — all files will be re-processed")

        if not self._check_bucket():
            return

        yielded = 0
        for obj in self._list_objects():
            if self.max_files and yielded >= self.max_files:
                logger.info(f"max_files={self.max_files} reached, stopping crawl.")
                break

            result = self._evaluate(obj)
            if result is None:
                continue

            self._update_stats(result, obj)
            yield result
            yielded += 1

        self._log_stats()

    def dry_run(self) -> list[CrawlResult]:
        logger.info("DRY RUN — no ingestion will occur")
        return list(self.crawl())

    # ── Private ──────────────────────────────────────────────────────────────

    def _check_bucket(self) -> bool:
        if not self.bucket:
            logger.error("S3 bucket not configured. Set [S3] bucket in config.ini")
            return False
        try:
            # list_objects_v2 is more reliable than head_bucket across regions
            _thread_client().list_objects_v2(Bucket=self.bucket, MaxKeys=1)
            logger.info(f"S3 bucket confirmed: s3://{self.bucket}/{self.prefix}")
            return True
        except Exception as e:
            err = str(e)
            if "NoSuchBucket" in err or "404" in err:
                logger.error(f"S3 bucket '{self.bucket}' does not exist.")
            elif "AccessDenied" in err or "403" in err:
                logger.error(
                    f"Access denied to s3://{self.bucket}. "
                    f"Check IAM permissions: s3:ListBucket + s3:GetObject are required."
                )
            elif "400" in err or "Bad Request" in err or "301" in err:
                logger.error(
                    f"S3 bucket '{self.bucket}' returned: {err}\n"
                    f"  Most likely cause: wrong region in config.ini [S3].\n"
                    f"  Current region setting: {S3_REGION}\n"
                    f"  Fix: open AWS Console → S3 → {self.bucket} → Properties → "
                    f"find the correct region, then update config.ini [S3] region = <correct-region>"
                )
            else:
                logger.error(f"Cannot access S3 bucket '{self.bucket}': {e}")
            return False

    def _list_objects(self) -> Iterator[dict]:
        """Paginate through all objects under the configured prefix (lazy, no buffering)."""
        paginator = _thread_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                if Path(obj["Key"]).suffix.lower() in self.extensions:
                    yield obj

    def _evaluate(self, obj: dict) -> Optional[CrawlResult]:
        """
        Decide whether an S3 object needs processing.
        Uses ETag as file_hash — no download needed at this stage.
        Returns CrawlResult with local_path=None (download deferred).
        """
        key       = obj["Key"]
        file_name = Path(key).name
        file_size = obj.get("Size", 0)
        etag      = _strip_etag(obj.get("ETag", ""))
        s3_uri    = _s3_uri(self.bucket, key)
        doc_id    = hash_file_path(s3_uri)
        dlog      = DocLogger(file_name, doc_id)

        last_modified: datetime = obj.get("LastModified")
        if last_modified is None:
            last_modified = datetime.now(tz=timezone.utc)
        elif last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)

        if file_size == 0:
            dlog.warning(f"Zero-byte S3 object — skipping: {key}")
            return CrawlResult(
                file_path=s3_uri, file_name=file_name, doc_id=doc_id,
                file_hash=etag, file_size=0, last_modified=last_modified,
                should_process=False, skip_reason="empty_file",
            )

        # ── DB lookup ────────────────────────────────────────────────────────
        record = None if self.force_reingest else get_ingestion_record(s3_uri)

        # ── Fast-skip: SUCCESS + ETag + LastModified all match ───────────────
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "SUCCESS"
            and record.get("file_hash") == etag
            and _mtime_matches_record(last_modified, record)
        ):
            dlog.debug(f"FAST-SKIP {file_name}  (SUCCESS + ETag unchanged)")
            self.stats.bytes_avoided += file_size
            return CrawlResult(
                file_path=s3_uri, file_name=file_name, doc_id=doc_id,
                file_hash=etag, file_size=file_size, last_modified=last_modified,
                should_process=False, skip_reason="unchanged",
            )

        # ── Already running on another worker ────────────────────────────────
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "RUNNING"
        ):
            dlog.debug(f"SKIP {file_name}  (RUNNING on another worker)")
            return CrawlResult(
                file_path=s3_uri, file_name=file_name, doc_id=doc_id,
                file_hash=etag, file_size=file_size, last_modified=last_modified,
                should_process=False, skip_reason="already_running",
            )

        # ── Permanently failed (max retries) ─────────────────────────────────
        if (
            not self.force_reingest
            and record is not None
            and record["status"] == "ERROR"
            and record.get("retry_count", 0) >= 3
        ):
            dlog.warning(
                f"SKIP {file_name}  "
                f"(ERROR × {record['retry_count']} retries — permanently skipped)"
            )
            return CrawlResult(
                file_path=s3_uri, file_name=file_name, doc_id=doc_id,
                file_hash=etag, file_size=file_size, last_modified=last_modified,
                should_process=False,
                skip_reason=f"max_retries_exceeded ({record['retry_count']})",
            )

        # ── Skip/ingest decision ──────────────────────────────────────────────
        if self.force_reingest:
            process, reason = True, "force_reingest"
        else:
            process, reason = should_ingest(s3_uri, etag)

        if not process:
            dlog.debug(f"SKIP {file_name}  reason={reason}")
            return CrawlResult(
                file_path=s3_uri, file_name=file_name, doc_id=doc_id,
                file_hash=etag, file_size=file_size, last_modified=last_modified,
                should_process=False, skip_reason=reason,
            )

        # ── Queue the file — download is deferred to the worker thread ────────
        dlog.info(
            f"QUEUE  {file_name}  "
            f"({file_size / 1024 / 1024:.1f} MB)  reason={reason}"
        )
        try:
            upsert_ingestion_log(
                file_path=s3_uri,
                file_name=file_name,
                file_hash=etag,
                file_size=file_size,
                last_modified=last_modified,
                status="RUNNING",
            )
        except Exception as e:
            dlog.error(f"Could not mark RUNNING: {e}")

        # local_path=None here — download happens in _process_doc_thread
        return CrawlResult(
            file_path=s3_uri,
            file_name=file_name,
            doc_id=doc_id,
            file_hash=etag,
            file_size=file_size,
            last_modified=last_modified,
            should_process=True,
            local_path=None,
        )

    def _update_stats(self, result: CrawlResult, obj: dict):
        self.stats.total_found += 1
        self.stats.total_size_mb += result.file_size / 1024 / 1024

        ext = Path(obj["Key"]).suffix.lower()
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
        logger.info(f"  S3 crawl complete")
        logger.info(f"  Bucket/prefix      : s3://{self.bucket}/{self.prefix}")
        logger.info(f"  Total objects found: {s.total_found:,}")
        logger.info(f"  To process         : {s.to_process:,}")
        logger.info(f"  Fast-skipped       : {s.skipped_unchanged:,}  "
                    f"(SUCCESS + ETag match — no download)")
        logger.info(f"  Download avoided   : {avoided_gb:.2f} GB  "
                    f"({s.bytes_avoided / 1024 / 1024:.0f} MB)")
        logger.info(f"  Other skipped      : {s.skipped_other:,}")
        logger.info(f"  Errors             : {s.errors:,}")
        logger.info(f"  Total size         : {s.total_size_mb:.1f} MB")
        for ext, count in sorted(s.files_by_ext.items()):
            logger.info(f"    {ext:<8} {count:>8,} objects")
        logger.info("─" * 60)
