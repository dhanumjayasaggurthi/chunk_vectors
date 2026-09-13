from __future__ import annotations

import argparse
import json

from .batch import BatchRunner, S3Source
from .logging_utils import configure_logging
from .pipeline import MIRPipeline
from .profile import validate_runtime
from .rimdocs import JSONLRimDocsProvider
from .settings import Settings
from .store import PostgresStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="MIR-AI bounded-memory ingestion pipeline")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--file")
    parser.add_argument("--root")
    parser.add_argument("--s3", action="store_true")
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--canonical-path")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--rimdocs-json")
    parser.add_argument("--rimdocs-jsonl")
    parser.add_argument("--init-db", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.load(args.config)
    validate_runtime(settings)
    configure_logging("logs", settings.log_level)
    store = PostgresStore(
        settings,
        maxconn=max(8, settings.doc_workers * 3 + settings.page_workers + 4),
    )
    provider = None
    try:
        if args.init_db:
            store.init_schema()
            if not args.file and not args.root and not args.s3:
                return 0

        if not args.file and not args.root and not args.s3:
            parser.error(
                "one of --file, --root, or --s3 is required unless only --init-db is requested"
            )
        if sum(bool(x) for x in (args.file, args.root, args.s3)) > 1:
            parser.error("--file, --root, and --s3 are mutually exclusive")

        if args.root or args.s3:
            # The source requirements define RimDocs as authoritative and permit
            # LLM extraction only after overlap analysis. Batch ingestion therefore
            # fails closed if an authoritative handover is not supplied; silently
            # treating every one of the 50 requested fields as missing would violate
            # that rule. The provider boundary can be replaced by the approved live
            # RimDocs interface once Business/Data Hub supplies it.
            if not args.rimdocs_jsonl:
                parser.error(
                    "batch MIR-AI ingestion requires --rimdocs-jsonl so authoritative "
                    "RimDocs overlap analysis can be performed"
                )
            provider = JSONLRimDocsProvider(args.rimdocs_jsonl, settings.scratch_dir)
            runner = BatchRunner(settings, store, provider)
            results = (
                runner.run_s3(S3Source(args.config), args.max_files)
                if args.s3
                else runner.run_nas(args.root, args.max_files)
            )
            counts = {}
            for result in results:
                counts[result.status] = counts.get(result.status, 0) + 1
                print(json.dumps(result.__dict__, ensure_ascii=False))
            print(json.dumps({"summary": counts}, indent=2))
            return 0

        rimdocs = None
        if args.rimdocs_json:
            with open(args.rimdocs_json, "r", encoding="utf-8") as f:
                rimdocs = json.load(f)
            if not isinstance(rimdocs, dict):
                raise ValueError("--rimdocs-json must contain a JSON object")

        result = MIRPipeline(settings, store=store).process_file(
            args.file,
            canonical_path=args.canonical_path,
            source_url=args.source_url,
            rimdocs_metadata=rimdocs,
        )
        print(json.dumps(result.__dict__, indent=2))
        return 0
    finally:
        if provider is not None and hasattr(provider, "close"):
            provider.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
