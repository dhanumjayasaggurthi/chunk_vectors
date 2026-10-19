"""Durable task state. SQS is a wake-up transport; PostgreSQL owns task truth.

No distributed exactly-once claims. Tasks are at-least-once and idempotent.
A DB transaction creates task(s); dispatch retries messages after crashes.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import String, Integer, DateTime, ForeignKey, Index, Text, select, update, and_, or_, exists, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from . import config,store

class StageTask(store.Base):
    __tablename__ = "stage_tasks"
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64),index=True)
    document_id: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(24))
    chunk_number: Mapped[int] = mapped_column(Integer,default=-1)
    state: Mapped[str] = mapped_column(String(20),default="READY",index=True)
    attempts: Mapped[int] = mapped_column(Integer,default=0)
    failures: Mapped[int] = mapped_column(Integer,default=0)
    lease_token: Mapped[str|None] = mapped_column(String(36))
    lease_until: Mapped[datetime|None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime|None] = mapped_column(DateTime)
    published_at: Mapped[datetime|None] = mapped_column(DateTime)
    error_code: Mapped[str|None] = mapped_column(String(80))
    __table_args__=(Index("ix_stage_republish","state","next_attempt_at","published_at"),Index("ix_stage_document","job_id","document_id"),
                     UniqueConstraint("job_id","document_id","stage","chunk_number",name="uq_stage_unit"))

class PageChunk(store.Base):
    __tablename__="page_chunks"
    job_id: Mapped[str]=mapped_column(String(64),primary_key=True)
    document_id: Mapped[str]=mapped_column(String(128),primary_key=True)
    ordinal: Mapped[int]=mapped_column(Integer,primary_key=True)
    start_page: Mapped[int]=mapped_column(Integer)
    end_page: Mapped[int]=mapped_column(Integer)
    source_version: Mapped[str]=mapped_column(String(255))
    checkpoint_key: Mapped[str]=mapped_column(Text)
    ocr_key: Mapped[str|None]=mapped_column(Text)
    ocr_page_map: Mapped[str|None]=mapped_column(Text)
    ocr_operation: Mapped[str|None]=mapped_column(String(255))
    ocr_state: Mapped[str]=mapped_column(String(24),default="NOT_REQUIRED")
    error_code: Mapped[str|None]=mapped_column(String(80))


class DocumentManifest(store.Base):
    __tablename__='document_manifests'
    job_id:Mapped[str]=mapped_column(String(64),primary_key=True)
    document_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    source_version:Mapped[str]=mapped_column(String(255))
    expected_chunks:Mapped[int]=mapped_column(Integer)
    total_pages:Mapped[int]=mapped_column(Integer)
    state:Mapped[str]=mapped_column(String(16),default='PREPARING')


def put_task(s,job_id,document_id,stage,chunk_number=-1):
    """Creates work atomically within caller's database transaction."""
    t=StageTask(id=str(uuid4()),job_id=job_id,document_id=document_id,stage=stage,chunk_number=chunk_number)
    s.add(t)
    return t.id


def queue_prepared_documents(limit=200):
    """Used after Snowflake resolution; never queue a document twice."""
    with store.Session.begin() as s:
        q=select(store.Document).join(store.Job,store.Document.job_id==store.Job.id).where(store.Job.cancelled.is_(False),store.Document.status=="QUEUED",
           ~exists(select(StageTask.id).where(StageTask.job_id==store.Document.job_id,
                                             StageTask.document_id==store.Document.r_object_id))).order_by(
                  store.Job.total.asc()).limit(limit)
        if s.bind.dialect.name=="postgresql":q=q.with_for_update(skip_locked=True)
        docs=s.scalars(q).all()
        count=0
        for d in docs:
            existing=s.scalar(select(StageTask.id).where(StageTask.job_id==d.job_id,StageTask.document_id==d.r_object_id).limit(1))
            if existing:continue
            put_task(s,d.job_id,d.r_object_id,"PREPARE")
            count+=1
        return count


def due_to_publish(limit=100):
    """Race-safe outbox listing. Re-send is safe because claiming task is fenced."""
    t=store.now()
    with store.Session.begin() as s:
        q=select(StageTask).where(StageTask.state=="READY",or_(StageTask.next_attempt_at.is_(None),StageTask.next_attempt_at<=t),
            or_(StageTask.published_at.is_(None),StageTask.published_at<t-timedelta(seconds=config.TASK_REPUBLISH_SECONDS)))\
          .order_by(StageTask.published_at).limit(limit)
        if s.bind.dialect.name=="postgresql":q=q.with_for_update(skip_locked=True)
        rows=s.scalars(q).all()
        ids=[x.id for x in rows]
        # Reserve publication for this dispatcher; a crash is recovered by the republish timer.
        for r in rows:r.published_at=t
        return ids


def claim(task_id):
    with store.Session.begin() as s:
        t=s.get(StageTask,task_id,with_for_update=(s.bind.dialect.name=="postgresql"))
        if not t:return None
        now=store.now()
        if t.state in ("DONE","FAILED","CANCELLED"):return None
        job=s.get(store.Job,t.job_id)
        if not job or job.cancelled:return None
        if t.state=="RUNNING" and t.lease_until and t.lease_until>now:return None
        if t.next_attempt_at and t.next_attempt_at>now:return None
        t.state="RUNNING";t.attempts+=1;t.lease_token=str(uuid4());t.lease_until=now+timedelta(seconds=config.TASK_LEASE_SECONDS)
        return dict(id=t.id,job_id=t.job_id,document_id=t.document_id,stage=t.stage,
                    chunk_number=t.chunk_number,token=t.lease_token,attempts=t.attempts)


def heartbeat(task):
    with store.Session.begin() as s:
        n=s.execute(update(StageTask).where(StageTask.id==task['id'],StageTask.state=="RUNNING",
             StageTask.lease_token==task['token']).values(lease_until=store.now()+timedelta(seconds=config.TASK_LEASE_SECONDS))).rowcount
        return bool(n)


def done(task,callback=None):
    with store.Session.begin() as s:
        t=s.get(StageTask,task['id'],with_for_update=(s.bind.dialect.name=="postgresql"))
        if not t or t.state!="RUNNING" or t.lease_token!=task['token']:return False
        if callback:callback(s,t)
        t.state="DONE";t.lease_token=None;t.lease_until=None
        return True


def defer(task,seconds=60,next_stage=None):
    """Yield worker during remote OCR. SQS message is acknowledged, outbox republishes."""
    with store.Session.begin() as s:
        t=s.get(StageTask,task['id'],with_for_update=(s.bind.dialect.name=="postgresql"))
        if not t or t.state!="RUNNING" or t.lease_token!=task['token']:return False
        t.state="READY";t.lease_token=None;t.lease_until=None;t.published_at=None
        t.next_attempt_at=store.now()+timedelta(seconds=seconds)
        if next_stage:t.stage=next_stage
        return True


def error(task,code,retryable=True):
    with store.Session.begin() as s:
        t=s.get(StageTask,task['id'],with_for_update=(s.bind.dialect.name=="postgresql"))
        if not t or t.state!="RUNNING" or t.lease_token!=task['token']:return False
        t.error_code=code[:80];t.lease_until=None;t.lease_token=None;t.published_at=None
        t.failures+=1
        if retryable and t.failures<config.TASK_MAX_ATTEMPTS:
            t.state="READY";t.next_attempt_at=store.now()+timedelta(seconds=min(900, 5*(2**min(t.attempts,7))))
        else:
            t.state="FAILED"
            if t.stage in ("INGEST","EXPORT"):
                from .bulk import InputUpload,JobExport
                entry=s.get(InputUpload,t.document_id) if t.stage=="INGEST" else s.get(JobExport,t.document_id)
                if entry:entry.state="FAILED"
            d=s.get(store.Document,(t.job_id,t.document_id))
            if d and d.status not in ("COMPLETED","REVIEW_REQUIRED","FAILED","CANCELLED"):
                d.error=code[:80]
                store._transition(s,d,"FAILED")
        return True


def schedule_final_if_ready(s,job_id,document_id):
    manifest=s.get(DocumentManifest,(job_id,document_id),with_for_update=(s.bind.dialect.name=="postgresql"))
    if not manifest or manifest.state!="PREPARED":return False
    chunks=s.scalars(select(PageChunk).where(PageChunk.job_id==job_id,PageChunk.document_id==document_id)).all()
    if len(chunks)!=manifest.expected_chunks or any(c.ocr_state not in ("DONE","NOT_REQUIRED") for c in chunks):return False
    existing=s.scalar(select(StageTask.id).where(StageTask.job_id==job_id,StageTask.document_id==document_id,StageTask.stage=="FINALIZE").limit(1))
    if not existing:put_task(s,job_id,document_id,"FINALIZE")
    return True


def unstick_expired(limit=200):
    """Reconciliation after container crashes or queue visibility expiration."""
    with store.Session.begin() as s:
        q=select(StageTask).where(StageTask.state=="RUNNING",StageTask.lease_until<store.now()).limit(limit)
        if s.bind.dialect.name=="postgresql":q=q.with_for_update(skip_locked=True)
        tasks=s.scalars(q).all()
        for t in tasks:
            t.state="READY";t.lease_token=None;t.lease_until=None;t.published_at=None
        return len(tasks)
