from __future__ import annotations

from contextlib import contextmanager
import json
import random
import threading
import time
import uuid

from .diagnostics import ErrorDiagnostic
from .logging_utils import get_logger
from .store import PostgresStore

log = get_logger("mir_ai.store")


class ResilientPostgresStore(PostgresStore):
    """Postgres store hardened for long-running ingestion.

    Critical writes are idempotent and transient connection failures discard the
    broken pooled connection before a bounded retry. Streamed reads intentionally
    propagate a mid-stream connection failure to the document-level retry so the
    deterministic stage can restart from durable state rather than replaying a
    partially consumed server cursor inside the same iterator.
    """

    def __init__(self, settings, minconn=1, maxconn=None):
        self.settings = settings
        import psycopg2.pool
        if maxconn is None or int(maxconn) <= 0:
            configured = int(getattr(settings, "db_pool_maxconn", 0) or 0)
            maxconn = configured or max(8, settings.doc_workers * 3 + settings.page_workers + 4)
        maxconn = max(int(minconn), int(maxconn))
        self.pool = psycopg2.pool.ThreadedConnectionPool(
            minconn,
            maxconn,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=settings.db_connect_timeout_s,
            keepalives=1,
            keepalives_idle=settings.db_keepalives_idle_s,
            keepalives_interval=settings.db_keepalives_interval_s,
            keepalives_count=settings.db_keepalives_count,
            application_name="mir_ai_ingestion",
        )
        self.schema = self._ident(settings.db_schema)
        self._conn_slots = threading.BoundedSemaphore(maxconn)

    @staticmethod
    def _is_transient_db_error(exc: BaseException) -> bool:
        try:
            import psycopg2
            if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                return True
        except Exception:
            pass
        text = f"{type(exc).__name__}: {exc}".casefold()
        return any(marker in text for marker in (
            "ssl error: unexpected eof",
            "server closed the connection unexpectedly",
            "connection not open",
            "connection already closed",
            "terminating connection",
            "could not connect to server",
            "connection timed out",
            "connection reset by peer",
            "broken pipe",
        ))

    @contextmanager
    def conn(self):
        self._conn_slots.acquire()
        c = None
        broken = False
        try:
            c = self.pool.getconn()
            if getattr(c, "closed", 0):
                try:
                    self.pool.putconn(c, close=True)
                finally:
                    c = self.pool.getconn()
            yield c
        except Exception as exc:
            broken = self._is_transient_db_error(exc) or bool(getattr(c, "closed", 0))
            if c is not None:
                try:
                    c.rollback()
                except Exception:
                    broken = True
            raise
        finally:
            if c is not None:
                try:
                    self.pool.putconn(c, close=broken)
                except Exception:
                    try:
                        c.close()
                    except Exception:
                        pass
            self._conn_slots.release()

    def _retry(self, operation, fn):
        attempts = max(1, int(self.settings.db_max_retries))
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:
                if not self._is_transient_db_error(exc) or attempt >= attempts:
                    raise
                delay = min(30.0, self.settings.db_retry_base_seconds * (2 ** (attempt - 1))) + random.random()
                log.warning(
                    "PostgreSQL transient operation failure; retrying",
                    extra={
                        "service": "postgres", "operation": operation, "attempt": attempt,
                        "max_attempts": attempts, "error_class": type(exc).__name__,
                        "error_category": "DB_CONNECTION_LOST", "error_detail": str(exc)[:1000],
                        "retryable": True,
                    },
                )
                time.sleep(delay)

    def init_schema(self):
        self._retry("init_schema", lambda: super(ResilientPostgresStore, self).init_schema())
        self.ensure_observability_schema()

    def ensure_document(self, *args, **kwargs):
        return self._retry("ensure_document", lambda: super(ResilientPostgresStore, self).ensure_document(*args, **kwargs))

    def get_business_metadata(self, *args, **kwargs):
        return self._retry("get_business_metadata", lambda: super(ResilientPostgresStore, self).get_business_metadata(*args, **kwargs))

    def ensure_generation(self, *args, **kwargs):
        return self._retry("ensure_generation", lambda: super(ResilientPostgresStore, self).ensure_generation(*args, **kwargs))

    def claim_generation(self, gid, worker_id, lease_seconds, max_attempts=3):
        def op():
            s = self.schema
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(
                        f"""UPDATE {s}.mirai_generations SET
                            status='RUNNING',
                            worker_id=%s,
                            lease_expires_at=NOW()+(%s*INTERVAL '1 second'),
                            attempt_count=attempt_count + CASE
                                WHEN status='RUNNING' AND worker_id=%s THEN 0 ELSE 1 END,
                            error_message=NULL,
                            updated_at=NOW()
                        WHERE generation_id=%s
                          AND status NOT IN ('ACTIVE','SUPERSEDED')
                          AND (attempt_count < %s OR (status='RUNNING' AND worker_id=%s))
                          AND (worker_id IS NULL OR worker_id=%s OR lease_expires_at IS NULL OR lease_expires_at<NOW())""",
                        (worker_id, lease_seconds, worker_id, gid, max_attempts, worker_id, worker_id),
                    )
                    ok = cur.rowcount == 1
                c.commit()
            return ok
        return self._retry("claim_generation", op)

    def heartbeat(self, *args, **kwargs):
        return self._retry("heartbeat", lambda: super(ResilientPostgresStore, self).heartbeat(*args, **kwargs))

    def generation_progress(self, *args, **kwargs):
        return self._retry("generation_progress", lambda: super(ResilientPostgresStore, self).generation_progress(*args, **kwargs))

    def upsert_page(self, *args, **kwargs):
        return self._retry("upsert_page", lambda: super(ResilientPostgresStore, self).upsert_page(*args, **kwargs))

    def repeated_header_footer_signatures(self, *args, **kwargs):
        return self._retry("header_footer_signatures", lambda: super(ResilientPostgresStore, self).repeated_header_footer_signatures(*args, **kwargs))

    def active_source_state(self, *args, **kwargs):
        return self._retry("active_source_state", lambda: super(ResilientPostgresStore, self).active_source_state(*args, **kwargs))

    def prune_generation_chunks(self, *args, **kwargs):
        return self._retry("prune_generation_chunks", lambda: super(ResilientPostgresStore, self).prune_generation_chunks(*args, **kwargs))

    def generation_chunk_count(self, *args, **kwargs):
        return self._retry("generation_chunk_count", lambda: super(ResilientPostgresStore, self).generation_chunk_count(*args, **kwargs))

    def upsert_metadata(self, *args, **kwargs):
        return self._retry("upsert_metadata", lambda: super(ResilientPostgresStore, self).upsert_metadata(*args, **kwargs))

    def mark_failed(self, *args, **kwargs):
        return self._retry("mark_failed", lambda: super(ResilientPostgresStore, self).mark_failed(*args, **kwargs))

    def verify_schema(self):
        required = ["mirai_documents", "mirai_generations", "mirai_pages", "mirai_chunks", "mirai_metadata", "mirai_ingestion_runs", "mirai_ingestion_run_items", "mirai_ingestion_events", "mirai_ingestion_status"]
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    missing=[]
                    for name in required:
                        cur.execute("SELECT to_regclass(%s)", (f"{self.schema}.{name}",))
                        if cur.fetchone()[0] is None:
                            missing.append(name)
                c.rollback()
            if missing:
                raise RuntimeError(
                    f"MIR-AI schema is not initialized; missing tables: {', '.join(missing)}. "
                    "Run mir_ai_main.py --config <config> --init-db with an authorized schema owner before ingestion."
                )
            return True
        return self._retry("verify_schema", op)

    def ping(self):
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute("SELECT 1")
                    value = cur.fetchone()[0]
                c.rollback()
            return value == 1
        return self._retry("ping", op)

    def existing_embedded_chunk_ids(self, gid, chunks) -> set[str]:
        pairs = [(x.chunk_id, x.text) for x in chunks]
        if not pairs:
            return set()
        def op():
            ids = [p[0] for p in pairs]
            text_by_id = dict(pairs)
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(
                        f"SELECT chunk_id,chunk_text FROM {self.schema}.mirai_chunks "
                        f"WHERE generation_id=%s AND chunk_id = ANY(%s) AND embedding IS NOT NULL",
                        (gid, ids),
                    )
                    rows = cur.fetchall()
                c.rollback()
            return {cid for cid, text in rows if text_by_id.get(cid) == text}
        return self._retry("existing_embedded_chunk_ids", op)

    def insert_chunk_batch(self, chunks):
        if not chunks:
            return
        import psycopg2.extras
        rows = []
        for x in chunks:
            vector = None if x.embedding is None else "[" + ",".join(f"{float(v):.8g}" for v in x.embedding) + "]"
            rows.append((
                x.generation_id, x.chunk_id, x.doc_id, x.chunk_index, x.text,
                x.page_start, x.page_end, json.dumps(x.page_labels), json.dumps(x.section_path),
                x.content_types, x.source_url, json.dumps(x.table_bboxes), json.dumps(x.metadata), vector,
            ))
        sql = f"""INSERT INTO {self.schema}.mirai_chunks(
            generation_id,chunk_id,doc_id,chunk_index,chunk_text,page_start,page_end,page_labels,
            section_path,content_types,source_url,table_bboxes,metadata,embedding)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,%s::vector)
        ON CONFLICT(generation_id,chunk_id) DO UPDATE SET
            chunk_text=EXCLUDED.chunk_text,
            page_start=EXCLUDED.page_start,
            page_end=EXCLUDED.page_end,
            page_labels=EXCLUDED.page_labels,
            section_path=EXCLUDED.section_path,
            content_types=EXCLUDED.content_types,
            source_url=EXCLUDED.source_url,
            table_bboxes=EXCLUDED.table_bboxes,
            metadata=EXCLUDED.metadata,
            embedding=CASE
                WHEN {self.schema}.mirai_chunks.chunk_text=EXCLUDED.chunk_text AND EXCLUDED.embedding IS NULL
                THEN {self.schema}.mirai_chunks.embedding ELSE EXCLUDED.embedding END"""
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, sql, rows, page_size=min(100, len(rows)))
                c.commit()
        return self._retry("insert_chunk_batch", op)

    def activate_generation(self, gid, worker_id):
        def op():
            s = self.schema
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(
                        f"SELECT doc_id,status,worker_id FROM {s}.mirai_generations WHERE generation_id=%s FOR UPDATE",
                        (gid,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise KeyError(gid)
                    did, status, owner = row
                    if status == "ACTIVE":
                        cur.execute(f"UPDATE {s}.mirai_documents SET active_generation_id=%s,updated_at=NOW() WHERE doc_id=%s", (gid, did))
                        c.commit()
                        return True
                    if status != "RUNNING" or owner != worker_id:
                        raise RuntimeError("Cannot activate: generation is not owned RUNNING work")
                    cur.execute(f"UPDATE {s}.mirai_generations SET status='SUPERSEDED',updated_at=NOW() WHERE doc_id=%s AND status='ACTIVE' AND generation_id<>%s", (did, gid))
                    cur.execute(
                        f"UPDATE {s}.mirai_generations SET status='ACTIVE',stage='COMPLETE',completed_at=NOW(),worker_id=NULL,lease_expires_at=NULL,updated_at=NOW() WHERE generation_id=%s",
                        (gid,),
                    )
                    cur.execute(f"UPDATE {s}.mirai_documents SET active_generation_id=%s,updated_at=NOW() WHERE doc_id=%s", (gid, did))
                c.commit()
            return True
        return self._retry("activate_generation", op)

    # -------------------------- observability --------------------------
    def ensure_observability_schema(self):
        s = self.schema
        stmts = [
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_ingestion_runs(
                run_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_root TEXT,
                requested_limit INTEGER, selected_count INTEGER NOT NULL DEFAULT 0,
                issue_count INTEGER NOT NULL DEFAULT 0, processing_fingerprint TEXT,
                metadata_mode TEXT, status TEXT NOT NULL DEFAULT 'RUNNING',
                summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ)""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_ingestion_run_items(
                run_id TEXT NOT NULL REFERENCES {s}.mirai_ingestion_runs(run_id) ON DELETE CASCADE,
                logical_object_id TEXT NOT NULL, doc_id TEXT, generation_id TEXT,
                canonical_path TEXT, source_url TEXT, selected_format TEXT, selection_reason TEXT,
                status TEXT NOT NULL, stage TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                pages INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
                error_class TEXT, error_category TEXT, error_code TEXT, status_code INTEGER,
                request_id TEXT, retryable BOOLEAN, error_location TEXT, error_message TEXT,
                resolution_hint TEXT, root_cause_status TEXT,
                started_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ,
                PRIMARY KEY(run_id,logical_object_id))""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_ingestion_events(
                event_uid TEXT PRIMARY KEY, run_id TEXT, logical_object_id TEXT, doc_id TEXT,
                generation_id TEXT, event_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(), severity TEXT NOT NULL,
                event_type TEXT NOT NULL, stage TEXT, page_number INTEGER, chunk_id TEXT, attempt INTEGER,
                service TEXT, operation TEXT, error_class TEXT, error_category TEXT, error_code TEXT,
                status_code INTEGER, request_id TEXT, retryable BOOLEAN, error_location TEXT,
                message TEXT, resolution_hint TEXT, root_cause_status TEXT,
                details JSONB NOT NULL DEFAULT '{{}}'::jsonb)""",
            f"CREATE INDEX IF NOT EXISTS mirai_ingestion_events_run_idx ON {s}.mirai_ingestion_events(run_id,event_ts)",
            f"CREATE INDEX IF NOT EXISTS mirai_ingestion_events_doc_idx ON {s}.mirai_ingestion_events(doc_id,event_ts)",
            f"CREATE INDEX IF NOT EXISTS mirai_ingestion_items_status_idx ON {s}.mirai_ingestion_run_items(status,updated_at)",
            f"""CREATE OR REPLACE VIEW {s}.mirai_ingestion_status AS
                SELECT d.doc_id,d.canonical_path,d.source_url,d.active_generation_id,
                       g.generation_id,g.file_hash,g.source_version,g.processing_fingerprint,
                       CASE WHEN g.status='ACTIVE' THEN 'SUCCESS' ELSE g.status END AS status,
                       g.status AS generation_status,g.stage,g.attempt_count AS retry_count,
                       g.last_page_completed,g.pages_total,
                       (SELECT COUNT(*) FROM {s}.mirai_chunks c WHERE c.generation_id=g.generation_id) AS chunk_count,
                       g.error_message,g.started_at,g.updated_at,g.completed_at
                FROM {s}.mirai_documents d
                LEFT JOIN LATERAL (
                    SELECT * FROM {s}.mirai_generations x WHERE x.doc_id=d.doc_id
                    ORDER BY CASE WHEN x.generation_id=d.active_generation_id THEN 0 ELSE 1 END,
                             x.updated_at DESC LIMIT 1
                ) g ON TRUE""",
        ]
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    for stmt in stmts:
                        cur.execute(stmt)
                c.commit()
        return self._retry("ensure_observability_schema", op)

    def create_ingestion_run(self, run_id, source_type, source_root, requested_limit,
                             processing_fingerprint, metadata_mode, selected_count, issue_count):
        sql = f"""INSERT INTO {self.schema}.mirai_ingestion_runs(
            run_id,source_type,source_root,requested_limit,processing_fingerprint,metadata_mode,selected_count,issue_count,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING')
            ON CONFLICT(run_id) DO UPDATE SET updated_at=NOW()"""
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(sql, (run_id, source_type, source_root, requested_limit, processing_fingerprint, metadata_mode, selected_count, issue_count))
                c.commit()
        return self._retry("create_ingestion_run", op)

    def register_run_items(self, run_id, rows):
        if not rows: return
        import psycopg2.extras
        values = [(run_id, r.get("logical_object_id", ""), r.get("doc_id"), r.get("generation_id"),
                   r.get("canonical_path"), r.get("source_url"), r.get("selected_format"),
                   r.get("selection_reason"), r.get("status", "SELECTED"), r.get("stage"),
                   r.get("error_message"), r.get("error_category"), r.get("resolution_hint")) for r in rows]
        sql = f"""INSERT INTO {self.schema}.mirai_ingestion_run_items(
            run_id,logical_object_id,doc_id,generation_id,canonical_path,source_url,selected_format,selection_reason,status,stage,
            error_message,error_category,resolution_hint)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(run_id,logical_object_id) DO UPDATE SET
            canonical_path=EXCLUDED.canonical_path,source_url=EXCLUDED.source_url,
            selected_format=EXCLUDED.selected_format,selection_reason=EXCLUDED.selection_reason,
            status=EXCLUDED.status,stage=EXCLUDED.stage,error_message=EXCLUDED.error_message,
            error_category=EXCLUDED.error_category,resolution_hint=EXCLUDED.resolution_hint,updated_at=NOW()"""
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, sql, values, page_size=min(500, len(values)))
                c.commit()
        return self._retry("register_run_items", op)

    def update_run_item(self, run_id, logical_object_id, **values):
        allowed = {
            "doc_id", "generation_id", "status", "stage", "attempts", "pages", "chunks",
            "error_class", "error_category", "error_code", "status_code", "request_id", "retryable",
            "error_location", "error_message", "resolution_hint", "root_cause_status",
        }
        fields=[]; params=[]
        for key,value in values.items():
            if key in allowed:
                fields.append(f"{key}=%s"); params.append(value)
        if values.get("started"):
            fields.append("started_at=COALESCE(started_at,NOW())")
        if values.get("completed"):
            fields.append("completed_at=NOW()")
        fields.append("updated_at=NOW()")
        params.extend([run_id, logical_object_id])
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"UPDATE {self.schema}.mirai_ingestion_run_items SET {','.join(fields)} WHERE run_id=%s AND logical_object_id=%s", params)
                c.commit()
        return self._retry("update_run_item", op)

    def set_ingestion_run_status(self, run_id, status):
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"UPDATE {self.schema}.mirai_ingestion_runs SET status=%s,updated_at=NOW() WHERE run_id=%s", (status, run_id))
                c.commit()
        return self._retry("set_ingestion_run_status", op)

    def finish_ingestion_run(self, run_id):
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"SELECT status,COUNT(*) FROM {self.schema}.mirai_ingestion_run_items WHERE run_id=%s GROUP BY status", (run_id,))
                    counts = {str(k): int(v) for k,v in cur.fetchall()}
                    failed = sum(v for k,v in counts.items() if k in {"ERROR","SOURCE_NOT_FOUND","AMBIGUOUS_SOURCE","FORMAT_DISABLED","RIMDOCS_NOT_FOUND","PRECHECK_ERROR"})
                    status = "COMPLETED_WITH_ERRORS" if failed else "COMPLETED"
                    cur.execute(f"UPDATE {self.schema}.mirai_ingestion_runs SET status=%s,summary=%s::jsonb,completed_at=NOW(),updated_at=NOW() WHERE run_id=%s", (status, json.dumps(counts), run_id))
                c.commit()
            return counts
        return self._retry("finish_ingestion_run", op)

    def record_event(self, *, run_id="", logical_object_id="", doc_id="", generation_id="",
                     severity="INFO", event_type="EVENT", stage="", page_number=None, chunk_id="",
                     attempt=None, message="", diagnostic: ErrorDiagnostic | None = None,
                     service="", operation="", details=None, event_uid=None):
        d = diagnostic
        uid = event_uid or str(uuid.uuid4())
        params = (
            uid, run_id or None, logical_object_id or None, doc_id or None, generation_id or None,
            severity, event_type, stage or None, page_number, chunk_id or None, attempt,
            (d.service if d else service) or None, (d.operation if d else operation) or None,
            d.error_class if d else None, d.error_category if d else None, d.error_code if d else None,
            d.status_code if d else None, d.request_id if d else None, d.retryable if d else None,
            d.error_location if d else None, message or (d.observed_error if d else ""),
            d.resolution_hint if d else None, d.root_cause_status if d else None,
            json.dumps(details or {}, ensure_ascii=False, default=str),
        )
        sql = f"""INSERT INTO {self.schema}.mirai_ingestion_events(
            event_uid,run_id,logical_object_id,doc_id,generation_id,severity,event_type,stage,page_number,
            chunk_id,attempt,service,operation,error_class,error_category,error_code,status_code,request_id,
            retryable,error_location,message,resolution_hint,root_cause_status,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(event_uid) DO NOTHING"""
        try:
            def op():
                with self.conn() as c:
                    with c.cursor() as cur: cur.execute(sql, params)
                    c.commit()
            self._retry("record_event", op)
            return True
        except Exception as exc:
            log.error("Could not persist ingestion event", extra={
                "run_id": run_id, "doc_id": doc_id, "generation_id": generation_id,
                "error_class": type(exc).__name__, "error_detail": str(exc)[:1000],
                "service": "postgres", "operation": "record_event",
            })
            return False

    def get_pipeline_stats(self):
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"""SELECT status,COUNT(*),COALESCE(SUM(chunk_count),0),COALESCE(SUM(pages_total),0)
                        FROM {self.schema}.mirai_ingestion_status GROUP BY status ORDER BY status""")
                    rows=cur.fetchall()
                c.rollback()
            return {"by_status":[{"status":r[0],"doc_count":int(r[1]),"total_chunks":int(r[2]),"total_pages":int(r[3])} for r in rows]}
        return self._retry("get_pipeline_stats", op)

    def get_failed_docs(self, limit=100):
        """Return recent document/generation and source-selection failures.

        Source-selection failures do not have a generation yet, so they are read
        from run items in addition to the generation status view.
        """
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"""SELECT 'generation' AS failure_scope,doc_id,canonical_path,generation_id,status,stage,
                        retry_count,last_page_completed,pages_total,error_message,NULL::text AS error_category,
                        NULL::text AS error_code,NULL::integer AS status_code,NULL::text AS request_id,
                        NULL::text AS resolution_hint,updated_at
                        FROM {self.schema}.mirai_ingestion_status
                        WHERE status IN ('ERROR','PARTIAL')
                        UNION ALL
                        SELECT 'run_item',doc_id,canonical_path,generation_id,status,stage,attempts,NULL::integer,pages,
                        error_message,error_category,error_code,status_code,request_id,resolution_hint,updated_at
                        FROM {self.schema}.mirai_ingestion_run_items
                        WHERE status IN ('ERROR','SOURCE_NOT_FOUND','AMBIGUOUS_SOURCE','FORMAT_DISABLED','RIMDOCS_NOT_FOUND','PRECHECK_ERROR')
                        ORDER BY updated_at DESC LIMIT %s""", (int(limit),))
                    rows=cur.fetchall()
                c.rollback()
            keys=["failure_scope","doc_id","canonical_path","generation_id","status","stage","retry_count",
                  "last_page_completed","pages_total","error_message","error_category","error_code","status_code",
                  "request_id","resolution_hint","updated_at"]
            return [dict(zip(keys,r)) for r in rows]
        return self._retry("get_failed_docs", op)

    def get_run_status(self, run_id):
        def op():
            with self.conn() as c:
                with c.cursor() as cur:
                    cur.execute(f"SELECT run_id,source_type,source_root,requested_limit,selected_count,issue_count,status,summary,started_at,completed_at FROM {self.schema}.mirai_ingestion_runs WHERE run_id=%s", (run_id,))
                    run=cur.fetchone()
                    cur.execute(f"SELECT logical_object_id,doc_id,generation_id,canonical_path,selected_format,status,stage,attempts,pages,chunks,error_category,error_code,status_code,request_id,error_message,resolution_hint,updated_at FROM {self.schema}.mirai_ingestion_run_items WHERE run_id=%s ORDER BY logical_object_id", (run_id,))
                    items=cur.fetchall()
                c.rollback()
            if not run: return None
            rk=["run_id","source_type","source_root","requested_limit","selected_count","issue_count","status","summary","started_at","completed_at"]
            ik=["logical_object_id","doc_id","generation_id","canonical_path","selected_format","status","stage","attempts","pages","chunks","error_category","error_code","status_code","request_id","error_message","resolution_hint","updated_at"]
            return {"run":dict(zip(rk,run)),"items":[dict(zip(ik,x)) for x in items]}
        return self._retry("get_run_status", op)
