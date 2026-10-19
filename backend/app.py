"""EvidenceFlow API & UI. Same codebase as the worker; no microservice split."""
from __future__ import annotations
import csv
import io
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel,Field
from . import config, store
from sqlalchemy import text as sql_text
from .auth import identity
from . import enterprise_state  # Register durable workflow tables for migrations
from . import bulk

ROOT=Path(__file__).resolve().parents[1]
ID_PATTERN=re.compile(r"^[A-Za-z0-9_-]{1,128}$")

@asynccontextmanager
async def lifespan(app):
    if config.MODE=="live":
        if not config.DATABASE_URL.startswith("postgresql"):
            raise RuntimeError("Live processing requires PostgreSQL")
        if not (config.OIDC_ISSUER and config.OIDC_AUDIENCE and config.OIDC_JWKS_URL):
            raise RuntimeError("Live mode requires corporate OIDC issuer, audience, and JWKS URL")
    if config.MODE=="demo":
        store.init_db();store.seed_demo()
    # Live schema must be deployed by the explicit database migration job.
    if config.MODE=="demo":
        from .worker import serve
        threading.Thread(target=serve,kwargs={"concurrency":2},daemon=True).start()
    yield

app=FastAPI(title="EvidenceFlow API",version="1.0",lifespan=lifespan)

@app.middleware("http")
async def corporate_auth(request:Request,call_next):
    if request.url.path.startswith('/api/v1/') and request.url.path not in ('/api/v1/health','/api/v1/health/ready'):
        from fastapi.responses import JSONResponse
        try:request.state.identity=identity(request)
        except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    return response

def principal(request:Request):
    return request.state.identity

def require_job(request:Request,job_id:str):
    u=principal(request)
    j=store.job_get(job_id,u.subject,u.tenant)
    if not j:raise HTTPException(404,'Job not found')
    return j

class Submit(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=5000)
    label:str=Field(default="New extraction job",max_length=120)

@app.get("/api/v1/health")
def health():return {"status":"ok","mode":config.MODE,"live_connectors":config.MODE=="live",
                     "llm_provider":"JnJ Azure OpenAI" if config.MODE=="live" else "not connected (demo)"}

@app.get('/api/v1/health/ready')
def ready():
    """DB-backed readiness; not dependent on transient OCR/LLM provider latency."""
    try:
        with store.engine.connect() as conn:conn.execute(sql_text('SELECT 1'))
        return {'ready':True}
    except Exception as exc:raise HTTPException(503,'Operational database is unavailable') from exc

@app.get("/api/v1/jobs")
def jobs(request:Request):
    u=principal(request);return store.jobs_list(u.subject,u.tenant)

@app.post("/api/v1/jobs",status_code=202)
def submit(body:Submit,request:Request):
    ids=[x.strip() for x in body.ids if x.strip()]
    if not ids or any(not ID_PATTERN.fullmatch(x) for x in ids):raise HTTPException(422,"IDs must contain only letters, digits, underscore, and hyphen")
    u=principal(request)
    if config.MODE=="live" and "rimdocs:read_all" not in u.scopes and not config.SF_ENTITLEMENTS_TABLE:
        raise HTTPException(403,"Document entitlement service must be configured for non-admin users")
    jid=store.create_job(body.label.strip() or "New extraction job",u.subject,u.tenant,read_all=("rimdocs:read_all" in u.scopes))
    store.append_ids(jid,ids)
    return store.job_get(jid)

@app.post("/api/v1/jobs/upload",status_code=202)
def submit_bulk(request:Request,file:UploadFile=File(...),label:str=Form("Bulk extraction job")):
    """CSV is parsed and inserted in bounded chunks; never materialize one million IDs in RAM."""
    if not file.filename or not file.filename.lower().endswith((".csv",".txt")):
        raise HTTPException(422,"CSV or TXT upload required")
    if file.size and file.size>100*1024*1024:raise HTTPException(413,"Upload exceeds 100 MB")
    u=principal(request)
    if config.MODE=="live" and "rimdocs:read_all" not in u.scopes and not config.SF_ENTITLEMENTS_TABLE:
        raise HTTPException(403,"Document entitlements must be configured")
    jid=store.create_job(label[:120].strip() or "Bulk extraction job",u.subject,u.tenant,read_all=("rimdocs:read_all" in u.scopes))
    invalid=0;read=0;chunk=[]
    try:
        text=io.TextIOWrapper(file.file,encoding="utf-8-sig",newline="")
        rows=csv.reader(text)
        first=next(rows,None)
        if first is None:raise HTTPException(422,"Empty file")
        header=[x.strip().casefold() for x in first]
        if "r_object_id" in header:
            ix=header.index("r_object_id")
        else:
            ix=0
            rows=_chain_first(first,rows)
        for row in rows:
            read+=1
            if read>2_000_000:raise HTTPException(413,"Maximum 2 million CSV rows per job")
            value=row[ix].strip() if len(row)>ix else ""
            if not value:continue
            if not ID_PATTERN.fullmatch(value):invalid+=1;continue
            chunk.append(value)
            if len(chunk)>=1000:store.append_ids(jid,chunk);chunk=[]
        if chunk:store.append_ids(jid,chunk)
        if not store.job_get(jid)["counts"]["total"]:
            raise HTTPException(422,"No valid IDs found")
        return {**store.job_get(jid),"invalid_rows_skipped":invalid}
    finally:
        file.file.close()

def _chain_first(first,reader):
    yield first
    yield from reader

@app.get("/api/v1/jobs/{job_id}")
def job(job_id:str,request:Request):
    u=principal(request);j=store.job_get(job_id,u.subject,u.tenant)
    if not j:raise HTTPException(404,"Job not found")
    return j

@app.get("/api/v1/jobs/{job_id}/results")
def results(job_id:str,request:Request,offset:int=Query(0,ge=0),limit:int=Query(25,ge=1,le=200),search:str="",status:str="ALL"):
    require_job(request,job_id)
    return store.documents_list(job_id,offset,limit,search,status)

@app.get("/api/v1/jobs/{job_id}/documents/{document_id}")
def doc(job_id:str,document_id:str,request:Request):
    require_job(request,job_id)
    d=store.document_get(job_id,document_id)
    if not d:raise HTTPException(404,"Document not part of job")
    return d

@app.post("/api/v1/jobs/{job_id}/retry",status_code=202)
def retry(job_id:str,request:Request):
    require_job(request,job_id)
    return {"requeued":store.retry_failed(job_id)}

@app.get('/api/v1/jobs/{job_id}/documents/{document_id}/source-url')
def authorized_source(job_id:str,document_id:str,request:Request):
    """Short-lived, read-only URL to the exact object version. No PDF proxy bottleneck."""
    require_job(request,job_id)
    import boto3
    from urllib.parse import urlsplit
    from .integrations import PermanentError
    with store.Session() as s:
        d=s.get(store.Document,(job_id,document_id))
        if not d or not d.s3_uri or not d.source_version:
            raise HTTPException(404,'Verified source is not available')
        uri=d.s3_uri;version=d.source_version
    parsed=urlsplit(uri)
    bucket,key=parsed.netloc,parsed.path.lstrip('/')
    if bucket not in config.ALLOWED_S3_BUCKETS or (config.ALLOWED_S3_PREFIXES and not key.startswith(config.ALLOWED_S3_PREFIXES)):
        raise HTTPException(403,'Source is outside authorized storage')
    u=principal(request)
    if config.MODE=='live' and 'rimdocs:read_all' not in u.scopes:
        from .integrations import SnowflakeEntitlements
        if not config.SF_ENTITLEMENTS_TABLE or document_id not in SnowflakeEntitlements().allowed([document_id],u.subject,u.tenant):
            raise HTTPException(404,'Document not found or no longer authorized')
    s3=boto3.client('s3')
    try:head=s3.head_object(Bucket=bucket,Key=key,VersionId=version)
    except Exception as exc:raise HTTPException(409,'Immutable source version unavailable') from exc
    if head.get('VersionId')!=version:
        raise HTTPException(409,'Immutable source version unavailable; reprocessing required')
    url=s3.generate_presigned_url('get_object',Params={'Bucket':bucket,'Key':key,'VersionId':version,
                  'ResponseContentType':'application/pdf'},ExpiresIn=300)
    return {'url':url,'expires_seconds':300}

@app.post('/api/v1/jobs/{job_id}/cancel',status_code=202)
def cancel(job_id:str,request:Request):
    require_job(request,job_id)
    return {'cancelled':store.cancel_job(job_id)}

@app.get('/api/v1/jobs/{job_id}/activity')
def activity(job_id:str,request:Request):
    require_job(request,job_id)
    from sqlalchemy import select,func
    with store.Session() as s:
        rows=s.execute(select(enterprise_state.StageTask.stage,enterprise_state.StageTask.state,
                       func.count()).where(enterprise_state.StageTask.job_id==job_id)
                       .group_by(enterprise_state.StageTask.stage,enterprise_state.StageTask.state)).all()
        return {'task_states':[{'stage':a,'state':b,'count':c} for a,b,c in rows]}

# Resumable, direct-to-S3 multipart CSV ingestion for very large user submissions.
@app.post('/api/v1/uploads/init',status_code=202)
def upload_init(request:Request,label:str='Bulk extraction job'):
    if config.MODE!='live':raise HTTPException(409,'Multipart S3 upload is available in live mode')
    u=principal(request)
    if 'rimdocs:read_all' not in u.scopes and not config.SF_ENTITLEMENTS_TABLE:
        raise HTTPException(403,'Document entitlements must be configured')
    job_id=store.create_job(label[:120],u.subject,u.tenant,read_all=('rimdocs:read_all' in u.scopes))
    upload_id=bulk.start_upload(job_id)
    return {'job_id':job_id,'upload_id':upload_id,'part_bytes':8*1024*1024}

@app.get('/api/v1/jobs/{job_id}/uploads/{upload_id}')
def upload_status(job_id:str,upload_id:str,request:Request):
    require_job(request,job_id)
    try:return bulk.uploaded_parts(job_id,upload_id)
    except ValueError as e:raise HTTPException(404,str(e))

@app.get('/api/v1/jobs/{job_id}/uploads/{upload_id}/parts/{part_number}')
def upload_part_url(job_id:str,upload_id:str,part_number:int,request:Request):
    require_job(request,job_id)
    try:return {'url':bulk.presign_part(job_id,upload_id,part_number)}
    except ValueError as e:raise HTTPException(422,str(e))

class Parts(BaseModel):
    parts:list[dict]=Field(min_length=1,max_length=10000)
@app.post('/api/v1/jobs/{job_id}/uploads/{upload_id}/complete',status_code=202)
def upload_complete(job_id:str,upload_id:str,request:Request,payload:Parts):
    require_job(request,job_id)
    try:return bulk.complete_upload(job_id,upload_id,payload.parts)
    except ValueError as e:raise HTTPException(422,str(e))

@app.post('/api/v1/jobs/{job_id}/exports',status_code=202)
def export_create(job_id:str,request:Request,format:Literal['csv','json']='csv'):
    require_job(request,job_id)
    return {'export_id':bulk.create_export(job_id,format),'state':'PENDING'}

@app.get('/api/v1/jobs/{job_id}/exports/{export_id}')
def export_get(job_id:str,export_id:str,request:Request):
    require_job(request,job_id)
    state=bulk.export_status(job_id,export_id)
    if not state:raise HTTPException(404,'Export not found')
    return {'status':state['state'],'url':bulk.export_download_url(job_id,export_id),
            'format':state['format']}

COLUMNS=["r_object_id","status","artifact_name","source_status","document_date","international_birth_date","product","active_ingredient","summary","error"]
def csv_safe(v):
    s="" if v is None else str(v)
    return "'"+s if s.lstrip().startswith(("=","+","-","@")) else s

def output_rows(job_id):
    offset=0
    while True:
        page=store.documents_list(job_id,offset=offset,limit=1000)
        if not page["items"]:break
        yield from page["items"]
        offset+=len(page["items"])

def stream_csv(job_id):
    f=io.StringIO();w=csv.DictWriter(f,fieldnames=COLUMNS)
    w.writeheader();yield f.getvalue();f.seek(0);f.truncate(0)
    for d in output_rows(job_id):
        row={k:d.get(k) for k in ("r_object_id","status","artifact_name","source_status","summary","error")}
        row.update({key:(d.get("fields") or {}).get(key,{}).get("value") for key in ("document_date","international_birth_date","product","active_ingredient")})
        w.writerow({k:csv_safe(row.get(k)) for k in COLUMNS});yield f.getvalue();f.seek(0);f.truncate(0)

def stream_json(job_id):
    yield '[\n';first=True
    for d in output_rows(job_id):
        if not first:yield ',\n'
        yield json.dumps(d,ensure_ascii=False)
        first=False
    yield '\n]'

@app.get("/api/v1/jobs/{job_id}/export")
def export(job_id:str,request:Request,format:Literal["csv","json"]="csv"):
    require_job(request,job_id)
    if store.job_get(job_id)["counts"]["total"]>50000:
        raise HTTPException(409,"Use asynchronous /exports for jobs above 50,000 documents")
    headers={"Content-Disposition":f'attachment; filename="{job_id}.{format}"',"Cache-Control":"no-store"}
    if format=="csv":return StreamingResponse(stream_csv(job_id),media_type="text/csv",headers=headers)
    return StreamingResponse(stream_json(job_id),media_type="application/json",headers=headers)

@app.get("/")
def index():return FileResponse(ROOT/"frontend"/"index.html",headers={"Cache-Control":"no-store"})

@app.get("/assets/{name}")
def asset(name:str):
    if name not in {"app.js","app.css"}:raise HTTPException(404)
    return FileResponse(ROOT/"frontend"/name)
