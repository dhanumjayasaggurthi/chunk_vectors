"""Independent, resumable OCR of native preserved PDF visual regions.

Native published pages remain immutable. One additive versioned job/result
table is explicitly initialized; source refs and original PDF bytes remain
authoritative. Shared GCP Vision quotas and the existing batched OCR function
are reused. No cloud calls except an explicit 'run --apply'.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

LOG=logging.getLogger("epod.deferred_ocr")
_DEFAULTS={
    "workers":4, "batch_images":8, "max_batch_bytes":6_000_000,
    "render_dpi":200, "max_render_pixels":50_000_000,
    "max_attempts":3, "max_api_retries":4,
    "timeout_seconds":90, "quota_wait_seconds":600,
    "lease_seconds":3600, "retry_delay_seconds":60,
    "confidence_min":0.60, "bbox_detail":"word",
    "rpm":1500, "ipm":1500,
}
_INT_LIMITS={
    "workers":(1,32),"batch_images":(1,16),
    "max_batch_bytes":(100_000,8_500_000),
    "render_dpi":(72,400),"max_render_pixels":(1_000_000,75_000_000),
    "max_attempts":(1,20),"max_api_retries":(1,10),
    "timeout_seconds":(5,600),"quota_wait_seconds":(1,3600),
    "lease_seconds":(120,7200),"retry_delay_seconds":(1,3600),
    "rpm":(1,100_000),"ipm":(16,100_000),
}
_TERMINAL=("TEXT_READY","VISUAL_PENDING","FAILED_TERMINAL")


def settings(profile):
    from page_stage import settings as native
    native_cfg=native(profile)
    if native_cfg["visual_processing"]!="preserve_defer":
        raise ValueError("Deferred OCR requires a native preserve_defer source")
    opt=profile.get("deferred_ocr",{})
    if not isinstance(opt,dict):
        raise ValueError("deferred_ocr must be a mapping")
    if unknown:=set(opt)-set(_DEFAULTS):
        raise ValueError("Unknown deferred_ocr options: "+",".join(sorted(unknown)))
    cfg={**_DEFAULTS,**opt}
    for field,(minimum,maximum) in _INT_LIMITS.items():
        value=cfg[field]
        if type(value) is not int or not minimum<=value<=maximum:
            raise ValueError(f"deferred_ocr.{field} must be {minimum}..{maximum}")
    if cfg["batch_images"]*1024>cfg["max_batch_bytes"]:
        raise ValueError("Deferred OCR byte budget is too small for configured batch")
    value=cfg["confidence_min"]
    if type(value) not in (int,float) or not 0<=value<=1:
        raise ValueError("deferred_ocr.confidence_min must be 0..1")
    if cfg["bbox_detail"] not in ("word","block"):
        raise ValueError("deferred_ocr.bbox_detail must be word or block")
    if cfg["lease_seconds"]<=cfg["max_api_retries"]*(cfg["quota_wait_seconds"]+cfg["timeout_seconds"])+60:
        raise ValueError("OCR lease must exceed worst-case one cloud batch retry budget")
    return cfg


def tables(profile):
    from page_stage import names as stage_names
    from profile_store import _safe_identifier
    n=stage_names(profile)
    source=profile["extraction"]["table"]
    table=source+"_visual_ocr"
    if len(table)>63:
        raise ValueError("OCR output table identifier exceeds PostgreSQL limit")
    n["ocr"]=n["schema"]+"."+_safe_identifier(table)
    n["ocr_table_name"]=table
    return n


def _canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,
                      separators=(",",":"),allow_nan=False)


def ocr_fingerprint(cfg):
    """Version recognition semantics, NOT retry budget or throttle settings."""
    payload={
        "version":1,
        "provider":"google_vision_document_text_detection",
        "render_dpi":cfg["render_dpi"],
        "confidence_min":cfg["confidence_min"],
        "bbox_detail":cfg["bbox_detail"],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def asset_id(doc_id,source_hash,extraction_hash,page_number,position,element,ocr_hash):
    ref=element.get("visual_asset") or element.get("unresolved_region")
    return hashlib.sha256(
        _canonical([doc_id,source_hash,extraction_hash,ocr_hash,page_number,position,
                    element.get("type"),element.get("bbox"),ref]).encode("utf-8")
    ).hexdigest()


def discover_element(doc_id,source,version,page_number,position,element,ocr_hash):
    """Fail closed on malformed source evidence; never infer image content."""
    from pdf_processor import validate_unresolved_region,validate_visual_asset
    if not isinstance(element,dict):
        raise ValueError("Malformed published page element")
    visual=element.get("visual_asset")
    unresolved=element.get("unresolved_region")
    if bool(visual)==bool(unresolved):
        return None
    kind=element.get("type")
    if visual:
        if kind not in ("image","chart"):
            raise ValueError("Visual source reference has wrong element type")
        ref=validate_visual_asset(visual,page_number=page_number)
    else:
        if kind!="vector_region":
            raise ValueError("Unresolved source reference has wrong element type")
        ref=validate_unresolved_region(unresolved,page_number=page_number)
    bbox=element.get("bbox")
    if (not isinstance(bbox,list) or len(bbox)!=4
            or any(type(v) not in (float,int) or not math.isfinite(v)
                   for v in bbox)
            or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]):
        raise ValueError("Malformed preserved source geometry")
    return (
        asset_id(doc_id,source,version,page_number,position,element,ocr_hash),
        doc_id,source,version,ocr_hash,page_number,kind,
        _canonical(bbox),_canonical(ref),
    )


def iter_documents(profile,doc_id=None,max_docs=1):
    from db import get_conn
    n=tables(profile)
    conn=get_conn()
    try:
        last=""
        count=0
        while count<max_docs:
            with conn.cursor() as cur:
                if doc_id:
                    cur.execute(f"""
                        SELECT doc_id,source_hash,extraction_hash,page_count
                        FROM {n['docs']} WHERE doc_id=%s
                    """,(doc_id,))
                else:
                    cur.execute(f"""
                        SELECT doc_id,source_hash,extraction_hash,page_count
                        FROM {n['docs']} WHERE doc_id>%s
                        ORDER BY doc_id LIMIT %s
                    """,(last,min(64,max_docs-count)))
                batch=cur.fetchall()
            if not batch:
                break
            for doc in batch:
                yield doc
                count+=1
            last=batch[-1][0]
            if doc_id:
                break
    finally:
        conn.rollback()
        conn.close()


def iter_assets(profile,doc):
    """Keyset pagination: only 32 pages' JSONB lives in memory at a time."""
    from db import get_conn
    n=tables(profile)
    doc_id,source,version,page_count=doc
    last=0
    ocr_hash=ocr_fingerprint(settings(profile))
    conn=get_conn()
    seen=0
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT page_number,source_hash,extraction_hash,elements
                    FROM {n['published']}
                    WHERE doc_id=%s AND page_number>%s
                    ORDER BY page_number LIMIT 32
                """,(doc_id,last))
                pages=cur.fetchall()
            if not pages:
                break
            for page_number,row_source,row_version,elements in pages:
                if (row_source,row_version)!=(source,version):
                    raise ValueError("Published page source/extraction version mismatch")
                if not isinstance(elements,list):
                    raise ValueError("Published page geometry is not JSONB array")
                seen+=1
                for position,element in enumerate(elements):
                    if not isinstance(element,dict):
                        raise ValueError("Malformed source JSONB element")
                    if "visual_asset" not in element and "unresolved_region" not in element:
                        continue
                    candidate=discover_element(
                        doc_id,source,version,page_number,position,element,ocr_hash)
                    if candidate is None:
                        raise ValueError("Ambiguous source visual provenance")
                    yield candidate
            last=pages[-1][0]
        if seen!=page_count:
            raise ValueError(f"Only {seen}/{page_count} original published pages")
    finally:
        conn.rollback()
        conn.close()


def init(profile,apply=False):
    n=tables(profile)
    if not apply:
        return {"writes":False,"output":n["ocr"],"note":"DDL requires init --apply"}
    from db import get_conn
    from profile_store import _safe_identifier
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            # Published-page schema already exists; no new schemas or
            # cloud/provider calls are made here.
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {n['ocr']} (
                  asset_id text PRIMARY KEY,
                  doc_id text NOT NULL,
                  source_hash text NOT NULL,
                  extraction_hash text NOT NULL,
                  ocr_hash text NOT NULL,
                  page_number integer NOT NULL,
                  kind text NOT NULL,
                  bbox jsonb NOT NULL,
                  source_ref jsonb NOT NULL,
                  status text NOT NULL DEFAULT 'PENDING',
                  attempts integer NOT NULL DEFAULT 0,
                  lease_token text,
                  lease_until timestamptz,
                  next_attempt_at timestamptz NOT NULL DEFAULT now(),
                  ocr_text text NOT NULL DEFAULT '',
                  ocr_regions jsonb NOT NULL DEFAULT '[]'::jsonb,
                  confidence double precision,
                  language text NOT NULL DEFAULT '',
                  error_code text,
                  error_message text,
                  updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {_safe_identifier(n['ocr_table_name']+'_claim_idx')}
                ON {n['ocr']}(status,next_attempt_at,lease_until,page_number)
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"initialized":n["ocr"],"native_source_unchanged":True}


def enqueue(profile,doc_id=None,max_docs=1,apply=False):
    from db import get_conn
    from psycopg2.extras import execute_values
    n=tables(profile)
    results={"documents":0,"candidates":0,"inserted":0,"writes":bool(apply)}
    if apply:
        conn_check=get_conn()
        try:
            with conn_check.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)",(n["ocr"],))
                if cur.fetchone()[0] is None:
                    raise ValueError("OCR output not initialized; run init --apply")
        finally:
            conn_check.rollback()
            conn_check.close()
    for doc in iter_documents(profile,doc_id=doc_id,max_docs=max_docs):
        results["documents"]+=1
        group=[]
        for asset in iter_assets(profile,doc):
            results["candidates"]+=1
            if not apply:
                continue
            group.append(asset)
            if len(group)>=128:
                results["inserted"]+=_insert_assets(n,group)
                group.clear()
        if group:
            results["inserted"]+=_insert_assets(n,group)
    return results


def _insert_assets(n,group):
    from db import get_conn
    from psycopg2.extras import execute_values
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur,f"""
                INSERT INTO {n['ocr']} (
                  asset_id,doc_id,source_hash,extraction_hash,ocr_hash,
                  page_number,kind,bbox,source_ref
                ) VALUES %s ON CONFLICT (asset_id) DO NOTHING
            """,group,template="(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            page_size=128)
            changed=cur.rowcount
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim(profile,limit,max_attempts,lease_seconds):
    """Global SKIP LOCKED claim; no DB connection held during OCR RPCs."""
    from db import get_conn
    n=tables(profile)
    token=str(uuid.uuid4())
    ocr_hash=ocr_fingerprint(settings(profile))
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            # Without this, a worker crash on its final allowed attempt
            # leaves a RUNNING row forever after its lease expires.
            cur.execute(f"""
                UPDATE {n['ocr']} AS j
                SET status='FAILED_TERMINAL',lease_token=NULL,
                    lease_until=NULL,error_code='OCR_ATTEMPT_LIMIT',
                    error_message='Retry allowance exhausted after expired lease',
                    updated_at=now()
                WHERE j.ocr_hash=%s AND j.attempts >= %s
                  AND (j.status IN ('PENDING','RETRY_WAIT')
                       OR (j.status='RUNNING' AND j.lease_until<now()))
                  AND EXISTS (
                    SELECT 1 FROM {n['docs']} AS d WHERE d.doc_id=j.doc_id
                    AND d.source_hash=j.source_hash
                    AND d.extraction_hash=j.extraction_hash
                  )
            """,(ocr_hash,max_attempts,))
            cur.execute(f"""
                WITH picked AS (
                  SELECT j.asset_id FROM {n['ocr']} AS j
                  JOIN {n['docs']} AS d ON d.doc_id=j.doc_id
                    AND d.source_hash=j.source_hash
                    AND d.extraction_hash=j.extraction_hash
                  WHERE j.ocr_hash=%s AND (
                    (j.status IN ('PENDING','RETRY_WAIT')
                       AND j.next_attempt_at<=now())
                    OR (j.status='RUNNING' AND j.lease_until<now())
                  )
                  AND j.attempts<%s
                  ORDER BY j.next_attempt_at,j.page_number,j.asset_id
                  LIMIT %s FOR UPDATE OF j SKIP LOCKED
                )
                UPDATE {n['ocr']} AS j
                SET status='RUNNING', attempts=j.attempts+1,
                    lease_token=%s,
                    lease_until=now()+(%s*interval '1 second'),
                    updated_at=now()
                FROM picked
                WHERE j.asset_id=picked.asset_id
                RETURNING j.asset_id,j.doc_id,j.source_hash,
                  j.extraction_hash,j.page_number,j.kind,
                  j.bbox,j.attempts,j.lease_token
            """,(ocr_hash,max_attempts,limit,token,lease_seconds))
            rows=cur.fetchall()
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew_leases(profile,jobs,lease_seconds):
    """Refresh only still-owned, unexpired claims before a cloud batch.

    When a bounded job group needs multiple sequential Vision requests, this
    prevents the next subgroup from expiring while the previous one waits for
    shared quota. An overwritten/expired lease fails closed.
    """
    if not jobs:
        return
    token=jobs[0][8]
    if any(job[8]!=token for job in jobs):
        raise RuntimeError("OCR claim group has mixed lease owners")
    ids=[job[0] for job in jobs]
    from db import get_conn
    n=tables(profile)
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {n['ocr']}
                SET lease_until=now()+(%s*interval '1 second'),
                    updated_at=now()
                WHERE asset_id=ANY(%s) AND lease_token=%s
                  AND status='RUNNING' AND lease_until>now()
                RETURNING asset_id
            """,(lease_seconds,ids,token))
            updated={row[0] for row in cur.fetchall()}
            if updated!=set(ids):
                raise RuntimeError("OCR lease expired or stolen before cloud call")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete(profile,job,*,status,text="",regions=None,confidence=None,
             language="",error_code=None,error_message="",delay_seconds=60):
    from db import get_conn
    n=tables(profile)
    asset_id,doc_id,source,version,page,kind,bbox,attempts,token=job
    if status not in ("TEXT_READY","VISUAL_PENDING","RETRY_WAIT","FAILED_TERMINAL"):
        raise ValueError("Invalid OCR result state")
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {n['ocr']}
                SET status=%s,ocr_text=%s,ocr_regions=%s::jsonb,
                    confidence=%s,language=%s,error_code=%s,
                    error_message=%s,lease_token=NULL,lease_until=NULL,
                    next_attempt_at=CASE WHEN %s='RETRY_WAIT'
                       THEN now()+(%s*interval '1 second') ELSE now() END,
                    updated_at=now()
                WHERE asset_id=%s AND lease_token=%s
                  AND status='RUNNING'
            """,(status,text,_canonical(regions or []),confidence,language,
                 error_code,error_message[:500],status,delay_seconds,
                 asset_id,token))
            if cur.rowcount!=1:
                raise RuntimeError("OCR lease ownership lost; stale result rejected")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def render_clipped(pdf,page_number,bbox,cfg):
    """Raster only the authoritative source clip; never alter native pages."""
    import fitz
    if type(page_number) is not int or not 1<=page_number<=pdf.page_count:
        raise ValueError("OCR page index out of source bounds")
    page=pdf[page_number-1]
    if (not isinstance(bbox,list) or len(bbox)!=4
            or not all(type(v) in (float,int) and math.isfinite(v)
                       for v in bbox)):
        raise ValueError("Malformed source bbox")
    rect=fitz.Rect(*bbox)
    if (rect.is_empty or rect.is_infinite or rect.x0<-1 or rect.y0<-1
            or rect.x1>page.rect.width+1 or rect.y1>page.rect.height+1):
        raise ValueError("OCR source bbox outside page")
    scale=cfg["render_dpi"]/72
    if math.ceil(rect.width*scale)*math.ceil(rect.height*scale)>cfg["max_render_pixels"]:
        raise ValueError("OCR clipped raster exceeds pixel budget")
    pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),
                        clip=rect,alpha=False)
    try:
        return pix.tobytes("png")
    finally:
        pix=None


def _file_path(profile,doc_id,source,version):
    from db import get_conn
    n=tables(profile)
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT file_path FROM {n['published']}
                WHERE doc_id=%s AND source_hash=%s
                  AND extraction_hash=%s AND page_number=1
            """,(doc_id,source,version))
            row=cur.fetchone()
            if row is None:
                raise ValueError("Published source PDF missing or version changed")
            return Path(row[0]).resolve(strict=True)
    finally:
        conn.rollback()
        conn.close()


class _SourceVerifier:
    def __init__(self):
        self._guard=threading.Lock()
        self._seen={}

    def verify(self,path,doc_id,source):
        from page_stage import source_hash as hash_pdf
        path=Path(path)
        if hashlib.sha256(str(path).encode("utf-8")).hexdigest()!=doc_id:
            raise ValueError("Source PDF doc_id/path mismatch")
        stat=path.stat()
        key=(str(path),source,stat.st_size,stat.st_mtime_ns)
        with self._guard:
            if key not in self._seen:
                if hash_pdf(str(path))!=source:
                    raise ValueError("Original PDF byte hash differs from published source")
                self._seen[key]=True
        return stat

    def ensure_unchanged(self,path,stat):
        now=Path(path).stat()
        if (now.st_size,now.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):
            raise ValueError("Original PDF changed while rendering OCR")


def _group_jobs(jobs,batch_size):
    """One PDF handle per task; keep claims and batches bounded."""
    buckets={}
    for job in jobs:
        buckets.setdefault((job[1],job[2],job[3]),[]).append(job)
    groups=[]
    for bucket in buckets.values():
        for start in range(0,len(bucket),batch_size):
            groups.append(bucket[start:start+batch_size])
    return groups


def _mark_failure(profile,job,cfg,exc,terminal=False):
    status=("FAILED_TERMINAL" if terminal or job[7]>=cfg["max_attempts"]
            else "RETRY_WAIT")
    delay=min(3600,cfg["retry_delay_seconds"]*(2**min(8,job[7]-1)))
    complete(profile,job,status=status,error_code=type(exc).__name__,
             error_message=str(exc),delay_seconds=delay)
    return status


def run_group(profile,group,cfg,limiter,verifier):
    """Own one PDF handle per thread and complete every claimed asset.

    The cloud batch and each persisted result are handled separately. A DB
    failure cannot be turned into a successful job by a generic exception
    handler; a crashed worker's lease remains recoverable.
    """
    from vision_ocr import batch_document_text_detection
    import fitz
    counts=Counter()
    finished=set()

    def fail(job,exc,terminal=False):
        if job[0] in finished:
            return
        status=_mark_failure(profile,job,cfg,exc,terminal=terminal)
        finished.add(job[0])
        counts[status]+=1

    def finalize(job,result):
        if job[0] in finished:
            return
        if (result.success and result.text.strip()
                and not result.error):
            complete(profile,job,status="TEXT_READY",text=result.text,
                     regions=result.regions,confidence=result.confidence,
                     language=result.language)
            status="TEXT_READY"
        elif result.success and (
                not result.text.strip() or result.error.startswith((
                    "low_confidence_","missing_word_confidence"))):
            # OCR transcription is not evidence of successful chart/table
            # interpretation. Keep its complete, unchanged response.
            complete(profile,job,status="VISUAL_PENDING",text=result.text,
                     regions=result.regions,confidence=result.confidence,
                     language=result.language,
                     error_code=result.error or "NO_OCR_TEXT")
            status="VISUAL_PENDING"
        else:
            fail(job,RuntimeError(result.error or "OCR unsuccessful"))
            return
        finished.add(job[0])
        counts[status]+=1

    try:
        path=_file_path(profile,group[0][1],group[0][2],group[0][3])
        state=verifier.verify(path,group[0][1],group[0][2])
        with fitz.open(str(path)) as pdf:
            current=[]
            byte_count=0

            def flush():
                nonlocal current,byte_count
                if not current:
                    return
                batch=current
                current=[]
                byte_count=0
                verifier.ensure_unchanged(path,state)
                renew_leases(
                    profile,[j for j in group if j[0] not in finished],
                    cfg["lease_seconds"],
                )
                try:
                    responses=batch_document_text_detection(
                        [png for _job,png in batch],
                        limiter=limiter,
                        max_retries=cfg["max_api_retries"],
                        timeout_seconds=cfg["timeout_seconds"],
                        min_confidence=cfg["confidence_min"],
                        bbox_detail=cfg["bbox_detail"],
                    )
                    if len(responses)!=len(batch):
                        raise RuntimeError("OCR response count mismatch")
                except Exception as exc:
                    for job,_ in batch:
                        fail(job,exc)
                    return
                for (job,_),result in zip(batch,responses):
                    # Persist each response under its own lease token.
                    # Do NOT convert database failures to OCR failures.
                    finalize(job,result)

            for job in group:
                try:
                    png=render_clipped(pdf,job[4],job[6],cfg)
                    if len(png)>cfg["max_batch_bytes"]:
                        raise ValueError("OCR PNG exceeds request byte budget")
                    if current and byte_count+len(png)>cfg["max_batch_bytes"]:
                        flush()
                    current.append((job,png))
                    byte_count+=len(png)
                except ValueError as exc:
                    fail(job,exc,terminal=True)
                except Exception as exc:
                    fail(job,exc)
            flush()
    except Exception as exc:
        # Persist failures only for leases not already completed. If DB
        # persistence itself is down this must propagate to the caller.
        for job in group:
            if job[0] not in finished:
                fail(job,exc,terminal=isinstance(
                    exc,(ValueError,FileNotFoundError)))
    return counts


def run(profile,max_jobs=100,workers=None):
    from ocr_quota import (PostgresOcrLimiter,detect_project_id,init_quota)
    cfg=settings(profile)
    workers=cfg["workers"] if workers is None else workers
    if type(workers) is not int or not 1<=workers<=32:
        raise ValueError("Invalid OCR workers")
    project=detect_project_id()
    key=init_quota(project,rpm=cfg["rpm"],ipm=cfg["ipm"])
    limiter=PostgresOcrLimiter(
        key,rpm=cfg["rpm"],ipm=cfg["ipm"],
        max_wait_seconds=cfg["quota_wait_seconds"])
    verifier=_SourceVerifier()
    counters=Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while sum(counters.values())<max_jobs:
            quota=min(max_jobs-sum(counters.values()),
                      workers*cfg["batch_images"])
            jobs=claim(profile,quota,cfg["max_attempts"],cfg["lease_seconds"])
            if not jobs:
                break
            future=[
                executor.submit(run_group,profile,group,cfg,limiter,verifier)
                for group in _group_jobs(jobs,cfg["batch_images"])
            ]
            for item in concurrent.futures.as_completed(future):
                counters.update(item.result())
    return {"processed":sum(counters.values()),"statuses":dict(counters),
            "cloud_calls_possible":True}


def status(profile):
    from db import get_conn
    n=tables(profile)
    ocr_hash=ocr_fingerprint(settings(profile))
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT j.status,count(*)
                FROM {n['ocr']} AS j
                JOIN {n['docs']} AS d ON d.doc_id=j.doc_id
                    AND d.source_hash=j.source_hash
                    AND d.extraction_hash=j.extraction_hash
                WHERE j.ocr_hash=%s
                GROUP BY j.status ORDER BY j.status
            """,(ocr_hash,))
            rows=cur.fetchall()
        return {"statuses":dict(rows),"writes":False}
    finally:
        conn.rollback()
        conn.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("validate","init","discover",
                                            "enqueue","run","status"))
    parser.add_argument("--profile",required=True)
    parser.add_argument("--doc-id")
    parser.add_argument("--max-docs",type=int,default=1)
    parser.add_argument("--max-jobs",type=int,default=100)
    parser.add_argument("--workers",type=int)
    parser.add_argument("--apply",action="store_true",
                        help="Required before any DDL, enqueue, or cloud OCR calls")
    args=parser.parse_args(argv)
    if not 1<=args.max_docs<=100000 or not 1<=args.max_jobs<=1000000:
        parser.error("Invalid bounded run limits")
    from processing_profile import load_profile
    profile=load_profile(args.profile)
    cfg=settings(profile)
    if args.command=="validate":
        answer={"valid":True,"writes":False,"source":tables(profile)["published"],
                "output":tables(profile)["ocr"],"options":cfg}
    elif args.command=="init":
        answer=init(profile,apply=args.apply)
    elif args.command in ("discover","enqueue"):
        answer=enqueue(profile,doc_id=args.doc_id,max_docs=args.max_docs,
                       apply=args.command=="enqueue" and args.apply)
    elif args.command=="status":
        answer=status(profile)
    else:
        if not args.apply:
            parser.error("OCR run requires --apply; no implicit cloud calls")
        answer=run(profile,max_jobs=args.max_jobs,workers=args.workers)
    print(json.dumps(answer,indent=2,ensure_ascii=False))
    return int(answer.get("statuses",{}).get("FAILED_TERMINAL",0)>0)


if __name__=="__main__":
    raise SystemExit(main())
