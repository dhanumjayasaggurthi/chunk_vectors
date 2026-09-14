from __future__ import annotations

import argparse
import json

from .batch import BatchRunner, S3Source, ERROR_STATUSES
from .logging_utils import configure_logging, get_logger, shutdown_logging
from .pipeline import MIRPipeline
from .preflight import run_preflight
from .profile import validate_runtime
from .resilient_store import ResilientPostgresStore
from .rimdocs import JSONLRimDocsProvider
from .settings import Settings

log=get_logger("mir_ai.cli")


def _configured_batch_source(settings,args,parser):
    explicit=[bool(args.file),bool(args.root),bool(args.s3)]
    if sum(explicit)>1:parser.error("--file, --root, and --s3 are mutually exclusive")
    if args.file:return "file",args.file
    if args.root:return "nas",args.root
    if args.s3:return "s3",None
    if args.init_db or args.status or args.failed is not None or args.run_status:return "command",None
    if not settings.source_auto_run:
        parser.error("no source argument supplied and source_auto_run=false; use --file/--root/--s3 or set [MIR_AI] source_auto_run=true")
    if settings.source_type=="nas":return "nas",str(settings.docs_root)
    if settings.source_type=="s3":return "s3",None
    parser.error(f"unsupported configured source_type: {settings.source_type}")


def _batch_rimdocs_provider(settings,args,parser):
    if settings.metadata_mode=="disabled":return None
    path=args.rimdocs_jsonl or settings.rimdocs_jsonl_path
    if not path:
        if settings.metadata_mode=="required":
            parser.error("metadata_mode=required but no authoritative RimDocs JSONL is configured. Set [MIR_AI] rimdocs_jsonl_path, pass --rimdocs-jsonl, or change metadata_mode for an ingestion-only run.")
        return None
    return JSONLRimDocsProvider(path,settings.scratch_dir)


def _print(value):
    print(json.dumps(value,ensure_ascii=False,indent=2,default=str))


def main(argv=None):
    parser=argparse.ArgumentParser(description="MIR-AI bounded, resumable ingestion pipeline")
    parser.add_argument("--config",default="config.ini"); parser.add_argument("--file"); parser.add_argument("--root"); parser.add_argument("--s3",action="store_true")
    parser.add_argument("--max-files",type=int); parser.add_argument("--canonical-path"); parser.add_argument("--source-url",default="")
    parser.add_argument("--rimdocs-json"); parser.add_argument("--rimdocs-jsonl"); parser.add_argument("--init-db",action="store_true")
    parser.add_argument("--plan",action="store_true",help="Resolve and print exact physical source selections without ingestion")
    parser.add_argument("--preflight-only",action="store_true",help="Validate configured dependencies and exit")
    parser.add_argument("--status",action="store_true",help="Show current ingestion status summary")
    parser.add_argument("--failed",nargs="?",const=100,type=int,help="Show failed documents (optional limit, default 100)")
    parser.add_argument("--run-status",help="Show one ingestion run and its document items")
    args=parser.parse_args(argv)

    settings=Settings.load(args.config); validate_runtime(settings)
    configure_logging(settings.log_dir,settings.log_level,max_bytes=settings.log_max_bytes,backup_count=settings.log_backup_count,
                      queue_size=settings.log_queue_size,console=settings.log_console,console_json=settings.log_console_json)
    maxconn=settings.db_pool_maxconn or max(8,settings.doc_workers*3+settings.page_workers+4)
    store=ResilientPostgresStore(settings,maxconn=maxconn); provider=None
    try:
        if settings.auto_init_db and not args.init_db:
            store.init_schema()
        if settings.requirements_mode=="warn" and not settings.requirement_compliant_embedding_config:
            log.warning("requirements_mode=warn: embedding configuration is not MIR-AI requirements-compliant; do not treat this run as qualification evidence",
                        extra={"error_category":"REQUIREMENTS_NONCOMPLIANT"})
        if args.init_db:
            store.init_schema(); _print({"init_db":"ok","schema":settings.db_schema})
            if not (args.status or args.failed is not None or args.run_status):return 0
        if args.status:
            store.verify_schema();_print(store.get_pipeline_stats());return 0
        if args.failed is not None:
            store.verify_schema();_print({"failed":store.get_failed_docs(args.failed)});return 0
        if args.run_status:
            store.verify_schema();data=store.get_run_status(args.run_status);_print(data or {"error":"run_not_found","run_id":args.run_status});return 0 if data else 4

        source_mode,source_value=_configured_batch_source(settings,args,parser)
        if source_mode=="command":return 0
        provider=_batch_rimdocs_provider(settings,args,parser) if source_mode in {"nas","s3"} else None
        runner=BatchRunner(settings,store,provider)
        max_files=args.max_files if args.max_files is not None else (settings.source_max_files or None)
        s3_source=S3Source(args.config) if source_mode=="s3" else None

        plan_only=args.plan or settings.source_plan_only
        if plan_only:
            if source_mode=="file":
                _print({"mode":"file","selected":source_value,"canonical_path":args.canonical_path or source_value});return 0
            results=runner.plan_s3(s3_source,max_files) if source_mode=="s3" else runner.plan_nas(source_value,max_files)
            counts={}
            for r in results:counts[r.status]=counts.get(r.status,0)+1;print(json.dumps(r.__dict__,ensure_ascii=False,default=str))
            _print({"plan_summary":counts,"requested_limit":max_files or 0});return 0 if not any(k in ERROR_STATUSES for k in counts) else 2

        if settings.preflight_enabled or args.preflight_only:
            checks=run_preflight(settings,store,source_mode=source_mode,source_value=source_value,s3_source=s3_source)
            _print({"preflight":[c.to_dict() for c in checks]})
            if not all(c.ok for c in checks):return 3
            if args.preflight_only:return 0

        if source_mode in {"nas","s3"}:
            results=runner.run_s3(s3_source,max_files) if source_mode=="s3" else runner.run_nas(source_value,max_files)
            counts={}; last_run=""
            for result in results:
                counts[result.status]=counts.get(result.status,0)+1;last_run=getattr(result,"run_id","") or last_run
                print(json.dumps(result.__dict__,ensure_ascii=False,default=str))
            _print({"summary":counts,"run_id":last_run})
            errors=sum(v for k,v in counts.items() if k in ERROR_STATUSES)
            return 2 if errors and settings.batch_exit_nonzero_on_error else 0

        rimdocs=None
        if args.rimdocs_json:
            with open(args.rimdocs_json,"r",encoding="utf-8") as f:rimdocs=json.load(f)
            if not isinstance(rimdocs,dict):raise ValueError("--rimdocs-json must contain a JSON object")
        elif settings.metadata_mode=="required":
            parser.error("metadata_mode=required for single-file ingestion; pass --rimdocs-json or set metadata_mode=optional/disabled")
        result=MIRPipeline(settings,store=store).process_file(source_value,canonical_path=args.canonical_path,source_url=args.source_url,rimdocs_metadata=rimdocs)
        _print(result.__dict__);return 0
    finally:
        if provider is not None and hasattr(provider,"close"):provider.close()
        store.close();shutdown_logging()

if __name__=="__main__":raise SystemExit(main())
