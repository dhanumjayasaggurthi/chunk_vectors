from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import threading

from .models import PageRecord
from .settings import Settings


class PostgresStore:
    def __init__(self, settings: Settings, minconn=1, maxconn=8):
        self.settings = settings
        import psycopg2.pool
        self.pool = psycopg2.pool.ThreadedConnectionPool(
            minconn,
            maxconn,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
        self.schema = self._ident(settings.db_schema)
        self._conn_slots = threading.BoundedSemaphore(maxconn)

    @staticmethod
    def _ident(value):
        if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
            raise ValueError(f"Unsafe SQL identifier: {value!r}")
        return value

    @contextmanager
    def conn(self):
        self._conn_slots.acquire()
        c = None
        try:
            c = self.pool.getconn()
            yield c
        except Exception:
            if c:
                try:
                    c.rollback()
                except Exception:
                    pass
            raise
        finally:
            if c:
                try:
                    self.pool.putconn(c)
                except Exception:
                    pass
            self._conn_slots.release()

    def close(self):
        self.pool.closeall()

    def init_schema(self):
        s = self.schema
        d = self.settings.embedding_dim
        stmts = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            f"CREATE SCHEMA IF NOT EXISTS {s}",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_documents(
                doc_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL UNIQUE,
                source_url TEXT,
                active_generation_id TEXT,
                business_metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_generations(
                generation_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL REFERENCES {s}.mirai_documents(doc_id) ON DELETE CASCADE,
                file_hash TEXT NOT NULL,
                source_version TEXT,
                processing_fingerprint TEXT NOT NULL DEFAULT 'legacy',
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'DISCOVERED',
                worker_id TEXT,
                lease_expires_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_page_completed INTEGER NOT NULL DEFAULT 0,
                pages_total INTEGER,
                error_message TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ)""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_pages(
                generation_id TEXT NOT NULL REFERENCES {s}.mirai_generations(generation_id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                page_label TEXT,
                header_candidate TEXT,
                footer_candidate TEXT,
                native_text TEXT,
                search_text TEXT,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(generation_id,page_number))""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_chunks(
                generation_id TEXT NOT NULL REFERENCES {s}.mirai_generations(generation_id) ON DELETE CASCADE,
                chunk_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                page_labels JSONB NOT NULL,
                section_path JSONB NOT NULL,
                content_types TEXT[] NOT NULL,
                source_url TEXT,
                table_bboxes JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                embedding vector({d}),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(generation_id,chunk_id),
                UNIQUE(generation_id,chunk_index))""",
            f"""CREATE TABLE IF NOT EXISTS {s}.mirai_metadata(
                generation_id TEXT NOT NULL REFERENCES {s}.mirai_generations(generation_id) ON DELETE CASCADE,
                field_key TEXT NOT NULL,
                source_label TEXT NOT NULL,
                value JSONB,
                raw_value JSONB,
                unit TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_text TEXT,
                evidence_pages INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
                confidence DOUBLE PRECISION,
                normalization_status TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(generation_id,field_key))""",
            f"ALTER TABLE {s}.mirai_generations ADD COLUMN IF NOT EXISTS processing_fingerprint TEXT NOT NULL DEFAULT 'legacy'",
            f"ALTER TABLE {s}.mirai_generations ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
            f"ALTER TABLE {s}.mirai_pages ADD COLUMN IF NOT EXISTS search_text TEXT",
            f"ALTER TABLE {s}.mirai_generations DROP CONSTRAINT IF EXISTS mirai_generations_doc_id_file_hash_key",
            f"CREATE UNIQUE INDEX IF NOT EXISTS mirai_generations_doc_file_profile_uidx ON {s}.mirai_generations(doc_id,file_hash,processing_fingerprint)",
            f"CREATE INDEX IF NOT EXISTS mirai_generations_doc_idx ON {s}.mirai_generations(doc_id,status)",
            f"CREATE INDEX IF NOT EXISTS mirai_pages_gen_idx ON {s}.mirai_pages(generation_id,page_number)",
            f"CREATE INDEX IF NOT EXISTS mirai_chunks_doc_idx ON {s}.mirai_chunks(doc_id,generation_id)",
        ]
        with self.conn() as conn:
            with conn.cursor() as cur:
                for statement in stmts:
                    cur.execute(statement)
                if self.settings.vector_index_mode == "halfvec_hnsw":
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS mirai_chunks_embedding_hnsw "
                        f"ON {s}.mirai_chunks USING hnsw "
                        f"((embedding::halfvec({d})) halfvec_cosine_ops) WHERE embedding IS NOT NULL"
                    )
            conn.commit()

    @staticmethod
    def doc_id(path):
        return "doc-" + hashlib.sha256(path.encode()).hexdigest()

    @staticmethod
    def generation_id(doc_id, file_hash, processing_fingerprint="legacy"):
        material = f"{doc_id}:{file_hash}:{processing_fingerprint}"
        return "gen-" + hashlib.sha256(material.encode()).hexdigest()

    def ensure_document(self, path, source_url="", business_metadata=None):
        did = self.doc_id(path)
        s = self.schema
        with self.conn() as c:
            with c.cursor() as cur:
                if business_metadata is None:
                    cur.execute(
                        f"""INSERT INTO {s}.mirai_documents(doc_id,canonical_path,source_url,business_metadata)
                        VALUES (%s,%s,%s,'{{}}'::jsonb)
                        ON CONFLICT(doc_id) DO UPDATE SET
                            source_url=EXCLUDED.source_url,
                            updated_at=NOW()""",
                        (did, path, source_url),
                    )
                else:
                    cur.execute(
                        f"""INSERT INTO {s}.mirai_documents(doc_id,canonical_path,source_url,business_metadata)
                        VALUES (%s,%s,%s,%s::jsonb)
                        ON CONFLICT(doc_id) DO UPDATE SET
                            source_url=EXCLUDED.source_url,
                            business_metadata=EXCLUDED.business_metadata,
                            updated_at=NOW()""",
                        (did, path, source_url, json.dumps(business_metadata, ensure_ascii=False)),
                    )
            c.commit()
        return did

    def get_business_metadata(self, doc_id):
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"SELECT business_metadata FROM {self.schema}.mirai_documents WHERE doc_id=%s",
                    (doc_id,),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            return {}
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])

    def ensure_generation(self, doc_id, file_hash, source_version=None, processing_fingerprint="legacy"):
        gid = self.generation_id(doc_id, file_hash, processing_fingerprint)
        s = self.schema
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {s}.mirai_generations(
                        generation_id,doc_id,file_hash,source_version,processing_fingerprint,status)
                    VALUES (%s,%s,%s,%s,%s,'PENDING')
                    ON CONFLICT(generation_id) DO UPDATE SET
                        source_version=COALESCE(EXCLUDED.source_version,{s}.mirai_generations.source_version)""",
                    (gid, doc_id, file_hash, source_version, processing_fingerprint),
                )
            c.commit()
        return gid

    def claim_generation(self, gid, worker_id, lease_seconds, max_attempts=3):
        s = self.schema
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"""UPDATE {s}.mirai_generations SET
                        status='RUNNING',
                        worker_id=%s,
                        lease_expires_at=NOW()+(%s*INTERVAL '1 second'),
                        attempt_count=attempt_count+1,
                        error_message=NULL,
                        updated_at=NOW()
                    WHERE generation_id=%s
                      AND status NOT IN ('ACTIVE','SUPERSEDED')
                      AND attempt_count < %s
                      AND (worker_id IS NULL OR worker_id=%s OR lease_expires_at IS NULL OR lease_expires_at<NOW())""",
                    (worker_id, lease_seconds, gid, max_attempts, worker_id),
                )
                ok = cur.rowcount == 1
            c.commit()
        return ok

    def heartbeat(
        self,
        gid,
        worker_id,
        lease_seconds,
        stage=None,
        last_page=None,
        pages_total=None,
    ):
        s = self.schema
        fields = ["lease_expires_at=NOW()+(%s*INTERVAL '1 second')", "updated_at=NOW()"]
        params = [lease_seconds]
        if stage is not None:
            fields.append("stage=%s")
            params.append(stage)
        if last_page is not None:
            fields.append("last_page_completed=GREATEST(last_page_completed,%s)")
            params.append(last_page)
        if pages_total is not None:
            fields.append("pages_total=%s")
            params.append(pages_total)
        params.extend([gid, worker_id])
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"UPDATE {s}.mirai_generations SET {','.join(fields)} "
                    f"WHERE generation_id=%s AND worker_id=%s AND status='RUNNING'",
                    params,
                )
                ok = cur.rowcount == 1
            c.commit()
        return ok

    def generation_progress(self, gid):
        keys = [
            "status", "stage", "last_page_completed", "pages_total", "worker_id",
            "lease_expires_at", "attempt_count", "processing_fingerprint",
        ]
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"SELECT status,stage,last_page_completed,pages_total,worker_id,lease_expires_at,"
                    f"attempt_count,processing_fingerprint FROM {self.schema}.mirai_generations "
                    f"WHERE generation_id=%s",
                    (gid,),
                )
                row = cur.fetchone()
        if not row:
            raise KeyError(gid)
        return dict(zip(keys, row))

    @staticmethod
    def _page_search_text(page):
        parts = []
        seen = set()
        for text in [page.native_text] + [e.text for e in page.elements if e.text]:
            value = (text or "").strip()
            if not value:
                continue
            key = " ".join(value.casefold().split())
            if key in seen:
                continue
            seen.add(key)
            parts.append(value)
        return "\n\n".join(parts)

    def upsert_page(self, gid, page):
        payload = json.dumps(page.to_dict(), ensure_ascii=False)
        search_text = self._page_search_text(page)
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {self.schema}.mirai_pages(
                        generation_id,page_number,page_label,header_candidate,footer_candidate,
                        native_text,search_text,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(generation_id,page_number) DO UPDATE SET
                        page_label=EXCLUDED.page_label,
                        header_candidate=EXCLUDED.header_candidate,
                        footer_candidate=EXCLUDED.footer_candidate,
                        native_text=EXCLUDED.native_text,
                        search_text=EXCLUDED.search_text,
                        payload=EXCLUDED.payload,
                        updated_at=NOW()""",
                    (
                        gid,
                        page.page_number,
                        page.page_label,
                        page.header_candidate,
                        page.footer_candidate,
                        page.native_text,
                        search_text,
                        payload,
                    ),
                )
            c.commit()

    def iter_pages(self, gid, start_page=1):
        with self.conn() as c:
            with c.cursor(name="mirai_pages_cursor") as cur:
                cur.itersize = 50
                cur.execute(
                    f"SELECT payload FROM {self.schema}.mirai_pages "
                    f"WHERE generation_id=%s AND page_number>=%s ORDER BY page_number",
                    (gid, start_page),
                )
                for (payload,) in cur:
                    yield PageRecord.from_dict(json.loads(payload) if isinstance(payload, str) else payload)

    def repeated_header_footer_signatures(self, gid, min_fraction=.5, min_pages=3):
        s = self.schema
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {s}.mirai_pages WHERE generation_id=%s", (gid,))
                total = int(cur.fetchone()[0])
                if total < min_pages:
                    return set(), set()
                threshold = max(min_pages, int(total * min_fraction + .999))
                norm = "regexp_replace(regexp_replace(lower(trim({col})), '\\s+', ' ', 'g'), '\\d+', '#', 'g')"
                hs = norm.format(col="header_candidate")
                fs = norm.format(col="footer_candidate")
                cur.execute(
                    f"SELECT {hs} sig FROM {s}.mirai_pages WHERE generation_id=%s "
                    f"AND COALESCE(header_candidate,'')<>'' GROUP BY sig HAVING COUNT(*) >= %s",
                    (gid, threshold),
                )
                headers = {r[0] for r in cur.fetchall()}
                cur.execute(
                    f"SELECT {fs} sig FROM {s}.mirai_pages WHERE generation_id=%s "
                    f"AND COALESCE(footer_candidate,'')<>'' GROUP BY sig HAVING COUNT(*) >= %s",
                    (gid, threshold),
                )
                footers = {r[0] for r in cur.fetchall()}
        return headers, footers

    def iter_metadata_candidate_pages(self, gid, fallback_pages=20):
        s = self.schema
        terms = ["%summary%", "%study administration%", "%protocol%", "%report approval%"]
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"SELECT EXISTS(SELECT 1 FROM {s}.mirai_pages WHERE generation_id=%s AND "
                    f"(COALESCE(search_text,native_text,'') ILIKE %s OR "
                    f" COALESCE(search_text,native_text,'') ILIKE %s OR "
                    f" COALESCE(search_text,native_text,'') ILIKE %s OR "
                    f" COALESCE(search_text,native_text,'') ILIKE %s))",
                    (gid, *terms),
                )
                has_sections = bool(cur.fetchone()[0])
            with c.cursor(name="mirai_metadata_pages_cursor") as cur:
                cur.itersize = 20
                if has_sections:
                    cur.execute(
                        f"""WITH matches AS (
                            SELECT page_number FROM {s}.mirai_pages WHERE generation_id=%s AND
                            (COALESCE(search_text,native_text,'') ILIKE %s OR
                             COALESCE(search_text,native_text,'') ILIKE %s OR
                             COALESCE(search_text,native_text,'') ILIKE %s OR
                             COALESCE(search_text,native_text,'') ILIKE %s)
                        ), wanted AS (
                            SELECT DISTINCT p.page_number FROM {s}.mirai_pages p JOIN matches m
                            ON p.page_number BETWEEN m.page_number-1 AND m.page_number+1
                            WHERE p.generation_id=%s
                        )
                        SELECT p.payload FROM {s}.mirai_pages p JOIN wanted w USING(page_number)
                        WHERE p.generation_id=%s ORDER BY p.page_number""",
                        (gid, *terms, gid, gid),
                    )
                else:
                    cur.execute(
                        f"SELECT payload FROM {s}.mirai_pages WHERE generation_id=%s "
                        f"ORDER BY page_number LIMIT %s",
                        (gid, fallback_pages),
                    )
                for (payload,) in cur:
                    yield PageRecord.from_dict(json.loads(payload) if isinstance(payload, str) else payload)

    def active_source_state(self, path):
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"""SELECT g.generation_id,g.source_version,g.processing_fingerprint,g.pages_total,
                        (SELECT COUNT(*) FROM {self.schema}.mirai_chunks c WHERE c.generation_id=g.generation_id)
                    FROM {self.schema}.mirai_documents d
                    JOIN {self.schema}.mirai_generations g ON g.generation_id=d.active_generation_id
                    WHERE d.canonical_path=%s AND g.status='ACTIVE'""",
                    (path,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return dict(zip(
            ["generation_id", "source_version", "processing_fingerprint", "pages_total", "chunk_count"],
            row,
        ))

    def active_source_version(self, path):
        state = self.active_source_state(path)
        return state["source_version"] if state else None

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
            embedding=EXCLUDED.embedding"""
        with self.conn() as c:
            with c.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, rows, page_size=min(100, len(rows)))
            c.commit()

    def prune_generation_chunks(self, gid, keep_count):
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self.schema}.mirai_chunks WHERE generation_id=%s AND chunk_index >= %s",
                    (gid, keep_count),
                )
            c.commit()

    def generation_chunk_count(self, gid):
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {self.schema}.mirai_chunks WHERE generation_id=%s",
                    (gid,),
                )
                return int(cur.fetchone()[0])

    def upsert_metadata(self, gid, values):
        with self.conn() as c:
            with c.cursor() as cur:
                for m in values:
                    cur.execute(
                        f"""INSERT INTO {self.schema}.mirai_metadata(
                            generation_id,field_key,source_label,value,raw_value,unit,source,status,
                            evidence_text,evidence_pages,confidence,normalization_status)
                        VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(generation_id,field_key) DO UPDATE SET
                            value=EXCLUDED.value,
                            raw_value=EXCLUDED.raw_value,
                            unit=EXCLUDED.unit,
                            source=EXCLUDED.source,
                            status=EXCLUDED.status,
                            evidence_text=EXCLUDED.evidence_text,
                            evidence_pages=EXCLUDED.evidence_pages,
                            confidence=EXCLUDED.confidence,
                            normalization_status=EXCLUDED.normalization_status,
                            updated_at=NOW()""",
                        (gid, m.key, m.source_label, json.dumps(m.value, ensure_ascii=False),
                         json.dumps(m.raw_value, ensure_ascii=False), m.unit, m.source, m.status,
                         m.evidence_text, m.evidence_pages, m.confidence, m.normalization_status),
                    )
            c.commit()

    def search_chunks(self, query_vector, top_k=10, doc_id=None):
        if len(query_vector) != self.settings.embedding_dim:
            raise ValueError(f"Expected query vector dimension {self.settings.embedding_dim}")
        q = "[" + ",".join(f"{float(v):.8g}" for v in query_vector) + "]"
        d = self.settings.embedding_dim
        where = ["g.status='ACTIVE'", "c.embedding IS NOT NULL"]
        filters = []
        if doc_id:
            where.append("c.doc_id=%s")
            filters.append(doc_id)
        distance = (
            f"c.embedding::halfvec({d}) <=> %s::halfvec({d})"
            if self.settings.vector_index_mode == "halfvec_hnsw"
            else "c.embedding <=> %s::vector"
        )
        sql = f"""SELECT c.chunk_id,c.doc_id,c.chunk_index,c.chunk_text,c.page_start,c.page_end,
            c.page_labels,c.section_path,c.content_types,c.source_url,c.table_bboxes,c.metadata,
            1-({distance}) AS similarity
        FROM {self.schema}.mirai_chunks c
        JOIN {self.schema}.mirai_generations g ON g.generation_id=c.generation_id
        WHERE {' AND '.join(where)} ORDER BY {distance} LIMIT %s"""
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(sql, [q] + filters + [q, int(top_k)])
                rows = cur.fetchall()
        keys = [
            "chunk_id", "doc_id", "chunk_index", "chunk_text", "page_start", "page_end",
            "page_labels", "section_path", "content_types", "source_url", "table_bboxes", "metadata",
            "similarity",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def mark_failed(self, gid, worker_id, error):
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"""UPDATE {self.schema}.mirai_generations SET
                        status='ERROR',error_message=%s,worker_id=NULL,lease_expires_at=NULL,updated_at=NOW()
                    WHERE generation_id=%s AND worker_id=%s""",
                    (error[:4000], gid, worker_id),
                )
            c.commit()

    def activate_generation(self, gid, worker_id):
        s = self.schema
        with self.conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    f"SELECT doc_id FROM {s}.mirai_generations "
                    f"WHERE generation_id=%s AND worker_id=%s AND status='RUNNING' FOR UPDATE",
                    (gid, worker_id),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Cannot activate: generation is not owned RUNNING work")
                did = row[0]
                cur.execute(
                    f"UPDATE {s}.mirai_generations SET status='SUPERSEDED',updated_at=NOW() "
                    f"WHERE doc_id=%s AND status='ACTIVE'",
                    (did,),
                )
                cur.execute(
                    f"""UPDATE {s}.mirai_generations SET
                        status='ACTIVE',stage='COMPLETE',completed_at=NOW(),worker_id=NULL,
                        lease_expires_at=NULL,updated_at=NOW() WHERE generation_id=%s""",
                    (gid,),
                )
                cur.execute(
                    f"UPDATE {s}.mirai_documents SET active_generation_id=%s,updated_at=NOW() WHERE doc_id=%s",
                    (gid, did),
                )
            c.commit()
