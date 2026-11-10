"""Distributed, resumable PostgreSQL-to-Azure embedding backfill.

One poisoned row cannot block its neighbors. All execution state and failure
reasons are durable. Run identical CLI workers on Linux or Windows.
"""
from __future__ import annotations
import concurrent.futures
import configparser
import hashlib
import json
import logging
import math
import random
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
import tiktoken
from config import POSTGRES
from profile_store import _safe_identifier

LOG = logging.getLogger("epod.fast_embeddings")
POOL = None
DDL = """
CREATE SCHEMA IF NOT EXISTS embedding_runtime;
CREATE TABLE IF NOT EXISTS embedding_runtime.runs(
 job_key text PRIMARY KEY, cursor_id text, discovery_done boolean DEFAULT false,
 created_at timestamptz DEFAULT now(),
 source_schema text, source_table text, profile_name text, model_name text,
 max_chunks bigint, last_started_at timestamptz);
-- Additive migration for existing runs from earlier deployment versions.
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS source_schema text;
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS source_table text;
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS profile_name text;
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS model_name text;
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS max_chunks bigint;
ALTER TABLE embedding_runtime.runs ADD COLUMN IF NOT EXISTS last_started_at timestamptz;
CREATE TABLE IF NOT EXISTS embedding_runtime.tasks(
 job_key text NOT NULL REFERENCES embedding_runtime.runs(job_key),
 row_id text NOT NULL, status text NOT NULL DEFAULT 'PENDING',
 owner text, claim_token text, lease_until timestamptz,
 attempts int NOT NULL DEFAULT 0, error_code text, error_message text,
 processing_comment text, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(job_key,row_id));
CREATE INDEX IF NOT EXISTS embed_claim_idx ON embedding_runtime.tasks
 (job_key,status,lease_until,row_id);
CREATE TABLE IF NOT EXISTS embedding_runtime.failure_events(
 event_id bigserial PRIMARY KEY,
 job_key text NOT NULL,
 row_id text NOT NULL,
 error_code text NOT NULL,
 error_message text,
 processing_comment text,
 occurred_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS embed_failure_events_idx ON embedding_runtime.failure_events
 (job_key,row_id,occurred_at);
CREATE TABLE IF NOT EXISTS embedding_runtime.quota(
 deployment_key text PRIMARY KEY, rpm int NOT NULL, tpm int NOT NULL,
 requests float8 NOT NULL, tokens float8 NOT NULL,
 last_refill timestamptz NOT NULL, cooldown timestamptz, multiplier float8 DEFAULT 1);
"""

class ApiError(Exception):
    def __init__(self, message, status=0, delay=1):
        super().__init__(message)
        self.status, self.delay = status, delay

class FatalApiError(Exception):
    pass

def db():
    return POOL.getconn()

def release(conn):
    try:
        if not conn.closed:
            conn.rollback()
    finally:
        POOL.putconn(conn, close=bool(conn.closed))

def txn(sql, params=(), fetch=False):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = cur.fetchall() if fetch else cur.rowcount
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)

def identity(profile, azure, max_chunks):
    s, e = profile["existing_chunks"], profile["embedding"]
    identity = {k:s.get(k) for k in ("schema","table","id_column","text_column","vector_column")}
    identity.update(model=e.get("model",azure.get("model")), deployment=e.get("deployment",azure["deployment"]),
                    dimensions=e.get("dimensions",int(azure.get("dimensions","1536"))),
                    endpoint=azure["api_base"].rstrip("/"),reuse=e["reuse_existing"],limit=max_chunks)
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()

def validate(profile, azure, max_chunks):
    src, emb = profile["existing_chunks"], profile["embedding"]
    for key in ("schema","table","id_column","text_column","vector_column"):
        if not src.get(key):
            raise ValueError("Missing existing_chunks."+key)
        _safe_identifier(src[key])
    if src.get("note_column"):
        _safe_identifier(src["note_column"])
    table = _safe_identifier(src["schema"])+"."+_safe_identifier(src["table"])
    id_col, txt_col, vec_col = (_safe_identifier(src[x]) for x in
                                ("id_column","text_column","vector_column"))
    dimensions = int(emb.get("dimensions",azure.get("dimensions","1536")))
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT attname,format_type(atttypid,NULL),atttypmod
              FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped""",(table,))
            columns={r[0]:(r[1],r[2]) for r in cur.fetchall()}
            for col in (src["id_column"],src["text_column"],src["vector_column"]):
                if col not in columns:
                    raise ValueError("Source column missing: "+col)
            if columns[src["vector_column"]] != ("vector",dimensions):
                raise ValueError("pgvector type/dimension mismatch for "+vec_col)
            idtype=columns[src["id_column"]][0]
            if idtype not in ("uuid","integer","bigint","smallint","text","character varying"):
                raise ValueError("Unsupported row ID type: "+idtype)
            if columns[src["text_column"]][0] not in ("text","character varying"):
                raise ValueError("Source text must be text/varchar")
            if src.get("note_column"):
                if columns.get(src["note_column"],(None,))[0] not in ("text","character varying"):
                    raise ValueError("Note column missing or not text")
            cur.execute("""SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_attribute a
               ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
               WHERE i.indrelid=%s::regclass AND i.indisunique AND i.indisvalid
                AND i.indnkeyatts=1 AND a.attname=%s)""",(table,src["id_column"]))
            if not cur.fetchone()[0]:
                raise ValueError("Row ID must have a unique single-column index; doc_id may not be unique")
            cur.execute(f"SELECT 1 FROM {table} WHERE {id_col} IS NULL LIMIT 1")
            if cur.fetchone():
                raise ValueError("Null row identifiers are unsupported")
    finally:
        release(conn)
    return dict(table=table,id=id_col,text=txt_col,vec=vec_col,
                note=_safe_identifier(src["note_column"]) if src.get("note_column") else None,
                note_limit=max(1,min(400,columns[src["note_column"]][1]-4))
                    if src.get("note_column") and columns[src["note_column"]][1]>=4 else 400,
                idtype=idtype,dim=dimensions)

def initialize(job, quota_key, rpm, tpm, batch_tokens, metadata=None):
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            metadata = metadata or {}
            cur.execute("""INSERT INTO embedding_runtime.runs(
                job_key,source_schema,source_table,profile_name,model_name,max_chunks,last_started_at)
                VALUES (%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (job_key) DO UPDATE SET
                source_schema=COALESCE(EXCLUDED.source_schema,embedding_runtime.runs.source_schema),
                source_table=COALESCE(EXCLUDED.source_table,embedding_runtime.runs.source_table),
                profile_name=COALESCE(EXCLUDED.profile_name,embedding_runtime.runs.profile_name),
                model_name=COALESCE(EXCLUDED.model_name,embedding_runtime.runs.model_name),
                last_started_at=now()""",
                (job,metadata.get("source_schema"),metadata.get("source_table"),
                 metadata.get("profile_name"),metadata.get("model_name"),
                 metadata.get("max_chunks")))
            cur.execute("""INSERT INTO embedding_runtime.quota
              (deployment_key,rpm,tpm,requests,tokens,last_refill)
              VALUES(%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING""",
              (quota_key,rpm,tpm,max(1,rpm/6),max(batch_tokens,tpm/6)))
            cur.execute("SELECT rpm,tpm FROM embedding_runtime.quota WHERE deployment_key=%s",(quota_key,))
            if cur.fetchone()!=(rpm,tpm):
                raise ValueError("Conflicting RPM/TPM on same deployment across processes")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)

def discover(c, stop, max_chunks):
    job=c["job"]
    while not stop.is_set():
        conn=db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT cursor_id,discovery_done FROM embedding_runtime.runs WHERE job_key=%s FOR UPDATE SKIP LOCKED",(job,))
                row=cur.fetchone()
                if row is None:
                    conn.rollback()
                    stop.wait(.1)
                    continue
                cursor, done=row
                if done:
                    conn.rollback()
                    return
                limit=2000
                if max_chunks:
                    cur.execute("SELECT count(*) FROM embedding_runtime.tasks WHERE job_key=%s",(job,))
                    limit=min(limit,max_chunks-cur.fetchone()[0])
                if limit<=0:
                    cur.execute("UPDATE embedding_runtime.runs SET discovery_done=true WHERE job_key=%s",(job,))
                    conn.commit()
                    return
                where=f"{c['vec']} IS NULL" if c["reuse"] else "TRUE"
                args=[]
                if cursor is not None:
                    where+=f" AND {c['id']}>%s::{c['idtype']}"
                    args.append(cursor)
                cur.execute(f"SELECT {c['id']}::text FROM {c['table']} WHERE {where} ORDER BY {c['id']} LIMIT %s",
                            (*args,limit))
                ids=[r[0] for r in cur.fetchall()]
                if ids:
                    psycopg2.extras.execute_values(cur,"""INSERT INTO embedding_runtime.tasks(job_key,row_id)
                       VALUES %s ON CONFLICT (job_key,row_id) DO UPDATE
                       SET status='PENDING',owner=NULL,claim_token=NULL,lease_until=NULL,
                           processing_comment='Source was requeued after rediscovery',
                           updated_at=now()
                       WHERE embedding_runtime.tasks.status IN ('SUCCESS','SKIPPED')""",
                       [(job,r) for r in ids],page_size=500)
                    cur.execute("UPDATE embedding_runtime.runs SET cursor_id=%s WHERE job_key=%s",(ids[-1],job))
                else:
                    cur.execute("UPDATE embedding_runtime.runs SET discovery_done=true WHERE job_key=%s",(job,))
            conn.commit()
            if not ids:
                return
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn)

def claim(c, owner):
    token=str(uuid.uuid4())
    result=txn("""WITH todo AS (
       SELECT row_id FROM embedding_runtime.tasks WHERE job_key=%s
       AND (status='PENDING' OR (status='RUNNING' AND lease_until<now()))
       ORDER BY row_id FOR UPDATE SKIP LOCKED LIMIT %s)
       UPDATE embedding_runtime.tasks t SET status='RUNNING',owner=%s,claim_token=%s,
        lease_until=now()+(%s*interval '1 second'),attempts=t.attempts+1,updated_at=now()
       FROM todo WHERE t.job_key=%s AND t.row_id=todo.row_id RETURNING t.row_id""",
       (c["job"],c["batch_size"],owner,token,c["lease"],c["job"]),True)
    return token,[r[0] for r in result]

def mark(c, token, row_id, status, code=None, message=None, comment=None):
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE embedding_runtime.tasks SET status=%s,error_code=%s,
              error_message=%s,processing_comment=%s,owner=NULL,claim_token=NULL,
              lease_until=NULL,updated_at=now() WHERE job_key=%s AND row_id=%s
              AND claim_token=%s AND status='RUNNING' RETURNING row_id""",
              (status,code,(message or "")[:1500],(comment or "")[:1000],c["job"],row_id,token))
            owned=bool(cur.fetchone())
            if owned and status == 'FAILED' and code:
                cur.execute("""INSERT INTO embedding_runtime.failure_events
                     (job_key,row_id,error_code,error_message,processing_comment)
                     VALUES (%s,%s,%s,%s,%s)""",
                     (c["job"],row_id,code,(message or "")[:1500],(comment or "")[:1000]))
            if owned and c["note"] and code:
                text=(code+": "+(message or ""))[:c["note_limit"]]
                where=f" AND {c['vec']} IS NULL" if c["reuse"] else ""
                cur.execute("SAVEPOINT embedding_note")
                try:
                    cur.execute(f"UPDATE {c['table']} SET {c['note']}=%s WHERE {c['id']}=%s::{c['idtype']}{where}",
                                (text,row_id))
                    cur.execute("RELEASE SAVEPOINT embedding_note")
                except psycopg2.Error:
                    # Never lose the durable failure record because a source-table
                    # note trigger/constraint rejected the optional note update.
                    cur.execute("ROLLBACK TO SAVEPOINT embedding_note")
                    cur.execute("RELEASE SAVEPOINT embedding_note")
                    cur.execute("""UPDATE embedding_runtime.tasks SET
                         processing_comment=LEFT(COALESCE(processing_comment,'') ||
                         '; source note write failed',1000)
                         WHERE job_key=%s AND row_id=%s""",(c["job"],row_id))
        conn.commit()
        return owned
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)

def commit_vectors(c, token, rows):
    """Atomic bulk write; claim token fences stale workers and text hash guards edits."""
    if not rows: return set()
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT row_id FROM embedding_runtime.tasks WHERE job_key=%s
              AND claim_token=%s AND status='RUNNING' AND row_id=ANY(%s) FOR UPDATE""",
              (c["job"],token,[r[0] for r in rows]))
            owned={r[0] for r in cur.fetchall()}
            values=[(rid,"["+",".join(map(str,vec))+"]",md5) for rid,md5,vec in rows if rid in owned]
            if not values:
                conn.rollback()
                return set()
            clear=f", {c['note']}=NULL" if c["note"] else ""
            guard=f" AND t.{c['vec']} IS NULL" if c["reuse"] else ""
            changed=psycopg2.extras.execute_values(cur,
                f"""UPDATE {c['table']} t SET {c['vec']}=v.embedding{clear}
                FROM (VALUES %s) v(row_id,embedding,source_md5)
                WHERE t.{c['id']}=v.row_id AND md5(t.{c['text']})=v.source_md5
                  {guard} RETURNING t.{c['id']}::text""",
                values,template=f"(%s::{c['idtype']},%s::vector,%s)",fetch=True,page_size=250)
            done={r[0] for r in changed}
            if done:
                cur.execute("""UPDATE embedding_runtime.tasks SET status='SUCCESS',
                  owner=NULL,claim_token=NULL,lease_until=NULL,updated_at=now(),
                  error_code=NULL,error_message=NULL,processing_comment='Vector committed'
                  WHERE job_key=%s AND claim_token=%s AND row_id=ANY(%s)""",
                  (c["job"],token,list(done)))
        conn.commit()
        return done
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)

def persist(c, token, rows):
    try:
        done=commit_vectors(c,token,rows)
    except (psycopg2.DataError,psycopg2.IntegrityError) as err:
        if len(rows)>1:
            n=len(rows)//2
            persist(c,token,rows[:n])
            persist(c,token,rows[n:])
            return
        mark(c,token,rows[0][0],"FAILED","DB_WRITE",str(err),"Inspect source constraints")
        return
    for rid,_,_ in rows:
        if rid not in done:
            mark(c,token,rid,"FAILED","SOURCE_CHANGED",
                 "Source text or vector changed while embedding","Requeue when source is stable")

def throttle(c, tokens, stop):
    """Cross-process leaky token bucket with shared 429 cooldown."""
    while not stop.is_set():
        conn=db()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT rpm,tpm,requests,tokens,last_refill,cooldown,multiplier,now()
                     FROM embedding_runtime.quota WHERE deployment_key=%s FOR UPDATE""",(c["quota"],))
                rpm,tpm,r,t,last,cool,scale,now=cur.fetchone()
                seconds=max(0,(now-last).total_seconds())
                if rpm:r=min(max(1,rpm/6),r+seconds*rpm*scale/60)
                if tpm:t=min(max(c["batch_tokens"],tpm/6),t+seconds*tpm*scale/60)
                delay=max(0,(cool-now).total_seconds()) if cool else 0
                if rpm and r<1:delay=max(delay,(1-r)*60/(rpm*scale))
                if tpm and t<tokens:delay=max(delay,(tokens-t)*60/(tpm*scale))
                if delay==0:
                    if rpm:r-=1
                    if tpm:t-=tokens
                    cur.execute("""UPDATE embedding_runtime.quota SET requests=%s,tokens=%s,
                         last_refill=%s WHERE deployment_key=%s""",(r,t,now,c["quota"]))
                    conn.commit()
                else:
                    # Waiting for quota must not generate WAL or update the shared
                    # throttle row. Refill is computed from last_refill at next claim.
                    conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn)
        if delay==0:return True
        stop.wait(min(delay,2))
    return False

def limit_429(c, seconds):
    txn("""UPDATE embedding_runtime.quota SET
      cooldown=GREATEST(COALESCE(cooldown,now()),now()+(%s*interval '1 second')),
      multiplier=GREATEST(.2,multiplier*.8) WHERE deployment_key=%s""",
      (min(max(seconds,.1),120),c["quota"]))

def call(c, session, texts):
    url=(f"{c['base']}/openai/deployments/{c['deployment']}/embeddings"
         f"?api-version={c['api_version']}")
    try:
        response=session.post(url,headers={"api-key":c["api_key"]},
              json={"model":c["model"],"input":texts,"dimensions":c["dim"]},
              timeout=(10,c["timeout"]))
    except requests.RequestException as ex:
        raise ApiError(type(ex).__name__+": "+str(ex)[:250]) from ex
    if response.status_code in (401,403,404):
        raise FatalApiError(f"Authentication/deployment configuration HTTP {response.status_code}")
    if response.status_code in (400,422):
        try:
            detail=response.json().get("error",{})
            parameter=str(detail.get("param","")).lower()
            reason=str(detail.get("code","")).lower()
            if parameter in ("model","dimensions","deployment") or reason in (
                "deploymentnotfound","invalidmodel","invaliddeployment"):
                raise FatalApiError("Embedding configuration error: "+str(detail)[:300])
        except (TypeError,ValueError,AttributeError):
            pass
    if not response.ok:
        delay=2
        for key,factor in (("retry-after-ms",.001),("Retry-After",1)):
            try:
                delay=max(.1,float(response.headers[key])*factor)
                break
            except (KeyError,ValueError,TypeError):
                pass
        raise ApiError(f"HTTP {response.status_code}: {response.text[:500]}",
                       response.status_code,delay)
    try:
        items=response.json()["data"]
        items.sort(key=lambda x:x["index"])
        if len(items)!=len(texts) or any(x["index"]!=i for i,x in enumerate(items)):
            raise ValueError("Result index/count mismatch")
        vectors=[x["embedding"] for x in items]
        if any(len(v)!=c["dim"] or not all(math.isfinite(float(x)) for x in v)
               or not any(float(x)!=0 for x in v) for v in vectors):
            raise ValueError("Invalid dimensions/zero/nonfinite vector")
        return vectors
    except (ValueError,KeyError,TypeError) as ex:
        raise ApiError("Malformed Azure embedding result: "+str(ex)) from ex

def do_batch(c,session,token,items,stop):
    """Bisect 400 errors, isolating malformed inputs without stalling neighbors."""
    err=None
    for attempt in range(c["attempts"]):
        if not throttle(c,sum(x[3] for x in items),stop):return
        try:
            vectors=call(c,session,[x[1] for x in items])
            persist(c,token,[(x[0],x[2],v) for x,v in zip(items,vectors)])
            txn("""UPDATE embedding_runtime.quota SET multiplier=LEAST(1,multiplier+.002)
                    WHERE deployment_key=%s""",(c["quota"],))
            return
        except ApiError as ex:
            err=ex
            if ex.status in (400,413,422) and len(items)>1:
                mid=len(items)//2
                do_batch(c,session,token,items[:mid],stop)
                do_batch(c,session,token,items[mid:],stop)
                return
            if ex.status==429:limit_429(c,ex.delay)
            if ex.status in (400,413,422):break
            if attempt+1<c["attempts"]:
                stop.wait(min(60,max(ex.delay if ex.status==429 else 0,
                                     1.5*2**attempt+random.random())))
    for rid,_,_,_ in items:
        mark(c,token,rid,"FAILED","API_FAILURE",str(err),
             "Retries exhausted; run --retry-embed-failures to retry")

def process(c,session,token,ids,stop):
    placeholders=",".join(["%s"]*len(ids))
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT {c['id']}::text,{c['text']},{c['vec']} IS NOT NULL
               FROM {c['table']} WHERE {c['id']} IN ({placeholders})""",ids)
            rows={rid:(txt,present) for rid,txt,present in cur.fetchall()}
    finally:
        release(conn)
    encoding=tiktoken.get_encoding("cl100k_base")
    batch=[]
    tokens=0
    for rid in ids:
        if stop.is_set():return
        if rid not in rows:
            mark(c,token,rid,"SKIPPED","MISSING","Source row was deleted","Skipped")
            continue
        text,present=rows[rid]
        if present and c["reuse"]:
            mark(c,token,rid,"SKIPPED","ALREADY_DONE","Vector already exists","Skipped")
            continue
        if not isinstance(text,str) or not text.strip():
            mark(c,token,rid,"SKIPPED","EMPTY_TEXT","Null or empty source text","Skipped")
            continue
        try:
            count=len(encoding.encode(text,disallowed_special=()))
            digest=hashlib.md5(text.encode("utf-8")).hexdigest()
        except Exception as ex:
            mark(c,token,rid,"FAILED","ENCODING",str(ex),"Inspect invalid input")
            continue
        if count>8191 or count>c["batch_tokens"]:
            mark(c,token,rid,"FAILED","INPUT_TOO_LONG",
                 f"{count} tokens exceeds configured/model limit","Split input upstream")
            continue
        if batch and (len(batch)>=c["batch_size"] or tokens+count>c["batch_tokens"]):
            do_batch(c,session,token,batch,stop)
            batch=[]
            tokens=0
        batch.append((rid,text,digest,count))
        tokens+=count
    if batch:do_batch(c,session,token,batch,stop)

def heartbeat(c,owner,stop):
    while not stop.wait(max(5,c["lease"]//4)):
        try:
            txn("""UPDATE embedding_runtime.tasks SET
                lease_until=now()+(%s*interval '1 second')
                WHERE job_key=%s AND owner=%s AND status='RUNNING'""",
                (c["lease"],c["job"],owner))
        except Exception:
            LOG.exception("Heartbeat error; claim expiration is recoverable")

def status(c):
    rows=txn("SELECT status,count(*) FROM embedding_runtime.tasks WHERE job_key=%s GROUP BY status",
             (c["job"],),True)
    return dict(rows),bool(txn("SELECT discovery_done FROM embedding_runtime.runs WHERE job_key=%s",
                              (c["job"],),True)[0][0])

def progress_reporter(c, stop, interval_seconds=60):
    """Read-only progress logs; one lightweight summary per process per minute."""
    try:
        initial, _ = status(c)
        previous_success = initial.get("SUCCESS", 0)
    except Exception:
        LOG.exception("Unable to read initial embedding progress")
        previous_success = 0
    previous_at = time.monotonic()
    while not stop.wait(interval_seconds):
        try:
            states, discovered = status(c)
            now = time.monotonic()
            successes = states.get("SUCCESS", 0)
            rows_per_min = max(0, successes - previous_success) * 60 / max(1, now - previous_at)
            LOG.info(
                "embedding_progress job_key=%s discovered=%s queued=%s running=%s "
                "success=%s failed=%s skipped=%s successful_rows_per_min=%.1f",
                c["job"], discovered, states.get("PENDING", 0),
                states.get("RUNNING", 0), successes, states.get("FAILED", 0),
                states.get("SKIPPED", 0), rows_per_min,
            )
            previous_success, previous_at = successes, now
        except Exception:
            # Progress reporting must never interrupt embedding workers.
            LOG.exception("Embedding progress reporting failed")


def worker(c,owner,stop):
    session=requests.Session()
    try:
        while not stop.is_set():
            token,ids=claim(c,owner)
            if ids:
                process(c,session,token,ids,stop)
                continue
            # Completion probe must use indexes, not aggregate COUNT(*) across
            # millions of historical task rows from every idle worker.
            completed=txn("SELECT discovery_done FROM embedding_runtime.runs WHERE job_key=%s",
                          (c["job"],),True)[0][0]
            if completed:
                remaining=txn("""SELECT EXISTS(SELECT 1 FROM embedding_runtime.tasks
                   WHERE job_key=%s AND status='PENDING' LIMIT 1),
                   EXISTS(SELECT 1 FROM embedding_runtime.tasks
                   WHERE job_key=%s AND status='RUNNING' LIMIT 1)""",
                   (c["job"],c["job"]),True)[0]
                if not any(remaining):
                    return
            stop.wait(1)
    finally:
        session.close()

def embed_existing(profile, *, dry_run=False, max_chunks=None, workers=1,
                   retry_failed=False,rescan_source=False):
    """Run many workers/hosts. Per-row failures remain queryable and do not abort the run."""
    global POOL
    if not profile["embedding"]["enabled"]:raise ValueError("Embeddings disabled")
    if not 1<=workers<=128:raise ValueError("workers must be 1..128")
    if max_chunks is not None and max_chunks<1:raise ValueError("max_chunks must be positive")
    azure_cfg=configparser.ConfigParser()
    if not azure_cfg.read("config.ini"):raise ValueError("config.ini missing")
    azure=azure_cfg["AZURE_OPENAI_EMBEDDING"]
    emb=profile["embedding"]
    rpm,tpm=int(emb.get("rpm",0)),int(emb.get("tpm",0))
    if not dry_run and (rpm<=0 or tpm<=0):
        raise ValueError("Provide positive embedding.rpm and embedding.tpm for safe distributed throttling")
    job=identity(profile,azure,max_chunks)
    deployment=emb.get("deployment",azure["deployment"])
    quota=hashlib.sha256((azure["api_base"].lower()+":"+deployment.lower()).encode()).hexdigest()
    POOL=psycopg2.pool.ThreadedConnectionPool(
        1,workers+8,host=POSTGRES["host"],port=POSTGRES["port"],
        dbname=POSTGRES["database"],user=POSTGRES["user"],password=POSTGRES["password"])
    stop=threading.Event()
    owner=str(uuid.uuid4())
    handlers={}
    try:
        c=validate(profile,azure,max_chunks)
        c.update(job=job,quota=quota,base=azure["api_base"].rstrip("/"),
                 api_version=azure["api_version"],api_key=azure["api_key"],
                 deployment=deployment,model=emb.get("model",azure.get("model",deployment)),
                 batch_size=int(emb.get("batch_size",128)),
                 batch_tokens=int(emb.get("batch_tokens",24000)),
                 timeout=int(emb.get("timeout_seconds",90)),
                 lease=int(emb.get("lease_seconds",300)),
                 attempts=int(emb.get("max_attempts",5)),
                 reuse=emb["reuse_existing"])
        if not 1<=c["batch_size"]<=2048 or not 1<=c["batch_tokens"]<=300000:
            raise ValueError("Invalid batch constraints")
        if c["lease"]<=c["timeout"]+30:raise ValueError("Lease must exceed timeout by >30 seconds")
        if tpm and c["batch_tokens"]>tpm:
            raise ValueError("batch_tokens must not exceed deployment TPM")
        if dry_run:
            where=f"WHERE {c['vec']} IS NULL" if c["reuse"] else ""
            n=txn(f"SELECT count(*) FROM {c['table']} {where}",fetch=True)[0][0]
            return dict(dry_run=True,eligible=min(n,max_chunks) if max_chunks else n)
        initialize(job,quota,rpm,tpm,c['batch_tokens'],{
            "source_schema": profile["existing_chunks"]["schema"],
            "source_table": profile["existing_chunks"]["table"],
            "profile_name": profile.get("name", "unnamed"),
            "model_name": c["model"],
            "max_chunks": max_chunks,
        })
        if retry_failed:
            txn("""UPDATE embedding_runtime.tasks SET status='PENDING',updated_at=now(),
               processing_comment='Explicitly requeued; prior failure reason retained'
               WHERE job_key=%s AND status='FAILED'""",(job,))
        if rescan_source:
            txn("UPDATE embedding_runtime.runs SET cursor_id=NULL,discovery_done=false WHERE job_key=%s",(job,))
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT,signal.SIGTERM):
                handlers[sig]=signal.getsignal(sig)
                signal.signal(sig,lambda _s,_f:stop.set())
        LOG.info(
            "embedding_started job_key=%s table=%s workers=%s rpm=%s tpm=%s "
            "batch_size=%s batch_tokens=%s max_chunks=%s",
            job, c["table"], workers, rpm, tpm, c["batch_size"],
            c["batch_tokens"], max_chunks if max_chunks is not None else "all",
        )
        errors=[]
        def producer():
            try:discover(c,stop,max_chunks)
            except Exception as ex:
                errors.append(ex)
                stop.set()
        discovery=threading.Thread(target=producer,daemon=True)
        beat=threading.Thread(target=heartbeat,args=(c,owner,stop),daemon=True)
        progress=threading.Thread(target=progress_reporter,args=(c,stop),daemon=True)
        discovery.start()
        beat.start()
        progress.start()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures=[pool.submit(worker,c,owner,stop) for _ in range(workers)]
                for future in concurrent.futures.as_completed(futures):
                    try:future.result()
                    except Exception:
                        stop.set()
                        raise
            if errors:raise errors[0]
            states,done=status(c)
            LOG.info("embedding_finished job_key=%s states=%s discovery_done=%s",
                     job, states, done)
            return dict(job_key=job,states=states,discovery_done=done)
        finally:
            stop.set()
            discovery.join(timeout=5)
            beat.join(timeout=5)
            progress.join(timeout=5)
            txn("""UPDATE embedding_runtime.tasks SET status='PENDING',owner=NULL,
                claim_token=NULL,lease_until=NULL,updated_at=now()
                WHERE job_key=%s AND owner=%s AND status='RUNNING'""",(job,owner))
    finally:
        for sig,handler in handlers.items():signal.signal(sig,handler)
        POOL.closeall()
        POOL=None
