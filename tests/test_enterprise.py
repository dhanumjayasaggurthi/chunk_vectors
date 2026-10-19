"""Enterprise regression checks: idempotent work, resumability, big PDFs, and isolation."""
from __future__ import annotations
from pathlib import Path
import fitz
from backend import config,store,enterprise_state
from backend import enterprise_pipeline as pipeline


def test_job_isolation_and_cancellation():
    store.init_db()
    jid=store.create_job('owner-isolated',owner_id='alice',tenant_id='org-1')
    store.append_ids(jid,['A','B'])
    assert store.job_get(jid,'alice','org-1')
    assert store.job_get(jid,'bob','org-1') is None
    assert store.job_get(jid,'alice','org-2') is None
    assert store.cancel_job(jid)
    j=store.job_get(jid,'alice','org-1')
    assert j['status']=='CANCELLED' and j['counts']['failed']==2


def test_durable_claim_fencing_retry_and_defer():
    store.init_db()
    jid=store.create_job('queue-test')
    store.append_ids(jid,['DOC-A'])
    with store.Session.begin() as s:
        t=enterprise_state.put_task(s,jid,'DOC-A','OCR_START',0)
    assert t in enterprise_state.due_to_publish(500)
    x=enterprise_state.claim(t)
    assert x and enterprise_state.claim(t) is None
    assert enterprise_state.heartbeat(x)
    assert enterprise_state.defer(x,seconds=0,next_stage='OCR_POLL')
    y=enterprise_state.claim(t)
    assert y and y['token']!=x['token']
    assert enterprise_state.done(x) is False  # previous lease owner is fenced
    assert enterprise_state.done(y)
    assert enterprise_state.claim(t) is None  # completed messages are harmless


def test_failed_stage_marks_only_its_document():
    store.init_db()
    jid=store.create_job('failure isolation')
    store.append_ids(jid,['BAD','GOOD'])
    with store.Session.begin() as s:
        good=enterprise_state.put_task(s,jid,'GOOD','PREPARE')
        bad=enterprise_state.put_task(s,jid,'BAD','PREPARE')
    b=enterprise_state.claim(bad)
    assert enterprise_state.error(b,'CorruptPDF',retryable=False)
    assert store.document_get(jid,'BAD')['status']=='FAILED'
    assert store.document_get(jid,'GOOD')['status']=='PENDING'
    assert enterprise_state.claim(good)


def test_prepare_checkpoints_more_than_1600_pages(monkeypatch,tmp_path):
    """No S3/OCR calls needed: native extraction still chunks and resumes."""
    pdf_path=tmp_path/'big.pdf'
    with fitz.open() as d:
        for i in range(1601):
            p=d.new_page()
            if i==1530:p.insert_text((50,50),'International Birth Date is 12 December 2008.')
        d.save(pdf_path)
    from contextlib import contextmanager
    @contextmanager
    def source(_):yield str(pdf_path),'immutable-v1'
    calls={}
    class FakeCheckpoint:
        def __init__(self):pass
        def key(self,job,doc,version,chunk,kind='pages'):
            return f'{job}-{doc}-{version}-{chunk}-{kind}'
        def put(self,key,items):calls[key]=items
        def put_pdf(self,key,raw):calls[key]=raw
    monkeypatch.setattr(pipeline,'local_pdf',source)
    monkeypatch.setattr(pipeline,'Checkpoints',FakeCheckpoint)
    monkeypatch.setattr(config,'CHUNK_PAGES',64)
    jid=store.create_job('large document')
    store.append_ids(jid,['DOC-1601'])
    with store.Session.begin() as s:
        d=s.get(store.Document,(jid,'DOC-1601'))
        d.s3_uri='s3://allowed/path.pdf'
    task={'job_id':jid,'document_id':'DOC-1601'}
    assert pipeline.prepare(task)==1601
    with store.Session() as s:
        m=s.get(enterprise_state.DocumentManifest,(jid,'DOC-1601'))
        assert (m.state,m.expected_chunks,m.total_pages)==('PREPARED',26,1601)
        chunks=s.query(enterprise_state.PageChunk).filter_by(job_id=jid,document_id='DOC-1601').all()
        assert len(chunks)==26
        finals=s.query(enterprise_state.StageTask).filter_by(job_id=jid,document_id='DOC-1601',stage='FINALIZE').all()
        assert len(finals)==1
    n=len(calls)
    pipeline.prepare(task)
    assert len(calls)==n  # checkpoint reuse; no repeated parsing/writes
    assert any('International Birth Date' in p['text'] for v in calls.values() if isinstance(v,list) for p in v)


def test_manifest_prevents_premature_finalize():
    store.init_db()
    jid=store.create_job('barrier')
    with store.Session.begin() as s:
        s.add(enterprise_state.DocumentManifest(job_id=jid,document_id='D',source_version='v1',expected_chunks=2,total_pages=1601,state='PREPARING'))
        s.add(enterprise_state.PageChunk(job_id=jid,document_id='D',ordinal=0,start_page=1,end_page=64,source_version='v1',checkpoint_key='k',ocr_state='DONE'))
        assert not enterprise_state.schedule_final_if_ready(s,jid,'D')
    with store.Session.begin() as s:
        s.add(enterprise_state.PageChunk(job_id=jid,document_id='D',ordinal=1,start_page=65,end_page=1601,source_version='v1',checkpoint_key='k2',ocr_state='DONE'))
        s.get(enterprise_state.DocumentManifest,(jid,'D')).state='PREPARED'
        assert enterprise_state.schedule_final_if_ready(s,jid,'D')
        assert enterprise_state.schedule_final_if_ready(s,jid,'D')  # idempotent task creation
    with store.Session() as s:
        assert s.query(enterprise_state.StageTask).filter_by(job_id=jid,document_id='D',stage='FINALIZE').count()==1


def test_asynchronous_ocr_completion_maps_original_pdf_pages(monkeypatch):
    """Remote OCR operation result pages are relative to extracted OCR subset, not PDF page 1."""
    import sys,types
    store.init_db()
    jid=store.create_job('ocr-page-map')
    store.append_ids(jid,['X'])
    original=[{'page':200,'text':''},{'page':201,'text':''}]
    content={'evidence-key':original}
    class FakeCP:
        def __init__(self):pass
        def get(self,key):return content[key]
        def put(self,key,value):content[key]=value
    class FakeTX:
        def get_document_text_detection(self,**_):
            return {'JobStatus':'SUCCEEDED','Blocks':[
                    {'BlockType':'LINE','Page':1,'Text':'International Birth Date is 12 December 2008.'},
                    {'BlockType':'LINE','Page':2,'Text':'STELARA (Ustekinumab)'}]}
    fake_boto3=types.SimpleNamespace(client=lambda name:FakeTX())
    monkeypatch.setitem(sys.modules,'boto3',fake_boto3)
    monkeypatch.setattr(pipeline,'Checkpoints',FakeCP)
    with store.Session.begin() as s:
        s.add(enterprise_state.DocumentManifest(job_id=jid,document_id='X',source_version='version-1',expected_chunks=1,total_pages=201,state='PREPARED'))
        s.add(enterprise_state.PageChunk(job_id=jid,document_id='X',ordinal=0,start_page=200,end_page=201,
              source_version='version-1',checkpoint_key='evidence-key',ocr_page_map='[200,201]',
              ocr_state='WAITING',ocr_operation='textract-operation'))
    assert pipeline.ocr_poll({'job_id':jid,'document_id':'X','chunk_number':0})
    assert 'International Birth Date' in content['evidence-key'][0]['text']
    assert content['evidence-key'][0]['page']==200
    with store.Session() as s:
        assert s.get(enterprise_state.PageChunk,(jid,'X',0)).ocr_state=='DONE'
        assert s.query(enterprise_state.StageTask).filter_by(job_id=jid,stage='FINALIZE').count()==1


def test_evidence_ranking_is_product_independent():
    from backend.azure_llm import page_score,select_context
    pages=[
        {'page':1,'text':'Overview of safety report'},
        {'page':2,'text':'International Birth Date is 12 December 2008.'},
        {'page':3,'text':'Manufacturer: Sample Biopharma Inc. Product: SAMPLEMED'},
        {'page':4,'text':'Conclusions and overall safety assessment.'},
    ]
    assert page_score(pages[1])>page_score(pages[0])
    context=select_context(pages,12500)
    assert 'International Birth Date' in context
    assert 'SAMPLEMED' in context
    assert 'STELARA' not in context
