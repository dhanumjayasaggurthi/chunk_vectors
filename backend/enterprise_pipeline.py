"""Resumable large-PDF pipeline.

PREPARE: download once to bounded local scratch, checkpoint native text every N pages,
         publish asynchronous OCR work for chunks needing OCR.
OCR_START: submit a small PDF containing only unreadable pages to Textract.
OCR_POLL: check remote operation without occupying a worker between polls.
FINALIZE: evidence extraction from persisted page text; never full-PDF prompt.

Every stage is retryable; evidence chunks in S3 survive a crash.
"""
from __future__ import annotations
import gzip, hashlib, io, json, logging, os, re, tempfile
from contextlib import contextmanager
from urllib.parse import urlsplit
from typing import Iterator
import fitz
from . import config, store
from .enterprise_state import PageChunk, DocumentManifest, put_task, schedule_final_if_ready
from .integrations import PermanentError
from .extract import rule_extract,FIELD_NAMES,CRITICAL,_verify_llm_field
from .azure_llm import JnJAzureLLM,page_score
from sqlalchemy import select

log=logging.getLogger(__name__)

class Checkpoints:
    def __init__(self,s3=None):
        if not config.CHECKPOINT_BUCKET:raise RuntimeError('CHECKPOINT_BUCKET must be configured')
        if s3 is None:
            import boto3
            s3=boto3.client('s3')
        self.s3=s3
    def key(self,job,doc,version,chunk,kind='pages'):
        # No document content or arbitrary submitted paths in checkpoint object keys.
        h=hashlib.sha256(f'{job}\0{doc}\0{version}\0{config.PROFILE_VERSION}'.encode()).hexdigest()
        return f'{config.CHECKPOINT_PREFIX.rstrip("/")}/{h}/{chunk:06d}-{kind}.gz'
    def put(self,key,obj):
        data=gzip.compress(json.dumps(obj,ensure_ascii=False).encode('utf-8'))
        self.s3.put_object(Bucket=config.CHECKPOINT_BUCKET,Key=key,Body=data,ContentType='application/json',ContentEncoding='gzip',ServerSideEncryption='AES256')
    def get(self,key):
        r=self.s3.get_object(Bucket=config.CHECKPOINT_BUCKET,Key=key)
        with r['Body'] as body:return json.loads(gzip.decompress(body.read()))
    def put_pdf(self,key,data):
        self.s3.put_object(Bucket=config.CHECKPOINT_BUCKET,Key=key,Body=data,ContentType='application/pdf',ServerSideEncryption='AES256')

@contextmanager
def local_pdf(uri):
    """Stream an authorized, version-fenced S3 object into scratch; never buffer whole PDF in RAM."""
    import boto3
    if not uri.startswith('s3://'):raise PermanentError('Invalid S3 URI')
    parts=urlsplit(uri)
    bucket,key=parts.netloc,parts.path.lstrip('/')
    if not bucket or not key or bucket not in config.ALLOWED_S3_BUCKETS:raise PermanentError('Unauthorized S3 location')
    if config.ALLOWED_S3_PREFIXES and not key.startswith(config.ALLOWED_S3_PREFIXES):raise PermanentError('S3 path outside approved prefixes')
    client=boto3.client('s3')
    head=client.head_object(Bucket=bucket,Key=key)
    size=int(head['ContentLength'])
    if size<=0 or size>config.MAX_FILE_MB*1024*1024:raise PermanentError('PDF outside configured size limit')
    version=head.get('VersionId') or head['ETag'].strip('"')
    kwargs={'Bucket':bucket,'Key':key}
    if head.get('VersionId'):kwargs['VersionId']=head['VersionId']
    else:kwargs['IfMatch']=head['ETag']
    path=None
    try:
        fd,path=tempfile.mkstemp(suffix='.pdf')
        with os.fdopen(fd,'wb') as out:
            response=client.get_object(**kwargs)
            with response['Body'] as body:
                downloaded=0
                for block in body.iter_chunks(chunk_size=1024*1024):
                    downloaded+=len(block)
                    if downloaded>config.MAX_FILE_MB*1024*1024:raise PermanentError('PDF exceeded size while downloading')
                    out.write(block)
        if downloaded!=size:raise IOError('Incomplete S3 read')
        with open(path,'rb') as f:
            if f.read(5)!=b'%PDF-':raise PermanentError('Object is not PDF')
        yield path,version
    finally:
        if path and os.path.exists(path):os.unlink(path)


def prepare(task):
    cp=Checkpoints()
    job,doc=task['job_id'],task['document_id']
    with store.Session() as s:
        d=s.get(store.Document,(job,doc))
        if not d or not d.s3_uri:raise PermanentError('Missing resolved S3 source')
        uri=d.s3_uri
    with local_pdf(uri) as (path,version):
        with fitz.open(path) as pdf:
            if pdf.needs_pass:raise PermanentError('PDF is password-protected')
            if pdf.page_count>config.MAX_PAGES:raise PermanentError('PDF exceeds maximum supported pages')
            page_count=pdf.page_count
            if page_count==0:raise PermanentError('Empty PDF')
            expected_chunks=(page_count+config.CHUNK_PAGES-1)//config.CHUNK_PAGES
            with store.Session.begin() as s:
                manifest=s.get(DocumentManifest,(job,doc))
                if manifest and manifest.source_version!=version:raise PermanentError('Document source changed; submit a new job')
                if not manifest:
                    s.add(DocumentManifest(job_id=job,document_id=doc,source_version=version,
                          expected_chunks=expected_chunks,total_pages=page_count,state='PREPARING'))
            for start in range(0,page_count,config.CHUNK_PAGES):
                with store.Session() as s:
                    j=s.get(store.Job,job)
                    if j and j.cancelled:raise PermanentError('Job cancelled')
                idx=start//config.CHUNK_PAGES
                end=min(start+config.CHUNK_PAGES,page_count)
                key=cp.key(job,doc,version,idx)
                with store.Session() as s:
                    previous=s.get(PageChunk,(job,doc,idx))
                    if previous and previous.source_version==version and previous.checkpoint_key==key and previous.ocr_state in ('NOT_REQUIRED','READY','WAITING','DONE'):
                        # The checkpoint object exists because it was written before DB commit.
                        continue
                pages=[];missing=[]
                for pidx in range(start,end):
                    page=pdf[pidx]
                    text=page.get_text('text',sort=True) or ''
                    pages.append({'page':pidx+1,'text':text})
                    if len(re.sub(r'\s+','',text))<40 and page.get_images():missing.append(pidx)
                cp.put(key,pages)
                ocr_key=None
                if missing and config.OCR_ENGINE=='textract_async':
                    sub=fitz.open()
                    try:
                        for pidx in missing:sub.insert_pdf(pdf,from_page=pidx,to_page=pidx)
                        # Textract async accepts <= 500MB, 3000 pages; partitions stay <= CHUNK_PAGES.
                        raw=sub.tobytes(garbage=3,deflate=True)
                        if len(raw)<=500*1024*1024:
                            ocr_key=cp.key(job,doc,version,idx,'ocr').removesuffix('.gz')+'.pdf'
                            cp.put_pdf(ocr_key,raw)
                        else:log.warning('OCR segment too large; requires manual review: chunk=%d',idx)
                    finally:sub.close()
                ocr_state='READY' if ocr_key else ('NOT_REQUIRED' if not missing else 'UNAVAILABLE')
                with store.Session.begin() as s:
                    old=s.get(PageChunk,(job,doc,idx))
                    if old and old.source_version!=version:
                        raise PermanentError('Source version changed between processing attempts; submit a new job')
                    if not old:
                        old=PageChunk(job_id=job,document_id=doc,ordinal=idx,start_page=start+1,end_page=end,
                                      source_version=version,checkpoint_key=key,ocr_key=ocr_key,
                                      ocr_page_map=json.dumps([x+1 for x in missing]),ocr_state=ocr_state)
                        s.add(old)
                    if ocr_key:put_task(s,job,doc,'OCR_START',idx)
            with store.Session.begin() as s:
                d=s.get(store.Document,(job,doc))
                manifest=s.get(DocumentManifest,(job,doc))
                manifest.state='PREPARED'
                d.source_version=version
                d.note=f'Pages: {page_count}; checkpoint size: {config.CHUNK_PAGES}'
                chunks=s.scalars(select(PageChunk).where(PageChunk.job_id==job,PageChunk.document_id==doc)).all()
                if any(c.ocr_state=='UNAVAILABLE' for c in chunks):
                    # Mark incomplete but still extract native evidence; status will require review.
                    for c in chunks:
                        if c.ocr_state=='UNAVAILABLE':
                            c.ocr_state='DONE';c.error_code='OCR_NOT_PERFORMED'
                schedule_final_if_ready(s,job,doc)
    return page_count


def ocr_start(task):
    import boto3
    with store.Session() as s:
        c=s.get(PageChunk,(task['job_id'],task['document_id'],task['chunk_number']))
        if not c:raise PermanentError('Missing OCR chunk manifest')
        if c.ocr_state in ('WAITING','DONE'):return
        key=c.ocr_key
    if not key:raise PermanentError('No OCR document')
    token=hashlib.sha256(f'{task["job_id"]}|{task["document_id"]}|{task["chunk_number"]}|{config.PROFILE_VERSION}'.encode()).hexdigest()[:64]
    tx=boto3.client('textract')
    result=tx.start_document_text_detection(DocumentLocation={'S3Object':{'Bucket':config.CHECKPOINT_BUCKET,'Name':key}},ClientRequestToken=token,
                                            OutputConfig={'S3Bucket':config.CHECKPOINT_BUCKET,
                                                          'S3Prefix':config.CHECKPOINT_PREFIX.strip('/')+'/textract/'})
    with store.Session.begin() as s:
        c=s.get(PageChunk,(task['job_id'],task['document_id'],task['chunk_number']))
        c.ocr_operation=result['JobId'];c.ocr_state='WAITING'


def ocr_poll(task):
    import boto3
    cp=Checkpoints()
    with store.Session() as s:
        chunk=s.get(PageChunk,(task['job_id'],task['document_id'],task['chunk_number']))
        if not chunk:raise PermanentError('No OCR chunk')
        if chunk.ocr_state=='DONE':return True
        if not chunk.ocr_operation:raise PermanentError('OCR operation not submitted')
        operation=chunk.ocr_operation; pages=json.loads(chunk.ocr_page_map or '[]');key=chunk.checkpoint_key
    tx=boto3.client('textract')
    response=tx.get_document_text_detection(JobId=operation,MaxResults=1000)
    status=response['JobStatus']
    if status=='IN_PROGRESS':return False
    if status not in ('SUCCEEDED','PARTIAL_SUCCESS'):raise RuntimeError('OCR provider returned '+status)
    lines={}
    while True:
        for block in response.get('Blocks',[]):
            if block.get('BlockType')=='LINE':lines.setdefault(int(block['Page']),[]).append(block['Text'])
        token=response.get('NextToken')
        if not token:break
        response=tx.get_document_text_detection(JobId=operation,NextToken=token,MaxResults=1000)
    native=cp.get(key)
    by_page={p['page']:p for p in native}
    for relative,texts in lines.items():
        if 1<=relative<=len(pages):by_page[pages[relative-1]]['text']='\n'.join(texts)
    # Write first, then mark DB DONE; crash-safe retry overwrites same key.
    cp.put(key,list(by_page.values()))
    with store.Session.begin() as s:
        c=s.get(PageChunk,(task['job_id'],task['document_id'],task['chunk_number']))
        c.ocr_state='DONE'
        if status!='SUCCEEDED' or any(not lines.get(i) for i in range(1,len(pages)+1)):
            c.error_code='OCR_PARTIAL_OR_EMPTY'
        schedule_final_if_ready(s,task['job_id'],task['document_id'])
    return True


def finalize(task):
    from . import extract
    job,doc=task['job_id'],task['document_id']
    with store.Session() as s:
        current=s.get(store.Document,(job,doc))
        if current and current.status in ('COMPLETED','REVIEW_REQUIRED','FAILED','CANCELLED'):return
    cp=Checkpoints()
    with store.Session() as s:
        manifest=s.get(DocumentManifest,(job,doc))
        chunks=s.scalars(select(PageChunk).where(PageChunk.job_id==job,PageChunk.document_id==doc).order_by(PageChunk.ordinal)).all()
    if not manifest or manifest.state!='PREPARED' or len(chunks)!=manifest.expected_chunks:raise RuntimeError('Document checkpoint manifest is incomplete')
    if not chunks or any(c.ocr_state not in ('DONE','NOT_REQUIRED') for c in chunks):
        raise RuntimeError('OCR chunks are not ready')
    # Complete rules pass over ALL pages while retaining only evidence-relevant pages for the LLM.
    fields={};conflicts=set(); candidates=[]; OCR_ERRORS=[]
    for chunk in chunks:
        if chunk.error_code:OCR_ERRORS.append(chunk.ordinal)
        pages=cp.get(chunk.checkpoint_key)
        matches=rule_extract(pages)
        for key,value in matches.items():
            if key in fields and fields[key]['value']!=value['value']:conflicts.add(key)
            else:fields.setdefault(key,value)
        for p in pages:
            score=page_score(p,fields.get('product',{}).get('value',''))
            if score>0 or p['page']<=2 or p['page']>=manifest.total_pages-1:
                candidates.append((score,p))
    # Preserve pages where deterministic fields were verified, plus cover/tail,
    # then fill with distinct high-scoring evidence pages. Avoid full-PDF prompts.
    by_page={p['page']:p for _,p in candidates}
    chosen=[];seen=set()
    mandatory=[*sorted({v['page'] for v in fields.values()}),1,2,manifest.total_pages-1,manifest.total_pages]
    for page_no in mandatory:
        if page_no in by_page and page_no not in seen:
            chosen.append(by_page[page_no]);seen.add(page_no)
    candidates.sort(key=lambda t:(-t[0],t[1]['page']))
    for score,p in candidates:
        if len(chosen)>=50:break
        if p['page'] not in seen:
            chosen.append(p);seen.add(p['page'])
    shortlisted=chosen
    summary=None;note=[]
    if conflicts:note.append('Conflicting values: '+', '.join(sorted(conflicts)))
    if OCR_ERRORS:note.append('OCR incomplete on chunks '+', '.join(map(str,OCR_ERRORS)))
    try:
        llm=JnJAzureLLM()
        answer,_=llm.extract(shortlisted,fields)
        for name in FIELD_NAMES:
            if name not in fields:
                item=extract._verify_llm_field(name,(answer.get('fields') or {}).get(name),shortlisted)
                if item:fields[name]=item
        s=answer.get('summary') or {}
        cites=s.get('evidence_pages',[]) if isinstance(s,dict) else []
        if isinstance(s,dict) and isinstance(s.get('text'),str) and cites and all(any(p['page']==n for p in shortlisted) for n in cites):
            summary=s['text'][:1000]
    except Exception:
        log.exception('Azure LLM failed during finalization')
        note.append('AI summary unavailable')
    missing=[x for x in CRITICAL if x not in fields]
    if missing:note.append('Missing evidence: '+', '.join(missing))
    if not summary:note.append('Summary pending validation')
    status='REVIEW_REQUIRED' if note else 'COMPLETED'
    # Reuse existing document-state counters. Completion is fenced by task lease at stage level.
    with store.Session.begin() as s:
        d=s.get(store.Document,(job,doc),with_for_update=(s.bind.dialect.name=='postgresql'))
        if not d or d.status in ('COMPLETED','REVIEW_REQUIRED','FAILED','CANCELLED'):return
        d.fields_json=json.dumps(fields,ensure_ascii=False);d.summary=summary
        d.note='; '.join(note) or 'Summary is an AI-generated draft with evidence-page references'
        d.error=None
        store._transition(s,d,status)
