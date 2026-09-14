from types import SimpleNamespace
from mir_ai.batch import BatchRunner,SourceItem
from mir_ai.pipeline import PipelineResult

class FakeStore:
    def __init__(self):self.items={};self.finished=False
    @staticmethod
    def doc_id(v):return 'doc:'+v
    def ensure_observability_schema(self):pass
    def create_ingestion_run(self,run_id,*args):self.run_id=run_id
    def register_run_items(self,run_id,rows):
        for r in rows:self.items[r['logical_object_id']]=dict(r)
    def update_run_item(self,run_id,logical_object_id,**values):self.items.setdefault(logical_object_id,{}).update(values)
    def record_event(self,**kwargs):return True
    def finish_ingestion_run(self,run_id):self.finished=True;return {}

def item(name):
    return SourceItem(f's3://bucket/{name}.pdf',None,f's3://bucket/{name}.pdf','v1',1,name,'pdf','PDF_AVAILABLE',('pdf',))

def test_document_failure_does_not_abort_remaining_batch():
    r=BatchRunner.__new__(BatchRunner);r.store=FakeStore();r.profile='profile'
    r.settings=SimpleNamespace(batch_retry_transient_errors=False,batch_retry_attempts=1,batch_retry_base_seconds=0,
        batch_continue_on_error=True,doc_workers=2,max_inflight_docs=2,metadata_mode='disabled',object_match_case_sensitive=False)
    r._metadata_for_item=lambda x:(None,'disabled',None);r._skip_if_unchanged=lambda x,metadata_version='':None
    def process(x,metadata,metadata_version,run_id,worker_id):
        if x.logical_object_id=='b':raise ValueError('permanent bad document')
        return PipelineResult('doc-'+x.logical_object_id,'gen-'+x.logical_object_id,1,1,'ACTIVE')
    results=list(r._run_selected('s3','s3://bucket/',[item('a'),item('b'),item('c')],[],3,process))
    assert len(results)==3
    by={x.logical_object_id:x for x in results}
    assert by['a'].status=='ACTIVE' and by['b'].status=='ERROR' and by['c'].status=='ACTIVE'
    assert r.store.finished is True
