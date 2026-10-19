"""One durable store and work queue. PostgreSQL for live multi-worker operation."""
from __future__ import annotations
import json
import re
import threading
from functools import wraps
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import create_engine, DateTime, ForeignKey, Index, Integer, String, Text, select, update, func, or_, and_, case, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from . import config


def now(): return datetime.now(timezone.utc).replace(tzinfo=None)  # UTC stored as naive to work with SQLite

class Base(DeclarativeBase): pass

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[str] = mapped_column(String(192),default="demo",index=True)
    tenant_id: Mapped[str] = mapped_column(String(192),default="demo",index=True)
    cancelled: Mapped[bool] = mapped_column(default=False)
    read_all: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    total: Mapped[int] = mapped_column(Integer, default=0)
    queued: Mapped[int] = mapped_column(Integer, default=0)
    processing: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    review: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)

class Document(Base):
    __tablename__ = "documents"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    r_object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    artifact_name: Mapped[str | None] = mapped_column(String(255))
    source_status: Mapped[str | None] = mapped_column(String(64))
    ingestion_status: Mapped[str | None] = mapped_column(String(64))
    s3_uri: Mapped[str | None] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(String(255))
    fields_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    pipeline_version: Mapped[str] = mapped_column(String(64), default=config.PROFILE_VERSION)

Index("ix_docs_claim", Document.status, Document.available_at, Document.lease_until)

_local_claim_lock = threading.RLock()  # SQLite demo only; Postgres uses row locks.

def synchronized_claim(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if config.DATABASE_URL.startswith("sqlite"):
            with _local_claim_lock:
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)
    return inner

engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    **({"connect_args":{"check_same_thread":False}} if config.DATABASE_URL.startswith('sqlite') else
       {"pool_size":int(config.setting('DB_POOL_SIZE','2')),
        "max_overflow":int(config.setting('DB_POOL_OVERFLOW','1')),
        "pool_timeout":int(config.setting('DB_POOL_TIMEOUT','10')),
        "pool_recycle":1800})
)
Session = sessionmaker(bind=engine, expire_on_commit=False)

def init_db(): Base.metadata.create_all(engine)

def _summary(j: Job):
    c = {k: getattr(j, k) for k in ("total", "completed", "review", "failed", "queued", "processing")}
    done = c["completed"] + c["review"] + c["failed"]
    status = "COMPLETED" if done == c["total"] and c["total"] else "PROCESSING"
    if done == c["total"] and (c["review"] or c["failed"]): status = "COMPLETED_WITH_EXCEPTIONS"
    return dict(job_id=j.id, label=j.label, created_at=j.created_at.replace(tzinfo=timezone.utc).isoformat(),
                status="CANCELLED" if j.cancelled else status, counts=c, progress_percent=round(100*done/c["total"],1) if c["total"] else 0)

def create_job(label: str,owner_id="demo",tenant_id="demo",read_all=False) -> str:
    jid = "job-" + uuid4().hex[:14]
    with Session.begin() as s:
        s.add(Job(id=jid, label=label,owner_id=owner_id,tenant_id=tenant_id,read_all=read_all))
    return jid

def append_ids(job_id: str, ids: list[str]):
    """Insert one bounded chunk; deduplicate both inside and across chunks."""
    ids = list(dict.fromkeys(ids))
    if not ids: return 0
    values = [dict(job_id=job_id, r_object_id=id, status="PENDING", fields_json="{}", attempts=0,
                   pipeline_version=config.PROFILE_VERSION) for id in ids]
    with Session.begin() as s:
        dialect = s.bind.dialect.name
        if dialect == "postgresql":
            r = s.execute(pg_insert(Document).values(values).on_conflict_do_nothing(index_elements=["job_id","r_object_id"]))
        elif dialect == "sqlite":
            r = s.execute(sqlite_insert(Document).values(values).on_conflict_do_nothing(index_elements=["job_id","r_object_id"]))
        else:
            raise RuntimeError("Use SQLite for local development or PostgreSQL in live mode")
        num = r.rowcount
        s.execute(update(Job).where(Job.id==job_id).values(total=Job.total+num, queued=Job.queued+num))
        return num

def jobs_list(owner_id=None,tenant_id=None):
    with Session() as s:
        q=select(Job)
        if owner_id is not None:q=q.where(Job.owner_id==owner_id,Job.tenant_id==tenant_id)
        return [_summary(j) for j in s.scalars(q.order_by(Job.created_at.desc()).limit(100))]

def job_get(job_id,owner_id=None,tenant_id=None):
    with Session() as s:
        j=s.get(Job,job_id)
        if j and owner_id is not None and (j.owner_id!=owner_id or j.tenant_id!=tenant_id):return None
        return _summary(j) if j else None

def _dict(d: Document):
    return dict(r_object_id=d.r_object_id,status=d.status,artifact_name=d.artifact_name,
                source_status=d.source_status,source_ingestion_status=d.ingestion_status,
                fields=json.loads(d.fields_json or "{}"),summary=d.summary,note=d.note,error=d.error,
                pipeline_version=d.pipeline_version, source_version=d.source_version)

def document_get(job_id,document_id):
    with Session() as s:
        d=s.get(Document,(job_id, document_id));return _dict(d) if d else None

def documents_list(job_id, offset=0,limit=25,search="",status="ALL"):
    with Session() as s:
        q=select(Document).where(Document.job_id==job_id)
        if search:
            cleaned = search.replace("%",r"\%").replace("_",r"\_")
            q=q.where(or_(Document.r_object_id.ilike(f"%{cleaned}%",escape="\\"),Document.artifact_name.ilike(f"%{cleaned}%",escape="\\")))
        if status!="ALL":q=q.where(Document.status==status)
        count=s.scalar(select(func.count()).select_from(q.subquery()))
        docs=s.scalars(q.order_by(Document.r_object_id).offset(offset).limit(limit)).all()
        return dict(items=[_dict(x) for x in docs],total=count,offset=offset,limit=limit)

def _lock_query(s,q):
    return q.with_for_update(skip_locked=True) if s.bind.dialect.name=="postgresql" else q

@synchronized_claim
def claim_pending(limit=200):
    """Bounded fair claiming: reserve up to half a batch for small jobs.
    Bulk jobs keep making progress even during interactive traffic.
    """
    with Session.begin() as s:
        base=select(Document).join(Job,Document.job_id==Job.id).where(
            Job.cancelled.is_(False),
            or_(Document.status=="PENDING",and_(Document.status=="RESOLVING",Document.lease_until<now())))
        results=[];seen=set()
        for size,quota in (("small",limit//2),("bulk",limit)):
            q=base.where(Job.total<=5000 if size=="small" else Job.total>5000)                    .order_by(Job.created_at.desc() if size=="small" else Job.created_at.asc(),Document.r_object_id)                    .limit(quota if size=="small" else limit-len(results))
            rows=s.scalars(_lock_query(s,q)).all()
            for d in rows:
                if (d.job_id,d.r_object_id) in seen:continue
                d.status="RESOLVING";d.lease_until=now()+timedelta(minutes=5)
                results.append((d.job_id,d.r_object_id));seen.add((d.job_id,d.r_object_id))
        if len(results)<limit:
            q=base.order_by(Job.created_at.desc()).limit(limit-len(results))
            for d in s.scalars(_lock_query(s,q)).all():
                if (d.job_id,d.r_object_id) in seen:continue
                d.status="RESOLVING";d.lease_until=now()+timedelta(minutes=5)
                results.append((d.job_id,d.r_object_id));seen.add((d.job_id,d.r_object_id))
        return results


def _transition(s, d, new):
    old=d.status
    if old==new:return
    def bucket(st):
        if st in ("PENDING","RESOLVING","QUEUED"): return "queued"
        if st=="PROCESSING":return "processing"
        if st=="COMPLETED":return "completed"
        if st=="REVIEW_REQUIRED":return "review"
        return "failed"
    old_b,new_b=bucket(old),bucket(new)
    if old_b!=new_b:
        s.execute(update(Job).where(Job.id==d.job_id).values({getattr(Job,old_b):getattr(Job,old_b)-1,
                                                            getattr(Job,new_b):getattr(Job,new_b)+1}))
    d.status=new

def resolve_result(job_id,object_id,meta=None,reason=None,missing=False):
    with Session.begin() as s:
        d=s.get(Document,(job_id,object_id))
        if not d or d.status!="RESOLVING":return
        if meta:
            d.artifact_name=meta.get("artifact_name")
            d.source_status=meta.get("a_status")
            d.ingestion_status=meta.get("ingestion_status")
            d.s3_uri=meta.get("file_path")
        d.lease_until=None
        if reason:
            d.error=reason;_transition(s,d,"REVIEW_REQUIRED" if not missing else "NOT_FOUND")
        else:_transition(s,d,"QUEUED")

def resolve_results_batch(decisions):
    """One DB transaction for an entire Snowflake response, with aggregated job counters.
    Each decision is (job_id, R_OBJECT_ID, metadata or None, reason or None, missing bool).
    """
    if not decisions:return 0
    with Session.begin() as s:
        wanted=[(j,r) for j,r,*_ in decisions]
        found={ (d.job_id,d.r_object_id):d for d in s.scalars(
            select(Document).where(tuple_(Document.job_id,Document.r_object_id).in_(wanted))).all() }
        changes={}
        def bucket(st):
            if st in ("PENDING","RESOLVING","QUEUED"):return "queued"
            if st=="PROCESSING":return "processing"
            if st=="COMPLETED":return "completed"
            if st=="REVIEW_REQUIRED":return "review"
            return "failed"
        for jid,rid,meta,reason,missing in decisions:
            d=found.get((jid,rid))
            if not d or d.status!='RESOLVING':continue
            if meta:
                d.artifact_name=meta.get('artifact_name')
                d.source_status=meta.get('a_status')
                d.ingestion_status=meta.get('ingestion_status')
                d.s3_uri=meta.get('file_path')
            d.lease_until=None
            new=('NOT_FOUND' if missing else 'REVIEW_REQUIRED') if reason else 'QUEUED'
            d.error=reason
            old_bucket,new_bucket=bucket(d.status),bucket(new)
            if old_bucket!=new_bucket:
                delta=changes.setdefault(jid,{})
                delta[old_bucket]=delta.get(old_bucket,0)-1
                delta[new_bucket]=delta.get(new_bucket,0)+1
            d.status=new
        for jid,change in changes.items():
            vals={getattr(Job,k):getattr(Job,k)+v for k,v in change.items()}
            s.execute(update(Job).where(Job.id==jid).values(vals))
        return len(decisions)


@synchronized_claim
def claim_document():
    with Session.begin() as s:
        t=now()
        q=select(Document).where(or_(and_(Document.status=="QUEUED",or_(Document.available_at==None,Document.available_at<=t)),
                                    and_(Document.status=="PROCESSING",Document.lease_until<t)))\
             .order_by(Document.available_at,Document.job_id).limit(1)
        d=s.scalars(_lock_query(s,q)).first()
        if not d:return None
        if d.status=="PROCESSING":
            d.error="Recovered expired worker lease"  # old worker must not commit after a new lease owner
        else:_transition(s,d,"PROCESSING")
        d.lease_until=t+timedelta(seconds=config.LEASE_SECONDS)
        d.attempts+=1
        return dict(job_id=d.job_id,r_object_id=d.r_object_id,s3_uri=d.s3_uri,attempts=d.attempts,
                    lease_until=d.lease_until,source_version=d.source_version)

def heartbeat(task):
    with Session.begin() as s:
        d=s.get(Document,(task["job_id"],task["r_object_id"]))
        if d and d.status=="PROCESSING" and d.attempts==task["attempts"]:
            d.lease_until=now()+timedelta(seconds=config.LEASE_SECONDS)

def finish(task,fields,summary,status,note=None,source_version=None):
    with Session.begin() as s:
        d=s.get(Document,(task["job_id"],task["r_object_id"]))
        if not d or d.status!="PROCESSING" or d.attempts!=task["attempts"]:return False
        d.fields_json=json.dumps(fields,ensure_ascii=False);d.summary=summary
        d.note=note;d.error=None;d.lease_until=None
        d.source_version=source_version
        _transition(s,d,status)
        return True

def fail(task,error,retryable=True):
    with Session.begin() as s:
        d=s.get(Document,(task["job_id"],task["r_object_id"]))
        if not d or d.status!="PROCESSING" or d.attempts!=task["attempts"]:return False
        d.error=str(error)[:2000];d.lease_until=None
        if retryable and d.attempts<config.RETRY_LIMIT:
            d.available_at=now()+timedelta(seconds=min(300,2**d.attempts*5))
            _transition(s,d,"QUEUED")
        else:_transition(s,d,"FAILED")
        return True

def retry_failed(job_id):
    from .enterprise_state import StageTask
    with Session.begin() as s:
        j=s.get(Job,job_id)
        if not j or j.cancelled:return 0
        rows=s.scalars(select(Document).where(Document.job_id==job_id,Document.status=="FAILED").limit(5000)).all()
        for d in rows:
            d.attempts=0;d.available_at=None;d.error=None
            tasks=s.scalars(select(StageTask).where(StageTask.job_id==job_id,StageTask.document_id==d.r_object_id,StageTask.state=="FAILED")).all()
            for task in tasks:
                task.failures=0;task.state="READY";task.next_attempt_at=None;task.published_at=None;task.error_code=None
            _transition(s,d,"QUEUED" if d.s3_uri else "PENDING")
        return len(rows)

def seed_demo():
    if config.MODE!="demo":return
    with Session() as s:
        if s.get(Job,"demo-001"):return
    with Session.begin() as s:s.add(Job(id="demo-001",label="PBRER sample evaluation"))
    append_ids("demo-001",["0901bacc84b20d46","0901bacc8283f1de","0901bacc802bcfa8","UNKNOWN-DEMO-001"])


def cancel_job(job_id):
    from .enterprise_state import StageTask
    with Session.begin() as s:
        j=s.get(Job,job_id,with_for_update=(s.bind.dialect.name=="postgresql"))
        if not j:return False
        j.cancelled=True
        s.execute(update(StageTask).where(StageTask.job_id==job_id,StageTask.state.in_(["READY","RUNNING"]))
                  .values(state="CANCELLED",lease_token=None,lease_until=None))
        # Leave finished rows unchanged. Outstanding rows are terminal and counted as failed.
        rows=s.scalars(select(Document).where(Document.job_id==job_id,Document.status.in_(["PENDING","RESOLVING","QUEUED","PROCESSING"]))).all()
        for d in rows:_transition(s,d,"CANCELLED")
        return True


def job_security(job_id):
    with Session() as s:
        j=s.get(Job,job_id)
        return dict(owner=j.owner_id,tenant=j.tenant_id,read_all=j.read_all) if j else None
