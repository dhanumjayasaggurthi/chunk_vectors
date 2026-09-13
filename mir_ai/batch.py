from __future__ import annotations
from dataclasses import dataclass
from datetime import timezone
import configparser,os,tempfile
from pathlib import Path
from typing import Optional,Iterator
from .bounded import bounded_parallel_map
from .pipeline import MIRPipeline,PipelineResult
from .resources import ResourceGovernor
from .rimdocs import EmptyRimDocsProvider
@dataclass(frozen=True)
class SourceItem:canonical_path:str;local_path:Optional[str];source_url:str;source_version:str;size:int
def iter_nas(root,max_files=None)->Iterator[SourceItem]:
    count=0
    for dirpath,dirnames,filenames in os.walk(str(root)):
        dirnames.sort();filenames.sort()
        for name in filenames:
            if Path(name).suffix.lower() not in {".pdf",".docx"}:continue
            p=Path(dirpath)/name
            try:st=p.stat()
            except OSError:continue
            canonical=str(p.resolve()).replace("\\","/");yield SourceItem(canonical,str(p),canonical,f"mtime_ns={st.st_mtime_ns};size={st.st_size}",st.st_size);count+=1
            if max_files and count>=max_files:return
class S3Source:
    def __init__(self,config_path="config.ini"):
        cfg=configparser.ConfigParser();cfg.read(config_path);sec="S3" if cfg.has_section("S3") else "s3"
        if not cfg.has_section(sec):raise KeyError("Missing [S3] configuration")
        c=cfg[sec];self.bucket=c.get("bucket","");self.prefix=c.get("prefix","");self.region=c.get("region","us-east-1");self.profile=c.get("profile","");self.endpoint_url=c.get("endpoint_url","");self.temp_dir=c.get("temp_dir","") or None
        import boto3
        from botocore.config import Config as BotoConfig
        session=boto3.Session(profile_name=self.profile or None,region_name=self.region);kw={"config":BotoConfig(retries={"max_attempts":10,"mode":"adaptive"})}
        if self.endpoint_url:kw["endpoint_url"]=self.endpoint_url
        self.client=session.client("s3",**kw)
    def items(self,max_files=None):
        count=0
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,Prefix=self.prefix):
            for obj in page.get("Contents",[]):
                key=obj["Key"]
                if Path(key).suffix.lower() not in {".pdf",".docx"}:continue
                etag=str(obj.get("ETag","")).strip('"');modified=obj.get("LastModified");mod=modified.astimezone(timezone.utc).isoformat() if modified else "";uri=f"s3://{self.bucket}/{key}";yield SourceItem(uri,None,uri,f"etag={etag};last_modified={mod};size={obj.get('Size',0)}",int(obj.get("Size",0)));count+=1
                if max_files and count>=max_files:return
    def download(self,item):
        rest=item.canonical_path[len("s3://"):];bucket,_,key=rest.partition("/");fd,path=tempfile.mkstemp(suffix=Path(key).suffix,dir=self.temp_dir);os.close(fd)
        try:self.client.download_file(bucket,key,path);return path
        except Exception:
            try:os.unlink(path)
            except OSError:pass
            raise
class BatchRunner:
    def __init__(self,settings,store,rimdocs=None):self.settings=settings;self.store=store;self.rimdocs=rimdocs or EmptyRimDocsProvider();self.governor=ResourceGovernor(settings.vision_concurrency,settings.chat_concurrency,settings.embedding_concurrency)
    def _pipeline(self):return MIRPipeline(self.settings,store=self.store,governor=self.governor)
    def run_nas(self,root,max_files=None):
        def work(item):return self._pipeline().process_file(item.local_path,canonical_path=item.canonical_path,source_url=item.source_url,source_version=item.source_version,rimdocs_metadata=self.rimdocs.get(item.canonical_path))
        yield from bounded_parallel_map(work,iter_nas(root,max_files),self.settings.doc_workers,self.settings.max_inflight_docs)
    def run_s3(self,source,max_files=None):
        def work(item):
            if self.store.active_source_version(item.canonical_path)==item.source_version:return PipelineResult(self.store.doc_id(item.canonical_path),"",0,0,"SKIPPED_UNCHANGED")
            local=source.download(item)
            try:return self._pipeline().process_file(local,canonical_path=item.canonical_path,source_url=item.source_url,source_version=item.source_version,rimdocs_metadata=self.rimdocs.get(item.canonical_path))
            finally:
                try:os.unlink(local)
                except OSError:pass
        yield from bounded_parallel_map(work,source.items(max_files),self.settings.doc_workers,self.settings.max_inflight_docs)
