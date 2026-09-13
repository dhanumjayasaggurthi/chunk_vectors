from __future__ import annotations
from dataclasses import dataclass
import hashlib,threading,uuid
from pathlib import Path
from .azure_gateway import AzureGateway
from .bounded import bounded_parallel_map,bounded_ordered_map,ContiguousProgress
from .docx_stream import DOCXPageStream,DOCXEnricher
from .enrich import PDFEnricher
from .logging_utils import get_logger
from .metadata import MetadataExtractor,overlap_with_rimdocs
from .models import ChunkRecord
from .pdf_stream import PDFPageStream
from .semantic import SemanticUnitStream,Chunker
from .settings import Settings
from .store import PostgresStore
from .vision import VisionOCR
from .resources import ResourceGovernor
log=get_logger("mir_ai.pipeline")
def sha256_file(path,block_size=1024*1024):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for block in iter(lambda:f.read(block_size),b""):h.update(block)
    return h.hexdigest()
@dataclass
class PipelineResult:doc_id:str;generation_id:str;pages:int;chunks:int;status:str
class LeaseHeartbeat:
    def __init__(self,store,g,w,lease,interval):self.store=store;self.g=g;self.w=w;self.lease=lease;self.interval=interval;self._stop=threading.Event();self._failed=threading.Event();self._thread=None
    def __enter__(self):
        def run():
            while not self._stop.wait(self.interval):
                try:
                    if not self.store.heartbeat(self.g,self.w,self.lease):self._failed.set();return
                except Exception:self._failed.set();return
        self._thread=threading.Thread(target=run,daemon=True);self._thread.start();return self
    def assert_owned(self):
        if self._failed.is_set():raise RuntimeError("Generation lease heartbeat failed or ownership was lost")
    def __exit__(self,*a):self._stop.set();self._thread.join(timeout=max(1,self.interval)) if self._thread else None
class MIRPipeline:
    def __init__(self,settings,store=None,gateway=None,vision_ocr=None,governor=None):
        settings.validate();self.settings=settings;self.store=store or PostgresStore(settings,maxconn=max(8,settings.page_workers+4));self.gateway=gateway or AzureGateway(settings.config_path);self.governor=governor or ResourceGovernor(settings.vision_concurrency,settings.chat_concurrency,settings.embedding_concurrency);base=vision_ocr or VisionOCR(settings.config_path).ocr
        def gv(data):
            with self.governor.slot(self.governor.vision):return base(data)
        self.vision_ocr=gv
        def gc(data,context=""):
            with self.governor.slot(self.governor.chat):return self.gateway.describe_chart(data,context)
        self.chart_describer=gc
    def process_file(self,file_path,*,canonical_path=None,source_url="",source_version=None,rimdocs_metadata=None,worker_id=None):
        path=Path(file_path)
        if not path.exists():raise FileNotFoundError(file_path)
        ext=path.suffix.lower()
        if ext not in {".pdf",".docx"}:raise ValueError(f"Unsupported document type: {ext}")
        canonical=canonical_path or str(path.resolve()).replace("\\","/");worker_id=worker_id or f"worker-{uuid.uuid4()}";fh=sha256_file(str(path));did=self.store.ensure_document(canonical,source_url,rimdocs_metadata or {});gid=self.store.ensure_generation(did,fh,source_version or fh)
        if not self.store.claim_generation(gid,worker_id,self.settings.lease_seconds):
            pr=self.store.generation_progress(gid)
            if pr["status"]=="ACTIVE":return PipelineResult(did,gid,pr.get("pages_total") or 0,0,"ACTIVE")
            raise RuntimeError(f"Generation is leased by another worker: {gid}")
        try:
            with LeaseHeartbeat(self.store,gid,worker_id,self.settings.lease_seconds,self.settings.heartbeat_seconds) as hb:
                pr=self.store.generation_progress(gid);resume=max(1,int(pr.get("last_page_completed") or 0)+1);pages_total=0
                if ext==".pdf":stream=PDFPageStream(str(path),did,self.settings.scanned_text_threshold);enr=PDFEnricher(str(path),did,self.settings.ocr_render_dpi,self.vision_ocr,self.chart_describer)
                else:stream=DOCXPageStream(str(path),did);enr=DOCXEnricher(str(path),self.vision_ocr)
                results=bounded_parallel_map(enr.enrich_page,(p for p in stream if p.page_number>=resume),self.settings.page_workers,self.settings.max_inflight_pages);cont=ContiguousProgress(resume-1)
                for page in results:
                    hb.assert_owned();self.store.upsert_page(gid,page);pages_total=max(pages_total,page.page_number);last=cont.mark(page.page_number)
                    if not self.store.heartbeat(gid,worker_id,self.settings.lease_seconds,stage="PAGE_EXTRACTION",last_page=last):raise RuntimeError("Lost generation ownership")
                pr=self.store.generation_progress(gid);pages_total=max(pages_total,int(pr.get("last_page_completed") or 0));hs,fs=self.store.repeated_header_footer_signatures(gid)
                def summarize(text,a,b):
                    if not text:return ""
                    with self.governor.slot(self.governor.chat):return self.gateway.chat_text([{"role":"system","content":"Summarize only the supplied extracted table. Do not infer missing values. Preserve key values, units, groups, and notable comparisons in concise factual prose."},{"role":"user","content":f"Pages {a}-{b}\n\n{text}"}],max_tokens=500,temperature=0)
                units=SemanticUnitStream(did,source_url,hs,fs,None).from_pages(self.store.iter_pages(gid))
                def su(u):
                    if u.unit_type=="table" and u.text:
                        sm=summarize(u.text,u.page_start,u.page_end)
                        if sm:u.text+=f"\n\n[TABLE SUMMARY]\n{sm}"
                    return u
                units=bounded_ordered_map(su,units,self.settings.table_summary_workers,max(self.settings.table_summary_workers,self.settings.table_summary_workers*2));chunks=Chunker(did,gid,self.settings.chunk_target_min_tokens,self.settings.chunk_target_max_tokens,self.settings.chunk_overlap_tokens).chunks(units);self.store.clear_generation_chunks(gid);batch=[];count=0
                for ch in chunks:
                    batch.append(ch)
                    if len(batch)>=self.settings.embedding_batch_size:self._embed_and_store(batch);count+=len(batch);batch=[]
                if batch:self._embed_and_store(batch);count+=len(batch)
                if count==0:raise RuntimeError("No chunks produced")
                if not self.store.heartbeat(gid,worker_id,self.settings.lease_seconds,stage="CHUNKING"):raise RuntimeError("Lost generation ownership")
                auth,missing=overlap_with_rimdocs(rimdocs_metadata or {});ex=MetadataExtractor(self.gateway).extract_missing_stream(missing,self.store.iter_metadata_candidate_pages(gid,20));self.store.upsert_metadata(gid,{**auth,**ex}.values())
                if not self.store.heartbeat(gid,worker_id,self.settings.lease_seconds,stage="METADATA"):raise RuntimeError("Lost generation ownership")
                self.store.activate_generation(gid,worker_id);return PipelineResult(did,gid,pages_total,count,"ACTIVE")
        except Exception as exc:
            try:self.store.mark_failed(gid,worker_id,str(exc))
            finally:log.exception("Document failed",extra={"doc_id":did,"generation_id":gid,"stage":"pipeline"})
            raise
    def _embed_and_store(self,batch):
        if self.settings.enable_embeddings:
            with self.governor.slot(self.governor.embedding):vecs=self.gateway.embeddings([c.text for c in batch])
            for c,v in zip(batch,vecs):c.embedding=v
        self.store.insert_chunk_batch(batch)
