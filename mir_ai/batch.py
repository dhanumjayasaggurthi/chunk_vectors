from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import configparser
import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Optional, Iterator

from .bounded import bounded_parallel_map
from .diagnostics import classify_exception, traceback_text
from .logging_utils import get_logger
from .object_selector import SelectionIssue, resolve_discovered_sources, resolve_preferred_sources
from .pipeline import MIRPipeline, PipelineResult
from .profile import processing_fingerprint
from .resources import ResourceGovernor
from .rimdocs import EmptyRimDocsProvider

log=get_logger("mir_ai.batch")
ERROR_STATUSES={"ERROR","SOURCE_NOT_FOUND","AMBIGUOUS_SOURCE","FORMAT_DISABLED","RIMDOCS_NOT_FOUND","PRECHECK_ERROR"}

@dataclass(frozen=True)
class SourceItem:
    canonical_path:str; local_path:Optional[str]; source_url:str; source_version:str; size:int
    logical_object_id:Optional[str]=None; selected_format:Optional[str]=None; selection_reason:str=""; available_formats:tuple[str,...]=()

@dataclass
class BatchResult:
    doc_id:str=""; generation_id:str=""; pages:int=0; chunks:int=0; status:str=""
    logical_object_id:str=""; canonical_path:str=""; source_url:str=""; selected_format:str=""
    selection_reason:str=""; available_formats:tuple[str,...]=(); detail:str=""; candidates:tuple[str,...]=()
    run_id:str=""; attempt:int=0; error_category:str=""; error_code:str=""; status_code:int|None=None
    request_id:str=""; retryable:bool=False; error_location:str=""; resolution_hint:str=""; root_cause_status:str=""


def _relative_logical_id(path:Path,root:Path)->str:
    return path.relative_to(root).with_suffix("").as_posix()

def iter_nas(root,max_files=None,recursive=True)->Iterator[SourceItem]:
    root=Path(root).resolve(); count=0
    for dirpath,dirnames,filenames in os.walk(str(root)):
        dirnames.sort(); filenames.sort()
        if not recursive: dirnames[:]=[]
        for name in filenames:
            if Path(name).suffix.lower() not in {".pdf",".docx"}: continue
            path=Path(dirpath)/name
            try: stat=path.stat()
            except OSError as exc:
                log.warning("NAS candidate stat failed",extra={"service":"nas","operation":"stat","error_class":type(exc).__name__,"error_detail":str(exc)[:1000]})
                continue
            canonical=str(path.resolve()).replace("\\","/")
            yield SourceItem(canonical,str(path),canonical,f"mtime_ns={stat.st_mtime_ns};size={stat.st_size}",stat.st_size,
                             logical_object_id=_relative_logical_id(path.resolve(),root))
            count+=1
            if max_files and count>=max_files:return


class S3Source:
    """S3 source retaining the existing server config pattern and boto3 credential chain."""
    def __init__(self,config_path="config.ini"):
        cfg=configparser.ConfigParser(); cfg.read(config_path)
        section="S3" if cfg.has_section("S3") else "s3"
        if not cfg.has_section(section): raise KeyError("Missing [S3] or [s3] configuration")
        c=cfg[section]; m=cfg["MIR_AI"] if cfg.has_section("MIR_AI") else {}
        self.bucket=c.get("bucket","").strip(); self.prefix=c.get("prefix","").strip(); self.region=c.get("region","us-east-1").strip() or "us-east-1"
        self.profile=c.get("profile","").strip(); self.endpoint_url=c.get("endpoint_url","").strip(); self.temp_dir=c.get("temp_dir","").strip() or None
        self.access_key_id=c.get("access_key_id","").strip(); self.secret_access_key=c.get("secret_access_key","").strip(); self.session_token=c.get("session_token","").strip()
        self.scratch_reserve_bytes=max(0,c.getint("scratch_reserve_mb",fallback=1024))*1024*1024
        self.max_attempts=max(1,int(c.get("max_attempts",m.get("s3_max_attempts",10))))
        self.connect_timeout_s=max(1,int(c.get("connect_timeout_s",m.get("s3_connect_timeout_s",20))))
        self.read_timeout_s=max(1,int(c.get("read_timeout_s",m.get("s3_read_timeout_s",120))))
        self._scratch_lock=threading.Lock(); self._reserved_bytes=0; self._reservations={}
        if not self.bucket: raise ValueError("S3 bucket is required")
        import boto3
        from botocore.config import Config as BotoConfig
        session_kwargs={"region_name":self.region}
        if self.access_key_id and self.secret_access_key:
            session_kwargs.update(aws_access_key_id=self.access_key_id,aws_secret_access_key=self.secret_access_key)
            if self.session_token: session_kwargs["aws_session_token"]=self.session_token
        elif self.profile: session_kwargs["profile_name"]=self.profile
        elif self.access_key_id or self.secret_access_key or self.session_token:
            raise ValueError("Incomplete static S3 credentials: provide both access_key_id and secret_access_key, configure profile, or use default AWS credential chain")
        session=boto3.Session(**session_kwargs)
        kwargs={"config":BotoConfig(retries={"max_attempts":self.max_attempts,"mode":"adaptive"},connect_timeout=self.connect_timeout_s,read_timeout=self.read_timeout_s,tcp_keepalive=True)}
        if self.endpoint_url: kwargs["endpoint_url"]=self.endpoint_url
        self.client=session.client("s3",**kwargs)

    def preflight(self):
        self.client.list_objects_v2(Bucket=self.bucket,Prefix=self.prefix,MaxKeys=1)
        return True
    def _relative_key(self,key):
        if not self.prefix:return key.lstrip("/")
        return key[len(self.prefix):].lstrip("/") if key.startswith(self.prefix) else key.lstrip("/")
    def items(self,max_files=None,recursive=True):
        count=0; paginator=self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket,Prefix=self.prefix):
            for obj in page.get("Contents",[]):
                key=obj["Key"]; suffix=Path(key).suffix.lower()
                if suffix not in {".pdf",".docx"}:continue
                relative=self._relative_key(key)
                if not recursive and "/" in relative:continue
                etag=str(obj.get("ETag","")).strip('"'); modified=obj.get("LastModified")
                modified_text=modified.astimezone(timezone.utc).isoformat() if modified else ""
                uri=f"s3://{self.bucket}/{key}"; size=int(obj.get("Size",0)); logical=str(PurePosixPath(relative).with_suffix(""))
                yield SourceItem(uri,None,uri,f"etag={etag};last_modified={modified_text};size={size}",size,logical_object_id=logical)
                count+=1
                if max_files and count>=max_files:return
    def _scratch_path(self):
        p=Path(self.temp_dir or tempfile.gettempdir()); p.mkdir(parents=True,exist_ok=True); return p
    def _reserve(self,size):
        scratch=self._scratch_path()
        with self._scratch_lock:
            free=shutil.disk_usage(scratch).free-self._reserved_bytes; required=size+self.scratch_reserve_bytes
            if free<required: raise RuntimeError(f"Insufficient S3 scratch space: need {required} bytes including reserve, available after reservations={max(0,free)} bytes")
            self._reserved_bytes+=size
    def download(self,item):
        self._reserve(item.size); rest=item.canonical_path[len("s3://"):]; bucket,_,key=rest.partition("/"); fd=None; path=None
        try:
            fd,path=tempfile.mkstemp(suffix=Path(key).suffix,dir=str(self._scratch_path())); os.close(fd); fd=None
            self.client.download_file(bucket,key,path); actual=os.path.getsize(path)
            if item.size and actual!=item.size: raise RuntimeError(f"S3 download size mismatch for {item.canonical_path}: expected {item.size}, got {actual}")
            with self._scratch_lock:self._reservations[path]=item.size
            return path
        except Exception:
            if fd is not None:
                try:os.close(fd)
                except OSError:pass
            if path:
                try:os.unlink(path)
                except OSError:pass
            with self._scratch_lock:self._reserved_bytes=max(0,self._reserved_bytes-item.size)
            raise
    def cleanup(self,path):
        try:os.unlink(path)
        except OSError:pass
        with self._scratch_lock:
            size=self._reservations.pop(path,0); self._reserved_bytes=max(0,self._reserved_bytes-size)


class BatchRunner:
    def __init__(self,settings,store,rimdocs=None):
        self.settings=settings; self.store=store; self.rimdocs=rimdocs or EmptyRimDocsProvider()
        self.governor=ResourceGovernor(settings.vision_concurrency,settings.chat_concurrency,settings.embedding_concurrency)
        self.profile=processing_fingerprint(settings)
    def _pipeline(self):return MIRPipeline(self.settings,store=self.store,governor=self.governor)
    @staticmethod
    def _safe_ident(value):
        value=str(value or "").strip()
        if not value or not value.replace("_","").isalnum() or value[0].isdigit():raise ValueError(f"Unsafe SQL identifier: {value!r}")
        return value
    def _load_object_ids(self,max_files=None):
        schema=self._safe_ident(self.settings.object_list_schema); table=self._safe_ident(self.settings.object_list_table); column=self._safe_ident(self.settings.object_list_id_column)
        sql=f"SELECT DISTINCT {column}::text FROM {schema}.{table} WHERE {column} IS NOT NULL AND btrim({column}::text)<>'' ORDER BY {column}::text"; params=[]
        if max_files:sql+=" LIMIT %s";params.append(int(max_files))
        rows=[]
        with self.store.conn() as conn:
            with conn.cursor(name="mirai_object_list_cursor") as cur:
                cur.itersize=1000;cur.execute(sql,params);rows.extend(str(r[0]).strip() for r in cur)
            conn.rollback()
        return rows
    def _resolve_items(self,items,max_files=None):
        if not self.settings.object_list_enabled:
            out=resolve_discovered_sources(items,enable_pdf=self.settings.enable_pdf,enable_docx=self.settings.enable_docx,
                                           preferred_format=self.settings.preferred_format,strict=self.settings.strict_source_selection,
                                           case_sensitive=self.settings.object_match_case_sensitive,max_items=max_files)
            return list(out.selected),list(out.issues)
        object_ids=self._load_object_ids(max_files)
        out=resolve_preferred_sources(items,object_ids,enable_pdf=self.settings.enable_pdf,enable_docx=self.settings.enable_docx,
                                      preferred_format=self.settings.preferred_format,strict=self.settings.strict_source_selection,
                                      case_sensitive=self.settings.object_match_case_sensitive)
        return list(out.selected),list(out.issues)
    def _document_key(self,item):
        if not item.logical_object_id:return item.canonical_path
        logical=item.logical_object_id.strip().replace("\\","/")
        if not self.settings.object_match_case_sensitive:logical=logical.casefold()
        return f"mirai-object:{logical}"
    def _metadata_for_item(self,item):
        if self.settings.metadata_mode=="disabled":return None,"disabled",None
        if hasattr(self.rimdocs,"get_with_version"):metadata,version=self.rimdocs.get_with_version(item.canonical_path)
        else:
            metadata=self.rimdocs.get(item.canonical_path)
            version="none" if metadata is None else hashlib.sha256(json.dumps(metadata,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        if self.settings.metadata_mode=="required" and metadata is None:
            oid=item.logical_object_id or item.canonical_path
            return None,version,SelectionIssue(oid,"RIMDOCS_NOT_FOUND","metadata_mode=required but no authoritative RimDocs row matched this source",(item.canonical_path,))
        return metadata,version,None
    def _effective_source_version(self,item,metadata_version=""):
        base=item.source_version
        if item.logical_object_id:base=f"{base};format={item.selected_format or ''};source={item.canonical_path}"
        return f"{base};metadata_mode={self.settings.metadata_mode};metadata_version={metadata_version or 'none'}"
    def _skip_if_unchanged(self,item,metadata_version=""):
        state=self.store.active_source_state(self._document_key(item))
        if state and state.get("source_version")==self._effective_source_version(item,metadata_version) and state.get("processing_fingerprint")==self.profile:
            return PipelineResult(self.store.doc_id(self._document_key(item)),state.get("generation_id") or "",int(state.get("pages_total") or 0),int(state.get("chunk_count") or 0),"SKIPPED_UNCHANGED")
        return None
    def _issue_result(self,issue,run_id=""):
        key=issue.object_id.strip().replace("\\","/")
        if not self.settings.object_match_case_sensitive:key=key.casefold()
        return BatchResult(doc_id=self.store.doc_id(f"mirai-object:{key}"),status=issue.status,logical_object_id=issue.object_id,
                           detail=issue.detail,candidates=issue.candidates,run_id=run_id)
    def _selected_result(self,item,run_id=""):
        return BatchResult(status="SELECTED",logical_object_id=item.logical_object_id or item.canonical_path,
                           canonical_path=item.canonical_path,source_url=item.source_url,selected_format=item.selected_format or "",
                           selection_reason=item.selection_reason,available_formats=item.available_formats,run_id=run_id)
    def plan_nas(self,root,max_files=None):
        sel,issues=self._resolve_items(iter_nas(root,recursive=self.settings.source_recursive),max_files)
        return [self._issue_result(i) for i in issues]+[self._selected_result(i) for i in sel]
    def plan_s3(self,source,max_files=None):
        sel,issues=self._resolve_items(source.items(recursive=self.settings.source_recursive),max_files)
        return [self._issue_result(i) for i in issues]+[self._selected_result(i) for i in sel]

    def _register_run(self,run_id,source_type,source_root,selected,issues,requested_limit):
        self.store.create_ingestion_run(run_id,source_type,source_root,requested_limit,self.profile,self.settings.metadata_mode,len(selected),len(issues))
        rows=[]
        for item in selected:
            rows.append({"logical_object_id":item.logical_object_id or item.canonical_path,"canonical_path":item.canonical_path,"source_url":item.source_url,
                         "selected_format":item.selected_format,"selection_reason":item.selection_reason,"status":"SELECTED","stage":"DISCOVERED"})
        for issue in issues:
            rows.append({"logical_object_id":issue.object_id,"canonical_path":issue.candidates[0] if len(issue.candidates)==1 else None,
                         "status":issue.status,"stage":"SOURCE_SELECTION","error_category":issue.status,
                         "error_message":issue.detail,"resolution_hint":"Review the source-selection issue and listed candidate paths; correct the source/control-list ambiguity or format policy before retrying."})
        self.store.register_run_items(run_id,rows)
        for issue in issues:
            self.store.record_event(
                run_id=run_id, logical_object_id=issue.object_id, severity="ERROR",
                event_type="SOURCE_SELECTION_ISSUE", stage="SOURCE_SELECTION",
                message=issue.detail, service="source_selector", operation="resolve_source",
                details={"status": issue.status, "candidates": list(issue.candidates)},
            )
        self.store.record_event(run_id=run_id,severity="INFO",event_type="RUN_STARTED",stage="SOURCE_SELECTION",
                                message="Ingestion run started",details={"source_type":source_type,"source_root":source_root,"requested_limit":requested_limit,"selected_count":len(selected),"issue_count":len(issues)})
        log.info("Ingestion run selected sources",extra={"run_id":run_id,"source_type":source_type,"requested_limit":requested_limit,"selected_count":len(selected),"issue_count":len(issues)})

    def _run_selected(self,source_type,source_root,selected,issues,requested_limit,process_item):
        run_id=f"run-{uuid.uuid4()}"; self._register_run(run_id,source_type,source_root,selected,issues,requested_limit)
        for issue in issues:yield self._issue_result(issue,run_id)

        def work(item):
            oid=item.logical_object_id or item.canonical_path; stable_worker=f"{run_id}:{hashlib.sha256(oid.encode()).hexdigest()[:16]}"
            metadata,metadata_version,metadata_issue=self._metadata_for_item(item)
            if metadata_issue:
                result=self._issue_result(metadata_issue,run_id); self.store.update_run_item(run_id,oid,status=result.status,stage="METADATA_PRECHECK",completed=True)
                return result
            skipped=self._skip_if_unchanged(item,metadata_version)
            if skipped:
                result=BatchResult(skipped.doc_id,skipped.generation_id,skipped.pages,skipped.chunks,skipped.status,oid,item.canonical_path,item.source_url,item.selected_format or "",item.selection_reason,item.available_formats,run_id=run_id)
                self.store.update_run_item(run_id,oid,doc_id=result.doc_id,generation_id=result.generation_id,status=result.status,stage="COMPLETE",pages=result.pages,chunks=result.chunks,completed=True)
                return result
            attempts=max(1,self.settings.batch_retry_attempts if self.settings.batch_retry_transient_errors else 1)
            self.store.update_run_item(run_id,oid,status="RUNNING",stage="STARTING",attempts=0,started=True)
            for attempt in range(1,attempts+1):
                try:
                    self.store.update_run_item(run_id,oid,status="RUNNING",attempts=attempt)
                    result=process_item(item,metadata,metadata_version,run_id,stable_worker)
                    out=BatchResult(result.doc_id,result.generation_id,result.pages,result.chunks,result.status,oid,item.canonical_path,item.source_url,
                                    item.selected_format or "",item.selection_reason,item.available_formats,run_id=run_id,attempt=attempt)
                    self.store.update_run_item(run_id,oid,doc_id=out.doc_id,generation_id=out.generation_id,status=out.status,stage="COMPLETE",attempts=attempt,pages=out.pages,chunks=out.chunks,completed=True)
                    return out
                except Exception as exc:
                    diag=classify_exception(exc); will_retry=bool(diag.retryable and attempt<attempts and self.settings.batch_retry_transient_errors)
                    self.store.record_event(run_id=run_id,logical_object_id=oid,severity="WARNING" if will_retry else "ERROR",
                                            event_type="DOCUMENT_RETRY" if will_retry else "DOCUMENT_FAILED",stage="BATCH",
                                            attempt=attempt,message=diag.observed_error,diagnostic=diag,
                                            details={"traceback":traceback_text(exc),"canonical_path":item.canonical_path})
                    log.warning("Document attempt failed; retry scheduled" if will_retry else "Document failed; batch will continue",
                                extra={"run_id":run_id,"logical_object_id":oid,"stage":"BATCH","attempt":attempt,"max_attempts":attempts,
                                       **{k:v for k,v in diag.to_dict().items() if v not in (None,"")}})
                    if will_retry:
                        time.sleep(min(30.0,self.settings.batch_retry_base_seconds*(2**(attempt-1)))+random.random());continue
                    self.store.update_run_item(run_id,oid,status="ERROR",stage="BATCH",attempts=attempt,error_class=diag.error_class,
                                               error_category=diag.error_category,error_code=diag.error_code,status_code=diag.status_code,
                                               request_id=diag.request_id,retryable=diag.retryable,error_location=diag.error_location,
                                               error_message=diag.observed_error,resolution_hint=diag.resolution_hint,root_cause_status=diag.root_cause_status,completed=True)
                    if not self.settings.batch_continue_on_error:raise
                    return BatchResult(status="ERROR",logical_object_id=oid,canonical_path=item.canonical_path,source_url=item.source_url,
                                       selected_format=item.selected_format or "",selection_reason=item.selection_reason,available_formats=item.available_formats,
                                       detail=diag.observed_error,run_id=run_id,attempt=attempt,error_category=diag.error_category,error_code=diag.error_code,
                                       status_code=diag.status_code,request_id=diag.request_id,retryable=diag.retryable,error_location=diag.error_location,
                                       resolution_hint=diag.resolution_hint,root_cause_status=diag.root_cause_status)

        try:
            yield from bounded_parallel_map(work,selected,self.settings.doc_workers,self.settings.max_inflight_docs)
        finally:
            try:
                counts=self.store.finish_ingestion_run(run_id)
                self.store.record_event(run_id=run_id,severity="INFO",event_type="RUN_COMPLETED",stage="COMPLETE",message="Ingestion run completed",details={"counts":counts})
                log.info("Ingestion run completed",extra={"run_id":run_id,"status":"COMPLETED","error_detail":json.dumps(counts,sort_keys=True)})
            except Exception:
                log.exception("Could not finalize ingestion run",extra={"run_id":run_id,"service":"postgres","operation":"finish_ingestion_run"})

    def run_nas(self,root,max_files=None):
        selected,issues=self._resolve_items(iter_nas(root,recursive=self.settings.source_recursive),max_files)
        def process(item,metadata,metadata_version,run_id,worker_id):
            return self._pipeline().process_file(item.local_path,canonical_path=self._document_key(item),source_url=item.source_url,
                                                 source_version=self._effective_source_version(item,metadata_version),rimdocs_metadata=metadata,
                                                 run_id=run_id,logical_object_id=item.logical_object_id or item.canonical_path,worker_id=worker_id)
        yield from self._run_selected("nas",str(root),selected,issues,max_files or 0,process)
    def run_s3(self,source,max_files=None):
        selected,issues=self._resolve_items(source.items(recursive=self.settings.source_recursive),max_files)
        def process(item,metadata,metadata_version,run_id,worker_id):
            local=source.download(item)
            try:
                return self._pipeline().process_file(local,canonical_path=self._document_key(item),source_url=item.source_url,
                                                     source_version=self._effective_source_version(item,metadata_version),rimdocs_metadata=metadata,
                                                     run_id=run_id,logical_object_id=item.logical_object_id or item.canonical_path,worker_id=worker_id)
            finally:source.cleanup(local)
        yield from self._run_selected("s3",f"s3://{source.bucket}/{source.prefix}",selected,issues,max_files or 0,process)
