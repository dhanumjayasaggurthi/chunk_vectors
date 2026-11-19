"""Durable PostgreSQL page-range coordination for ingestion v2.

All operations are short transactions. A per-claim random token fences stale
workers (including workers that wake after their lease expires). An expired
lease can be reclaimed; a SUCCESS is never claimed again. No in-memory queue,
and no transaction is held while PDF extraction or artifact upload runs.

This module is opt-in. It never touches existing production ingestion tables.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

from .plan import page_ranges

HEX64 = re.compile(r"^[0-9a-f]{64}$")
RETRYABLE = {"40P01", "40001", "55P03"}
MIGRATION = Path(__file__).resolve().parent / "sql" / "001_work_ledger.sql"


def connect_database():
    """Use PG* environment variables, else the existing config.ini section.

    Avoid importing config.py because doing so reads unrelated production
    Azure/GCP settings. No credentials are echoed to terminal or SQL logs.
    """
    if os.environ.get("PGHOST") or os.environ.get("PGSERVICE"):
        return psycopg2.connect(connect_timeout=10, application_name="ingestion_v2")
    config = configparser.ConfigParser()
    config.read(Path.cwd() / "config.ini")
    if not config.has_section("POSTGRES"):
        raise ValueError("Set PGHOST/PGUSER/PGDATABASE or provide [POSTGRES] in config.ini")
    values = config["POSTGRES"]
    return psycopg2.connect(
        host=values.get("host", ""),
        port=values.getint("port", fallback=5432),
        dbname=values["database"],
        user=values["user"],
        password=values["password"],
        connect_timeout=10,
        application_name="ingestion_v2",
    )


@dataclass(frozen=True)
class Claim:
    job_key: str
    start_page: int
    end_page: int
    attempts: int
    owner: str
    token: str


class Ledger:
    def __init__(self, connect=connect_database, transaction_retries=4):
        if not 1 <= transaction_retries <= 10:
            raise ValueError("transaction_retries must be 1..10")
        self._connect = connect
        self._retries = transaction_retries

    def _tx(self, callback):
        """Retry only the transaction, never a PDF extraction or cloud upload."""
        for attempt in range(self._retries):
            conn = None
            try:
                conn = self._connect()
                with conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '2000ms'")
                        cursor.execute("SET LOCAL statement_timeout = '30000ms'")
                        return callback(cursor)
            except psycopg2.Error as error:
                if error.pgcode not in RETRYABLE or attempt + 1 >= self._retries:
                    raise
                time.sleep(min(1.5, 0.05 * (2**attempt)) + random.random()*0.05)
            finally:
                if conn is not None:
                    conn.close()
        raise AssertionError("Unreachable retry state")

    def create_schema(self):
        script = MIGRATION.read_text(encoding="utf-8")
        self._tx(lambda cur: cur.execute(script))

    def register(self, *, job_key, document_id, source_uri, source_sha256,
                 source_size, page_count, range_pages, policy):
        """Idempotent immutable job registration plus all ranges, atomically."""
        if not HEX64.fullmatch(job_key) or not HEX64.fullmatch(document_id):
            raise ValueError("job_key and document_id must be 64-character SHA256 hex")
        if not HEX64.fullmatch(source_sha256):
            raise ValueError("source_sha256 must be SHA256 hex")
        if not isinstance(source_uri, str) or not source_uri.strip():
            raise ValueError("source_uri required")
        if not isinstance(source_size, int) or source_size <= 0:
            raise ValueError("source_size must be a positive integer")
        if not isinstance(policy, dict):
            raise ValueError("policy must be a mapping")
        ranges = page_ranges(page_count, range_pages)
        if not ranges:
            raise ValueError("Cannot register an empty document")

        def apply(cur):
            cur.execute("""
              INSERT INTO ingestion_v2_runtime.jobs
                (job_key,document_id,source_uri,source_sha256,
                 source_size,page_count,range_pages,policy)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (job_key,document_id,source_uri,source_sha256,
                  source_size,page_count,range_pages,Json(policy)))
            cur.execute("""
              SELECT document_id,source_uri,source_sha256,source_size,
                     page_count,range_pages,policy
              FROM ingestion_v2_runtime.jobs WHERE job_key=%s FOR UPDATE
            """, (job_key,))
            existing = cur.fetchone()
            expected = (document_id,source_uri,source_sha256,source_size,
                        page_count,range_pages,policy)
            if existing != expected:
                raise ValueError("Job key already exists with different immutable source metadata")

            execute_values(cur, """
              INSERT INTO ingestion_v2_runtime.ranges(job_key,start_page,end_page)
              VALUES %s ON CONFLICT DO NOTHING
            """, [(job_key,start,end) for start,end in ranges],page_size=200)
            cur.execute("""
              SELECT start_page,end_page FROM ingestion_v2_runtime.ranges
              WHERE job_key=%s ORDER BY start_page
            """, (job_key,))
            if cur.fetchall() != ranges:
                raise ValueError("Registered page ranges do not match planned document coverage")
            return len(ranges)

        return self._tx(apply)

    def claim(self, job_key, owner, lease_seconds, max_attempts):
        """Claim one PENDING or expired RUNNING row without waiting on others."""
        if not owner or not 60 <= lease_seconds <= 3600:
            raise ValueError("Valid worker owner and lease_seconds 60..3600 required")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be 1..20")
        token = uuid.uuid4().hex

        def acquire(cur):
            cur.execute("""
                WITH selected AS (
                  SELECT job_key,start_page
                  FROM ingestion_v2_runtime.ranges
                  WHERE job_key=%s AND attempts < %s AND
                    ((status='PENDING' AND next_attempt_at<=clock_timestamp())
                     OR (status='RUNNING' AND lease_until<clock_timestamp()))
                  ORDER BY start_page
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE ingestion_v2_runtime.ranges AS r
                SET status='RUNNING',owner=%s,claim_token=%s,
                    attempts=r.attempts+1,
                    lease_until=clock_timestamp()+(%s * interval '1 second'),
                    error_code=NULL,error_message=NULL,updated_at=clock_timestamp()
                FROM selected
                WHERE r.job_key=selected.job_key AND r.start_page=selected.start_page
                RETURNING r.start_page,r.end_page,r.attempts
            """, (job_key,max_attempts,owner,token,lease_seconds))
            return cur.fetchone()

        row = self._tx(acquire)
        if row is None:
            return None
        return Claim(job_key,row[0],row[1],row[2],owner,token)

    def renew(self, claim, lease_seconds):
        """Nonblocking heartbeat; skip a lock held by a concurrent commit."""
        if not 60 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be 60..3600")

        def update(cur):
            cur.execute("""
                WITH unlocked AS (
                  SELECT job_key,start_page FROM ingestion_v2_runtime.ranges
                  WHERE job_key=%s AND start_page=%s AND status='RUNNING'
                    AND owner=%s AND claim_token=%s
                    AND lease_until>clock_timestamp()
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE ingestion_v2_runtime.ranges AS r
                SET lease_until=clock_timestamp()+(%s*interval '1 second'),
                    updated_at=clock_timestamp()
                FROM unlocked u
                WHERE r.job_key=u.job_key AND r.start_page=u.start_page
                  AND r.owner=%s AND r.claim_token=%s AND r.status='RUNNING'
                RETURNING r.start_page
            """, (claim.job_key,claim.start_page,claim.owner,claim.token,
                  lease_seconds,claim.owner,claim.token))
            return cur.fetchone() is not None
        return self._tx(update)

    def complete(self, claim, artifact_uri, artifact_sha256, ocr_candidates):
        """Publish one verified artifact URI only for the current live claim."""
        if not isinstance(artifact_uri,str) or not artifact_uri:
            raise ValueError("Artifact URI is required")
        if not HEX64.fullmatch(artifact_sha256):
            raise ValueError("Artifact sha256 is invalid")
        if not isinstance(ocr_candidates,int) or not 0 <= ocr_candidates <= claim.end_page-claim.start_page:
            raise ValueError("OCR candidates must be within the claimed range")

        def update(cur):
            cur.execute("""
                UPDATE ingestion_v2_runtime.ranges
                SET status='SUCCESS',artifact_uri=%s,artifact_sha256=%s,
                    ocr_candidates=%s,owner=NULL,claim_token=NULL,lease_until=NULL,
                    error_code=NULL,error_message=NULL,updated_at=clock_timestamp()
                WHERE job_key=%s AND start_page=%s AND status='RUNNING'
                  AND owner=%s AND claim_token=%s
                  AND lease_until>clock_timestamp()
                RETURNING start_page
            """, (artifact_uri,artifact_sha256,ocr_candidates,
                  claim.job_key,claim.start_page,claim.owner,claim.token))
            return cur.fetchone() is not None
        return self._tx(update)

    def fail(self, claim, code, message, *, max_attempts, retry_delay_seconds=30,
             permanent=False):
        """Record every failure; retry boundedly without poisoning other ranges."""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",code):
            raise ValueError("Failure code must be an uppercase machine-readable identifier")
        if not 1 <= max_attempts <= 20 or not 0 <= retry_delay_seconds <= 86400:
            raise ValueError("Invalid retry policy")
        safe_message = str(message or "")[:800]
        terminal = permanent or claim.attempts >= max_attempts
        status = "FAILED" if terminal else "PENDING"
        def update(cur):
            cur.execute("""
                UPDATE ingestion_v2_runtime.ranges
                SET status=%s,owner=NULL,claim_token=NULL,lease_until=NULL,
                    next_attempt_at=clock_timestamp()+(%s*interval '1 second'),
                    error_code=%s,error_message=%s,updated_at=clock_timestamp()
                WHERE job_key=%s AND start_page=%s AND status='RUNNING'
                  AND owner=%s AND claim_token=%s
                  AND lease_until>clock_timestamp()
                RETURNING attempts
            """, (status,retry_delay_seconds,code,safe_message,
                  claim.job_key,claim.start_page,claim.owner,claim.token))
            if cur.fetchone() is None:
                return None
            cur.execute("""
                INSERT INTO ingestion_v2_runtime.range_failures(
                  job_key,start_page,attempt,error_code,error_message,worker_id)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (claim.job_key,claim.start_page,claim.attempts,
                  code,safe_message,claim.owner))
            return status
        return self._tx(update)

    def expire_exhausted(self, job_key, max_attempts, batch_limit=100):
        """Classify abandoned final attempts as FAILED; never leave them RUNNING forever."""
        if not 1 <= max_attempts <= 20 or not 1 <= batch_limit <= 1000:
            raise ValueError("Invalid exhaustion scan limits")

        def recover(cur):
            cur.execute("""
              WITH selected AS (
                SELECT job_key,start_page FROM ingestion_v2_runtime.ranges
                WHERE job_key=%s AND status='RUNNING' AND attempts >=%s
                  AND lease_until<clock_timestamp()
                ORDER BY start_page FOR UPDATE SKIP LOCKED LIMIT %s
              ), updated AS (
                UPDATE ingestion_v2_runtime.ranges r
                SET status='FAILED',owner=NULL,claim_token=NULL,lease_until=NULL,
                    error_code='LEASE_EXHAUSTED',
                    error_message='Worker lease expired after the maximum attempts',
                    updated_at=clock_timestamp()
                FROM selected s
                WHERE r.job_key=s.job_key AND r.start_page=s.start_page
                RETURNING r.job_key,r.start_page,r.attempts
              )
              INSERT INTO ingestion_v2_runtime.range_failures(
                job_key,start_page,attempt,error_code,error_message)
              SELECT job_key,start_page,attempt,'LEASE_EXHAUSTED',
                     'Worker lease expired after the maximum attempts'
              FROM updated
              RETURNING start_page
            """, (job_key,max_attempts,batch_limit))
            return len(cur.fetchall())
        return self._tx(recover)

    def summary(self, job_key):
        def read(cur):
            cur.execute("""
              SELECT status,count(*)::bigint,coalesce(sum(ocr_candidates),0)::bigint
              FROM ingestion_v2_runtime.ranges
              WHERE job_key=%s GROUP BY status
            """,(job_key,))
            return {status:{"ranges":cnt,"ocr_candidates":ocr}
                    for status,cnt,ocr in cur.fetchall()}
        return self._tx(read)

    def listed_artifacts(self, job_key):
        """Read-only manifest for a later versioned publication stage."""
        def read(cur):
            cur.execute("""
              SELECT start_page,end_page,artifact_uri,artifact_sha256,ocr_candidates
              FROM ingestion_v2_runtime.ranges
              WHERE job_key=%s AND status='SUCCESS' ORDER BY start_page
            """,(job_key,))
            return cur.fetchall()
        return self._tx(read)
