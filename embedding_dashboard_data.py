"""Read-only PostgreSQL queries for the embedding operations dashboard.

No writes, source text, vectors, API keys, or embedding API calls.
Keep the dashboard independent from the ingestion runtime and its heavy imports.
"""
from __future__ import annotations

import configparser
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

SCHEMA = "embedding_runtime"
MAX_JOBS = 30
MAX_FAILURE_ROWS = 200

# Metadata was added after some jobs already started. Treat absent columns as
# nullable to make the dashboard work against both the old and new schema.
OPTIONAL_RUN_COLUMNS = {
    "source_schema": "text",
    "source_table": "text",
    "profile_name": "text",
    "model_name": "text",
    "max_chunks": "bigint",
    "last_started_at": "timestamptz",
}


def connect_readonly():
    """Connect using local config.ini, optionally overriding with a SELECT-only user."""
    config = configparser.ConfigParser()
    if not config.read(Path(__file__).resolve().with_name("config.ini")):
        raise ValueError("Missing config.ini in repository directory")
    if "POSTGRES" not in config:
        raise ValueError("Missing [POSTGRES] database configuration")
    database = config["POSTGRES"]
    return psycopg2.connect(
        host=os.environ.get("EMBED_DASHBOARD_DB_HOST", database["host"]),
        port=int(os.environ.get("EMBED_DASHBOARD_DB_PORT", database.get("port", "5432"))),
        dbname=os.environ.get("EMBED_DASHBOARD_DB_NAME", database["database"]),
        user=os.environ.get("EMBED_DASHBOARD_DB_USER", database["user"]),
        password=os.environ.get("EMBED_DASHBOARD_DB_PASSWORD", database["password"]),
        connect_timeout=5,
        # Defense in depth. A SELECT-only Postgres role is still recommended.
        options="-c default_transaction_read_only=on -c statement_timeout=12000",
        application_name="embedding-status-dashboard",
    )


def _rows(conn, query, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _table_exists(conn, table_name):
    return bool(_rows(conn, """SELECT to_regclass(%s) IS NOT NULL AS present""",
                      (f"{SCHEMA}.{table_name}",))[0]["present"])


def summarize_job(job, counts, active_claims=0):
    """Derive human-readable status without pretending a lease proves a live process."""
    buckets = {k: int(counts.get(k, 0)) for k in (
        "PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED")}
    total = sum(buckets.values())
    processed = buckets["SUCCESS"] + buckets["FAILED"] + buckets["SKIPPED"]
    discovery_done = bool(job.get("discovery_done"))
    if active_claims > 0:
        phase = "Active claims"
    elif not discovery_done:
        phase = "Discovering / awaiting worker"
    elif buckets["PENDING"] or buckets["RUNNING"]:
        phase = "Pending / no active lease"
    elif buckets["FAILED"]:
        phase = "Completed with failures"
    elif total:
        phase = "Completed"
    else:
        phase = "No tasks discovered"
    return {
        **buckets,
        "total": total,
        "processed": processed,
        "active_claims": int(active_claims),
        "progress": processed / total if total else 0.0,
        "phase": phase,
    }


def fetch_overview(job_limit=MAX_JOBS):
    """Read one bounded snapshot; counts are discovered tasks, not source-table totals."""
    job_limit = max(1, min(int(job_limit), MAX_JOBS))
    conn = connect_readonly()
    try:
        columns = _rows(conn, """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name='runs'
        """, (SCHEMA,))
        existing = {item["column_name"] for item in columns}
        extra = ", ".join(
            f"r.{name}" if name in existing else f"NULL::{kind} AS {name}"
            for name, kind in OPTIONAL_RUN_COLUMNS.items()
        )
        jobs = _rows(conn, f"""
            SELECT r.job_key, r.created_at, r.discovery_done, {extra}
            FROM embedding_runtime.runs r
            ORDER BY r.created_at DESC
            LIMIT %s
        """, (job_limit,))
        keys = [j["job_key"] for j in jobs]
        snapshot = {
            "at": datetime.now(timezone.utc),
            "jobs": jobs,
            "counts": {},
            "active": {},
            "quotas": [],
        }
        if not keys:
            return snapshot

        counts = _rows(conn, """
            SELECT job_key, status, count(*)::bigint AS amount
            FROM embedding_runtime.tasks
            WHERE job_key = ANY(%s)
            GROUP BY job_key, status
        """, (keys,))
        grouped = defaultdict(dict)
        for item in counts:
            grouped[item["job_key"]][item["status"]] = int(item["amount"])
        snapshot["counts"] = dict(grouped)

        claims = _rows(conn, """
            SELECT job_key, count(*)::bigint AS active_claims,
                   max(lease_until) AS latest_lease
            FROM embedding_runtime.tasks
            WHERE job_key=ANY(%s) AND status='RUNNING' AND lease_until>now()
            GROUP BY job_key
        """, (keys,))
        snapshot["active"] = {item["job_key"]: item for item in claims}

        if _table_exists(conn, "quota"):
            snapshot["quotas"] = _rows(conn, """
                SELECT deployment_key, rpm, tpm, cooldown, multiplier
                FROM embedding_runtime.quota
                ORDER BY deployment_key
                LIMIT 30
            """)
        return snapshot
    finally:
        conn.close()


def fetch_failures(job_key, limit=50):
    """Recent failed rows only; result sets and text field sizes are bounded."""
    limit = max(1, min(int(limit), MAX_FAILURE_ROWS))
    conn = connect_readonly()
    try:
        reasons = _rows(conn, """
            SELECT COALESCE(error_code,'UNKNOWN') AS reason, count(*)::bigint AS amount
            FROM embedding_runtime.tasks
            WHERE job_key=%s AND status='FAILED'
            GROUP BY COALESCE(error_code,'UNKNOWN')
            ORDER BY amount DESC, reason
        """, (job_key,))
        failures = _rows(conn, """
            SELECT row_id, attempts, error_code,
                   left(COALESCE(error_message,''),400) AS error_message,
                   left(COALESCE(processing_comment,''),200) AS processing_comment,
                   updated_at
            FROM embedding_runtime.tasks
            WHERE job_key=%s AND status='FAILED'
            ORDER BY updated_at DESC
            LIMIT %s
        """, (job_key,limit))
        return {"reasons": reasons, "rows": failures}
    finally:
        conn.close()


def fetch_failure_history(job_key, limit=50):
    """Optional append-only failure audit; no large unbounded log download."""
    limit = max(1, min(int(limit), MAX_FAILURE_ROWS))
    conn = connect_readonly()
    try:
        if not _table_exists(conn,"failure_events"):
            return []
        return _rows(conn, """
            SELECT row_id, error_code, left(COALESCE(error_message,''),400) AS error_message,
                   left(COALESCE(processing_comment,''),200) AS processing_comment,
                   occurred_at
            FROM embedding_runtime.failure_events
            WHERE job_key=%s
            ORDER BY occurred_at DESC
            LIMIT %s
        """, (job_key,limit))
    finally:
        conn.close()
