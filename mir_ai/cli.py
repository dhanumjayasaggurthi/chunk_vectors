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


def _configured_batch_source(settings, args, parser):
    """Resolve batch source with CLI overrides taking precedence over config."""
    explicit = [bool(args.file), bool(args.root), bool(args.s3)]
    if sum(explicit) > 1:
        parser.error("--file, --root, and --s3 are mutually exclusive")

    if args.file:
        return "file", args.file
    if args.root:
        return "nas", args.root
    if args.s3:
        return "s3", None

    if args.init_db:
        return "init_only", None

    if not settings.source_auto_run:
        parser.error(
            "no source argument supplied and source_auto_run=false; use --file/--root/--s3 "
            "or set [MIR_AI] source_auto_run=true"
        )

    if settings.source_type == "nas":
        return "nas", str(settings.docs_root)
    if settings.source_type == "s3":
        return "s3", None
    parser.error(f"unsupported configured source_type: {settings.source_type}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="MIR-AI bounded-memory ingestion pipeline"
    )
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
    configure_logging(str(settings.log_dir), settings.log_level)
    store = PostgresStore(
        settings,
        maxconn=max(8, settings.doc_workers * 3 + settings.page_workers + 4),
    )
    provider = None
    try:
        if args.init_db:
            store.init_schema()

        source_mode, source_value = _configured_batch_source(settings, args, parser)
        if source_mode == "init_only":
            return 0

        if source_mode in {"nas", "s3"}:
            rimdocs_jsonl = args.rimdocs_jsonl or settings.rimdocs_jsonl_path
            if not rimdocs_jsonl:
                parser.error(
                    "batch MIR-AI ingestion requires authoritative RimDocs metadata. "
                    "Set [MIR_AI] rimdocs_jsonl_path or pass --rimdocs-jsonl."
                )

            provider = JSONLRimDocsProvider(rimdocs_jsonl, settings.scratch_dir)
            runner = BatchRunner(settings, store, provider)
            max_files = (
                args.max_files
                if args.max_files is not None
                else (settings.source_max_files or None)
            )
            results = (
                runner.run_s3(S3Source(args.config), max_files)
                if source_mode == "s3"
                else runner.run_nas(source_value, max_files)
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
            source_value,
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
