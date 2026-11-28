"""Optional, resumable native/English translation materialization.

The translator is injected explicitly: no implicit paid API calls, no
silent source mutation, and no translation used as evidence without its
source segment. PostgreSQL claims use SKIP LOCKED and leases for multi-host
workers; results are idempotent under a stable semantic fingerprint.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import threading
import time
from collections import deque

LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")


def translation_fingerprint(*, provider: str, model: str, glossary_version: str = "") -> str:
    if not provider or not model:
        raise ValueError("Translation provider and model are required")
    value = json.dumps(
        {"schema": 1, "provider": provider, "model": model,
         "glossary": glossary_version},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compose_language_input(native: str, english: str | None, *,
                           language: str, mode: str = "english") -> str:
    """Select embedding text without mutating the original content."""
    if mode not in ("english", "native", "bilingual"):
        raise ValueError("Unsupported embedding language mode")
    if not isinstance(native, str) or not isinstance(language, str):
        raise ValueError("Native text and language are required")
    if language and not LANGUAGE.fullmatch(language):
        raise ValueError("Invalid source language code")
    if mode == "native" or language.casefold().startswith("en"):
        return native
    if not english or not english.strip():
        raise ValueError("English translation is required for this mode")
    if mode == "english":
        return english
    return f"[Original | {language or 'und'}]\n{native}\n\n[English | en]\n{english}"


def create_translation_table(conn, *, schema: str = "public"):
    """Explicit DDL step; does not modify existing production chunk tables."""
    from profile_store import _safe_identifier
    qualified = _safe_identifier(schema) + ".rich_translation"
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {qualified} (
                segment_id text NOT NULL,
                source_sha256 text NOT NULL,
                translation_hash text NOT NULL,
                source_language text NOT NULL,
                target_language text NOT NULL DEFAULT 'en',
                native_text text NOT NULL,
                translated_text text,
                state text NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','leased','complete','failed')),
                attempts integer NOT NULL DEFAULT 0,
                lease_owner text,
                lease_until timestamptz,
                next_retry_at timestamptz NOT NULL DEFAULT now(),
                last_error text,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(segment_id,source_sha256,translation_hash)
            )
        """)
        cur.execute(f"""CREATE INDEX IF NOT EXISTS rich_translation_claim_idx
            ON {qualified}(state,next_retry_at,lease_until)
            WHERE state IN ('pending','leased','failed')""")
    conn.commit()


def enqueue_segments(conn, segments, *, translation_hash: str, schema="public",
                     batch_size: int = 256):
    """Batched, idempotent generator ingestion with O(batch_size) memory.

    The source digest includes language to prevent stale reuse when language
    identification changes while the original source text stays identical.
    A failed validation or DB write rolls back this invocation entirely.
    """
    from profile_store import _safe_identifier
    from psycopg2.extras import execute_values
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("Invalid translation enqueue batch size")
    if not isinstance(translation_hash,str) or not re.fullmatch(
            r"[0-9a-f]{64}",translation_hash):
        raise ValueError("Invalid translation semantic fingerprint")
    table = _safe_identifier(schema) + ".rich_translation"
    sql = f"""INSERT INTO {table}
        (segment_id,source_sha256,translation_hash,source_language,native_text)
        VALUES %s ON CONFLICT DO NOTHING"""
    inserted = 0
    batch = []
    try:
        with conn.cursor() as cur:
            for sid, native, language in segments:
                if (not isinstance(sid,str) or not 1 <= len(sid) <= 1024
                        or not isinstance(native,str) or not native.strip()
                        or len(native) > 32000):
                    raise ValueError("Invalid or oversized translation segment")
                if not isinstance(language,str) or not LANGUAGE.fullmatch(language):
                    raise ValueError("Invalid source language")
                source_hash=hashlib.sha256(
                    (language.casefold() + "\0" + native).encode("utf-8")
                ).hexdigest()
                batch.append((sid,source_hash,translation_hash,language,native))
                if len(batch)>=batch_size:
                    execute_values(cur,sql,batch,page_size=len(batch))
                    inserted+=cur.rowcount
                    batch.clear()
            if batch:
                execute_values(cur,sql,batch,page_size=len(batch))
                inserted+=cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return inserted


def claim_batch(conn, *, owner: str, batch_size=16, lease_seconds=300,
                max_attempts=5, schema="public"):
    """Atomically lease rows across processes/hosts; expired leases are recoverable."""
    from profile_store import _safe_identifier
    if not owner or not 1 <= batch_size <= 128 or not 30 <= lease_seconds <= 3600:
        raise ValueError("Invalid translation lease settings")
    table = _safe_identifier(schema) + ".rich_translation"
    with conn.cursor() as cur:
        cur.execute(f"""
            WITH picked AS (
                SELECT segment_id,source_sha256,translation_hash
                FROM {table}
                WHERE attempts < %s AND next_retry_at <= now() AND
                      (state IN ('pending','failed') OR (state='leased' AND lease_until < now()))
                ORDER BY next_retry_at,updated_at,segment_id
                FOR UPDATE SKIP LOCKED LIMIT %s
            )
            UPDATE {table} AS t SET state='leased',lease_owner=%s,
                lease_until=now()+(%s * interval '1 second'),
                attempts=t.attempts+1,updated_at=now()
            FROM picked p
            WHERE t.segment_id=p.segment_id AND t.source_sha256=p.source_sha256
              AND t.translation_hash=p.translation_hash
            RETURNING t.segment_id,t.source_sha256,t.translation_hash,
                      t.source_language,t.native_text,t.attempts
        """, (max_attempts,batch_size,owner,lease_seconds))
        rows=cur.fetchall()
    conn.commit()
    return rows


def finish_segment(conn, row, *, owner: str, english: str | None,
                   error: str = "", schema="public"):
    """Fencing: an expired/stolen lease cannot overwrite a newer worker."""
    from profile_store import _safe_identifier
    table = _safe_identifier(schema) + ".rich_translation"
    sid, source_sha, fingerprint = row[:3]
    if english is not None and (not isinstance(english, str) or not english.strip()):
        raise ValueError("Translation result must be nonempty")
    if len(row)<6 or type(row[5]) is not int or row[5]<1:
        raise ValueError("Missing translation attempt count")
    retry_delay = 0 if english is not None else min(300,2**min(row[5],8))
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {table} SET state=%s,translated_text=%s,last_error=%s,
                lease_owner=NULL,lease_until=NULL,updated_at=now(),
                next_retry_at=now()+(%s * interval '1 second')
            WHERE segment_id=%s AND source_sha256=%s AND translation_hash=%s
              AND state='leased' AND lease_owner=%s AND lease_until > now()
        """, ("complete" if english is not None else "failed",
              english, str(error)[:1000] if error else None, retry_delay,
              sid,source_sha,fingerprint,owner))
        changed=cur.rowcount
    conn.commit()
    return bool(changed)




def extend_lease(conn, row, *, owner: str, lease_seconds=300, schema="public") -> bool:
    """Heartbeat a translation claim without ever reviving an expired lease."""
    from profile_store import _safe_identifier
    if not owner or type(lease_seconds) is not int or not 30<=lease_seconds<=3600:
        raise ValueError("Invalid translation heartbeat")
    table=_safe_identifier(schema)+".rich_translation"
    sid,source_sha,fingerprint=row[:3]
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {table} SET lease_until=now()+(%s * interval '1 second'),
                updated_at=now()
            WHERE segment_id=%s AND source_sha256=%s AND translation_hash=%s
              AND state='leased' AND lease_owner=%s AND lease_until>now()
        """,(lease_seconds,sid,source_sha,fingerprint,owner))
        changed=cur.rowcount
    conn.commit()
    return bool(changed)


def translate_claimed(rows, translator, *, workers=8):
    """Parallelize remote translation; do not hold DB locks during RPCs.

    translator(text, source_language, target_language='en') -> str
    Results include original row identities for independently fenced commits.
    """
    if not 1 <= workers <= 64:
        raise ValueError("Translation workers must be 1..64")
    def work(row):
        try:
            value=translator(row[4],row[3],target_language="en")
            if not isinstance(value,str) or not value.strip():
                raise ValueError("Empty translation")
            return row,value,None
        except Exception as exc:
            return row,None,f"{type(exc).__name__}: {exc}"[:1000]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, rows))


def production_settings(value):
    """Small bounded local quota; allocate per host from the approved project quota."""
    expected={"model","rpm","max_chars","timeout_seconds","attempts"}
    if not isinstance(value,dict) or not expected<=set(value) or set(value)-expected-{"workers"} or value["model"] not in ("nmt","base"):
        raise ValueError("Invalid Google Translation configuration")
    for k,low,high in (("rpm",1,10000),("max_chars",100,30000),
                       ("timeout_seconds",5,600),("attempts",1,5)):
        if type(value[k]) is not int or not low<=value[k]<=high:
            raise ValueError("Invalid translation "+k)
    workers=value.get("workers",1)
    if type(workers) is not int or not 1<=workers<=16:
        raise ValueError("Invalid translation workers")
    return {**value,"workers":workers}


def _google_session(key_path):
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    credentials=service_account.Credentials.from_service_account_file(
        key_path,scopes=["https://www.googleapis.com/auth/cloud-translation"])
    return AuthorizedSession(credentials)


class GoogleTranslation:
    """Auto-detect source language, preserve source, publish English separately.

    No extra tables. A successfully published document skips on the same
    source/enrichment/translation identity. A crash before publication can
    repeat calls. Model/target are semantic, rate and batch limits operational.
    """
    def __init__(self,key_path,config,*,session=None):
        self.cfg=production_settings(config)
        self.key_path=key_path
        self.session=session
        if session is not None and self.cfg["workers"]!=1:
            raise ValueError("Injected translation session requires one worker")
        self.next_request=0.0
        self._rate_lock=threading.Lock()
        self.identity=translation_fingerprint(provider="google-translate-v2",
                                  model=self.cfg["model"],glossary_version="en-auto-source-v1")

    def _batch(self,values,*,session=None):
        if session is None:
            if self.session is None:
                self.session=_google_session(self.key_path)
            session=self.session
        for attempt in range(self.cfg["attempts"]):
            # All requests (including retries) share the coordinator pacing.
            with self._rate_lock:
                delay=max(0,self.next_request-time.monotonic())
                if delay:
                    time.sleep(delay)
                self.next_request=time.monotonic()+60/self.cfg["rpm"]
            response=session.post("https://translation.googleapis.com/language/translate/v2",
                json={"q":values,"target":"en","format":"text","model":self.cfg["model"]},
                timeout=self.cfg["timeout_seconds"])
            if response.status_code in (429,500,502,503,504) and attempt+1<self.cfg["attempts"]:
                with self._rate_lock:
                    self.next_request=max(self.next_request,time.monotonic()+min(60,2**attempt))
                continue
            response.raise_for_status()
            items=response.json()["data"]["translations"]
            if not isinstance(items,list) or len(items)!=len(values):
                raise ValueError("Translation response count mismatch")
            result=[]
            for item in items:
                lang=item.get("detectedSourceLanguage")
                text=item.get("translatedText")
                if not isinstance(lang,str) or not LANGUAGE.fullmatch(lang) or not isinstance(text,str) or not text.strip():
                    raise ValueError("Invalid translation response")
                result.append((lang,text))
            return result
        raise RuntimeError("Translation attempts exhausted")

    def _translated_batches(self,batches):
        """Keep at most workers requests in flight, in input order.

        A worker owns its own HTTP session. Failures propagate before the
        caller publishes anything; no unbounded executor submission.
        """
        if self.cfg["workers"]==1:
            for batch in batches:
                yield batch,self._batch(list(batch))
            return
        local=threading.local()
        sessions=[]
        lock=threading.Lock()
        def work(batch):
            if not hasattr(local,"session"):
                local.session=_google_session(self.key_path)
                with lock:
                    sessions.append(local.session)
            return self._batch(list(batch),session=local.session)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.cfg["workers"]) as pool:
                pending=deque()
                for batch in batches:
                    pending.append((batch,pool.submit(work,batch)))
                    if len(pending)>=self.cfg["workers"]:
                        first,future=pending.popleft()
                        yield first,future.result()
                while pending:
                    first,future=pending.popleft()
                    yield first,future.result()
        finally:
            for session in sessions:
                session.close()

    def _input_batches(self,rows,english_contract):
        """Exact text reuse within a bounded batch, independent row identities."""
        batch={}
        chars=count=0
        for i,row in enumerate(rows):
            if not english_contract and json.loads(row[13]).get("source_kind")=="visual_model":
                continue
            value=row[6]
            if len(value)>self.cfg["max_chars"]:
                raise ValueError("Translation segment exceeds configured character budget")
            added=0 if value in batch else len(value)
            if batch and (chars+added>self.cfg["max_chars"] or count>=64):
                yield batch
                batch={}
                chars=count=0
                added=len(value)
            batch.setdefault(value,[]).append(i)
            chars+=added
            count+=1
        if batch:
            yield batch

    def apply_rows(self,rows,profile,sources):
        from types import SimpleNamespace
        from reuse_materialize import compose_embedding_text
        english_contract=profile.get("output",{}).get("text_contract")=="english_v1"
        import tiktoken
        encoder=tiktoken.get_encoding(profile["chunking"].get("tokenizer","cl100k_base")) if english_contract else None
        result=list(rows)
        # Translation results are applied on the coordinator thread and only
        # returned once all bounded batches have succeeded.
        for batch,responses in self._translated_batches(self._input_batches(result,english_contract)):
            if len(responses)!=len(batch):
                raise ValueError("Translation response count mismatch")
            occurrences=((i,language,english) for indices,(language,english) in zip(batch.values(),responses)
                         for i in indices)
            for i,language,english in occurrences:
                row=result[i]
                meta=json.loads(row[13])
                meta["translation"]={"provider":"google-translate-v2","fingerprint":self.identity,
                                     "source_language":language,"target_language":"en",
                                     "source_text_sha256":hashlib.sha256(row[6].encode()).hexdigest(),
                                     "status":"ENGLISH_SOURCE" if language=="en" else "MACHINE_TRANSLATION_UNVERIFIED"}
                embedding=row[7]
                if english_contract:
                    # Use the target-language provider response even when the
                    # dominant source language is English (mixed-language text).
                    # No native filename, headers or metadata in English text.
                    if len(encoder.encode(english,disallowed_special=()))>profile["embedding"].get("input_max_tokens",6000):
                        raise ValueError("English translation exceeds configured embedding token budget")
                    model=meta.get("source_kind")=="visual_model"
                    if model:
                        meta["translation"]["status"]="MODEL_ENGLISH_UNVERIFIED"
                        native=None
                    else:
                        native=row[6]
                        if language!="en" or english!=native:
                            meta["translation"]["status"]="MACHINE_TRANSLATION_UNVERIFIED"
                    result[i]=row[:6]+(native,english,)+row[8:13]+(json.dumps(meta,ensure_ascii=False,sort_keys=True),)+row[14:]
                    continue
                if language!="en":
                    meta["translation"]["text"]=english
                    chunk=SimpleNamespace(chunk_text=english,section_title=row[10],file_name=row[5],
                           file_path=row[4],document_metadata=json.loads(row[12]),chunk_metadata=meta)
                    embedding=compose_embedding_text(chunk,profile["embedding"].get("input_fields",["chunk_text"]),
                          sources,max_tokens=profile["embedding"].get("input_max_tokens",6000),
                          tokenizer=profile["chunking"].get("tokenizer","cl100k_base"))
                result[i]=row[:7]+(embedding,)+row[8:13]+(json.dumps(meta,ensure_ascii=False,sort_keys=True),)+row[14:]
        return result
