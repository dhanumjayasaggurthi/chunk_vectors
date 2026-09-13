from __future__ import annotations
import argparse,json
from .logging_utils import configure_logging
from .pipeline import MIRPipeline
from .settings import Settings
from .store import PostgresStore
from .batch import BatchRunner,S3Source
from .rimdocs import JSONLRimDocsProvider,EmptyRimDocsProvider
def main(argv=None):
    ap=argparse.ArgumentParser(description="MIR-AI bounded-memory ingestion pipeline");ap.add_argument("--config",default="config.ini");ap.add_argument("--file");ap.add_argument("--root");ap.add_argument("--s3",action="store_true");ap.add_argument("--max-files",type=int);ap.add_argument("--canonical-path");ap.add_argument("--source-url",default="");ap.add_argument("--rimdocs-json");ap.add_argument("--rimdocs-jsonl");ap.add_argument("--init-db",action="store_true");args=ap.parse_args(argv);settings=Settings.load(args.config);settings.validate();configure_logging("logs",settings.log_level);store=PostgresStore(settings,maxconn=max(8,settings.doc_workers*3+settings.page_workers+4))
    try:
        if args.init_db:
            store.init_schema()
            if not args.file and not args.root and not args.s3:return 0
        if not args.file and not args.root and not args.s3:ap.error("one of --file, --root, or --s3 is required unless only --init-db is requested")
        if sum(bool(x) for x in (args.file,args.root,args.s3))>1:ap.error("--file, --root, and --s3 are mutually exclusive")
        if args.root or args.s3:
            provider=JSONLRimDocsProvider(args.rimdocs_jsonl,settings.scratch_dir) if args.rimdocs_jsonl else EmptyRimDocsProvider();runner=BatchRunner(settings,store,provider);results=runner.run_s3(S3Source(args.config),args.max_files) if args.s3 else runner.run_nas(args.root,args.max_files);counts={}
            for result in results:counts[result.status]=counts.get(result.status,0)+1;print(json.dumps(result.__dict__,ensure_ascii=False))
            print(json.dumps({"summary":counts},indent=2));return 0
        rimdocs={}
        if args.rimdocs_json:
            with open(args.rimdocs_json,"r",encoding="utf-8") as f:rimdocs=json.load(f)
        result=MIRPipeline(settings,store=store).process_file(args.file,canonical_path=args.canonical_path,source_url=args.source_url,rimdocs_metadata=rimdocs);print(json.dumps(result.__dict__,indent=2));return 0
    finally:store.close()
if __name__=="__main__":raise SystemExit(main())
