"""Resumable browser uploads and asynchronous S3 result exports.
All S3 objects are server-selected; no user-controlled bucket or key permitted.
"""
from __future__ import annotations
import csv,io,json,os,re,tempfile
from datetime import datetime
from uuid import uuid4
from sqlalchemy import String, DateTime, ForeignKey, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from . import store,config
from .enterprise_state import put_task

class InputUpload(store.Base):
    __tablename__='input_uploads'
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    job_id:Mapped[str]=mapped_column(String(64),index=True)
    object_key:Mapped[str]=mapped_column(Text)
    multipart_id:Mapped[str]=mapped_column(Text)
    state:Mapped[str]=mapped_column(String(16),default='UPLOADING')
    parts_json:Mapped[str|None]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=store.now)

class JobExport(store.Base):
    __tablename__='job_exports'
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    job_id:Mapped[str]=mapped_column(String(64),index=True)
    format:Mapped[str]=mapped_column(String(8))
    state:Mapped[str]=mapped_column(String(16),default='PENDING')
    object_key:Mapped[str|None]=mapped_column(Text)
    error:Mapped[str|None]=mapped_column(String(120))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=store.now)

def _s3():
    import boto3
    if not config.CHECKPOINT_BUCKET:raise RuntimeError('CHECKPOINT_BUCKET required')
    return boto3.client('s3')

def start_upload(job_id):
    upload_id=str(uuid4())
    key=f'{config.CHECKPOINT_PREFIX.rstrip("/")}/input/{job_id}/{upload_id}.csv'
    s3=_s3()
    result=s3.create_multipart_upload(Bucket=config.CHECKPOINT_BUCKET,Key=key,ContentType='text/csv',ServerSideEncryption='AES256')
    with store.Session.begin() as s:
        s.add(InputUpload(id=upload_id,job_id=job_id,object_key=key,multipart_id=result['UploadId']))
    return upload_id

def get_upload(job_id,upload_id):
    with store.Session() as s:
        u=s.get(InputUpload,upload_id)
        return dict(id=u.id,job_id=u.job_id,object_key=u.object_key,multipart_id=u.multipart_id,state=u.state,parts=json.loads(u.parts_json or '[]')) if u and u.job_id==job_id else None

def presign_part(job_id,upload_id,part):
    u=get_upload(job_id,upload_id)
    if not u or u['state']!='UPLOADING':raise ValueError('Upload not available')
    if not 1<=part<=10000:raise ValueError('Part number out of range')
    return _s3().generate_presigned_url('upload_part',Params=dict(Bucket=config.CHECKPOINT_BUCKET,
            Key=u['object_key'],UploadId=u['multipart_id'],PartNumber=part),ExpiresIn=900)

def uploaded_parts(job_id,upload_id):
    u=get_upload(job_id,upload_id)
    if not u:raise ValueError('Upload not found')
    if u['state']!='UPLOADING':return {'state':u['state'],'parts':u['parts']}
    sqs=_s3();parts=[];part_marker=0
    while True:
        result=sqs.list_parts(Bucket=config.CHECKPOINT_BUCKET,Key=u['object_key'],UploadId=u['multipart_id'],PartNumberMarker=part_marker)
        parts.extend({'PartNumber':int(p['PartNumber']),'ETag':p['ETag']} for p in result.get('Parts',[]))
        if not result.get('IsTruncated'):break
        part_marker=result['NextPartNumberMarker']
    return {'state':u['state'],'parts':parts}

def complete_upload(job_id,upload_id,parts):
    """Completion survives a crash between S3 complete and the DB transaction.
    Source of truth for in-progress parts is durable; HEAD reconciles NoSuchUpload.
    """
    u=get_upload(job_id,upload_id)
    if not u:raise ValueError('Upload not found')
    if u['state'] in ('READY','INGESTED'):
        return {'job_id':job_id,'upload_id':upload_id,'state':'QUEUED'}
    if u['state'] not in ('UPLOADING','COMPLETING'):raise ValueError('Upload unavailable')
    if u['state']=='UPLOADING':
        if not parts or len(parts)>10000 or any(int(p['PartNumber'])!=i+1 for i,p in enumerate(parts)):
            raise ValueError('Parts must be contiguous and ordered')
        with store.Session.begin() as s:
            x=s.get(InputUpload,upload_id,with_for_update=(s.bind.dialect.name=='postgresql'))
            x.state='COMPLETING';x.parts_json=json.dumps(parts)
    else:parts=u['parts']
    s3=_s3()
    try:
        s3.complete_multipart_upload(Bucket=config.CHECKPOINT_BUCKET,Key=u['object_key'],UploadId=u['multipart_id'],
                 MultipartUpload={'Parts':parts})
    except Exception as exc:
        try:s3.head_object(Bucket=config.CHECKPOINT_BUCKET,Key=u['object_key'])
        except Exception:raise exc
    with store.Session.begin() as s:
        x=s.get(InputUpload,upload_id,with_for_update=(s.bind.dialect.name=='postgresql'))
        if x.state=='COMPLETING':
            x.state='READY'
            put_task(s,job_id,upload_id,'INGEST')
    return {'job_id':job_id,'upload_id':upload_id,'state':'QUEUED'}


def ingest_upload(task):
    upload_id=task['document_id']
    u=get_upload(task['job_id'],upload_id)
    if not u or u['state'] not in ('READY','INGESTING'):return
    s3=_s3();response=s3.get_object(Bucket=config.CHECKPOINT_BUCKET,Key=u['object_key'])
    chunk=[];lines=0;invalid=0
    with response['Body'] as body:
        # iter_lines streams network chunks; csv.reader handles quoted CSV fields.
        reader=csv.reader((line.decode('utf-8-sig') for line in body.iter_lines(chunk_size=65536)))
        first=next(reader,None)
        if first is None:raise ValueError('Uploaded CSV is empty')
        header=[x.strip().lower() for x in first]
        idx=header.index('r_object_id') if 'r_object_id' in header else 0
        import itertools
        source=reader if 'r_object_id' in header else itertools.chain([first],reader)
        for row in source:
            lines+=1
            if lines>20_000_000:raise ValueError('Upload exceeds 20 million IDs')
            value=row[idx].strip() if len(row)>idx else ''
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):invalid+=1;continue
            chunk.append(value)
            if len(chunk)==1000:store.append_ids(task['job_id'],chunk);chunk=[]
        if chunk:store.append_ids(task['job_id'],chunk)
    with store.Session.begin() as s:
        x=s.get(InputUpload,upload_id)
        x.state='INGESTED'
    return {'read':lines,'invalid':invalid}


def create_export(job_id,format='csv'):
    if format not in ('csv','json'):raise ValueError('Unsupported format')
    export_id=str(uuid4())
    with store.Session.begin() as s:
        s.add(JobExport(id=export_id,job_id=job_id,format=format))
        put_task(s,job_id,export_id,'EXPORT')
    return export_id

def export_status(job_id,export_id):
    with store.Session() as s:
        e=s.get(JobExport,export_id)
        return {'state':e.state,'error':e.error,'format':e.format,'key':e.object_key} if e and e.job_id==job_id else None

def _iter_job_documents(job_id):
    """Keyset scan. No unbounded offsets or ORM identity accumulation."""
    last=None
    while True:
        with store.Session() as s:
            q=select(store.Document).where(store.Document.job_id==job_id)
            if last is not None:q=q.where(store.Document.r_object_id>last)
            rows=s.scalars(q.order_by(store.Document.r_object_id).limit(1000)).all()
            if not rows:return
            for d in rows:
                yield store._dict(d)
                last=d.r_object_id


def generate_export(task):
    with store.Session() as s:
        e=s.get(JobExport,task['document_id'])
        if not e or e.job_id!=task['job_id']:return
        fmt=e.format
    key=f'{config.CHECKPOINT_PREFIX.rstrip("/")}/export/{task["job_id"]}/{task["document_id"]}.{fmt}'
    with tempfile.TemporaryFile('w+b') as out:
        text=io.TextIOWrapper(out,encoding='utf-8',newline='',write_through=True)
        if fmt=='csv':
            keys=['r_object_id','status','artifact_name','document_date','international_birth_date','product','active_ingredient','summary','error']
            writer=csv.DictWriter(text,fieldnames=keys);writer.writeheader()
            for d in _iter_job_documents(task['job_id']):
                fields=d['fields'];row={k:d.get(k) for k in keys}
                for k in ('document_date','international_birth_date','product','active_ingredient'):
                    row[k]=fields.get(k,{}).get('value')
                for k in keys:
                    v=row.get(k)
                    if v is not None and str(v).lstrip().startswith(('=','+','-','@')):row[k]="'"+str(v)
                writer.writerow(row)
        else:
            text.write('[\n');first=True
            for d in _iter_job_documents(task['job_id']):
                if not first:text.write(',\n')
                text.write(json.dumps(d,ensure_ascii=False));first=False
            text.write('\n]')
        text.flush();out.seek(0)
        _s3().upload_fileobj(out,config.CHECKPOINT_BUCKET,key,ExtraArgs={'ServerSideEncryption':'AES256',
                      'ContentType':'text/csv' if fmt=='csv' else 'application/json'})
    with store.Session.begin() as s:
        e=s.get(JobExport,task['document_id'])
        e.state='COMPLETED';e.object_key=key
    return key

def export_download_url(job_id,export_id):
    e=export_status(job_id,export_id)
    if not e or e['state']!='COMPLETED':return None
    return _s3().generate_presigned_url('get_object',Params={'Bucket':config.CHECKPOINT_BUCKET,
                                'Key':e['key'],'ResponseContentDisposition':f'attachment; filename="{job_id}.{e["format"]}"'},ExpiresIn=600)
