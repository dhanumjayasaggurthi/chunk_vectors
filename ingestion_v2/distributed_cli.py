"""Opt-in NAS/local page-range worker backed by PostgreSQL leases.

This is a native-text *pilot runner*, not the production ingestion service.
No GCP requests, no source table writes, no vectorization, no auto-migrations.
Only process a source on different hosts if both hosts can read identical
bytes and can write/read the same validated shared artifact directory.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .extract import (
    _write_json_gzip_atomic, extract_range, fingerprint_pdf, job_identity,
    pdf_page_count,
)
from .ledger import Ledger
from .plan import ExtractionPolicy


def _hash_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def _source_id(uri):
    if not isinstance(uri,str) or not uri.strip() or "://" not in uri:
        raise ValueError("Provide a stable, qualified --source-uri (e.g. nas://archive/docs/A.pdf)")
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()


def register_document(ledger, source_path, source_uri, policy):
    fingerprint=fingerprint_pdf(source_path)
    count=pdf_page_count(source_path)
    if count<=0:
        raise ValueError("Empty PDF")
    source_id=_source_id(source_uri)
    job=job_identity(fingerprint["sha256"], policy, source_id)
    n=ledger.register(
        job_key=job,document_id=source_id,source_uri=source_uri,
        source_sha256=fingerprint["sha256"],source_size=fingerprint["size"],
        page_count=count,range_pages=policy.range_pages,policy=asdict(policy),
    )
    return {"job_key":job,"range_tasks":n,"pages":count,
            "source_sha256":fingerprint["sha256"]}


def _verify_source(ledger, job_key, pdf, source_uri):
    record=ledger.get_job(job_key)
    if record is None:
        raise ValueError("Job key is not registered")
    if record["source_uri"]!=source_uri:
        raise ValueError("Worker source URI differs from registered job")
    if record["document_id"]!=_source_id(source_uri):
        raise ValueError("Document identity mismatch")
    fingerprint=fingerprint_pdf(pdf)
    if record["source_size"]!=fingerprint["size"] or record["source_sha256"]!=fingerprint["sha256"]:
        raise ValueError("Source version differs from registered job; abort without claiming")
    if pdf_page_count(pdf)!=record["page_count"]:
        raise ValueError("Page count differs from registered job")
    policy=ExtractionPolicy(**record["policy"])
    if policy.range_pages!=record["range_pages"]:
        raise ValueError("Registered policy and range_pages mismatch")
    return record, fingerprint, policy


def _artifact_file(out, job, start, end, token):
    # Token-separated names are immutable: two workers can never overwrite
    # one another's staging file after a lease is reclaimed.
    name=f"pages-{start+1:07d}-{end:07d}-{token}.json.gz"
    return Path(out)/job/name


def _write_artifact(out, job_key, claim, pages, fingerprint, prefix):
    if [x["page"] for x in pages] != list(range(claim.start_page+1,claim.end_page+1)):
        raise ValueError("Range output has missing/duplicate page numbers")
    if not prefix.startswith(("nas://","smb://")):
        raise ValueError("Only explicitly mapped NAS/SMB artifact URI prefixes are supported")
    target=_artifact_file(out,job_key,claim.start_page,claim.end_page,claim.token)
    payload={
        "schema_version":1,"job_key":job_key,
        "source_sha256":fingerprint["sha256"],
        "range_start":claim.start_page,"range_end":claim.end_page,
        "claim_token":claim.token,"pages":pages,
    }
    _write_json_gzip_atomic(target,payload)
    digest=_hash_file(target)
    uri=prefix.rstrip("/")+"/"+job_key+"/"+target.name
    return uri,digest


def run_worker(*, ledger, job_key, source_path, source_uri, output_dir,
               artifact_uri_prefix, worker_id, max_claims=2):
    """Execute bounded native ranges while parent renews leases.

    max_claims=None is an explicit uncapped operator decision. The worker
    always stops on a database error rather than misreporting work complete.
    """
    if max_claims is not None and max_claims<1:
        raise ValueError("max_claims must be positive or None")
    if not artifact_uri_prefix.startswith(("nas://","smb://")):
        raise ValueError("Provide a shared NAS/SMB artifact URI prefix")
    if not worker_id or len(worker_id)>200:
        raise ValueError("worker_id must be 1..200 characters")

    started=time.perf_counter()
    record,source,policy=_verify_source(ledger,job_key,source_path,source_uri)
    summary={"job_key":job_key,"worker_id":worker_id,"SUCCESS":0,"FAILED":0,
             "RETRIED":0,"LOST_CLAIM":0,"claimed":0,"ocr_candidates":0,
             "pages_successful":0}
    # Spawned subprocesses open separate PDF handles. Parent retains the
    # database connection only for brief claim/heartbeat/commit transactions.
    with concurrent.futures.ProcessPoolExecutor(max_workers=1) as pool:
        while max_claims is None or summary["claimed"]<max_claims:
            ledger.expire_exhausted(job_key,policy.max_attempts)
            claim=ledger.claim(job_key,worker_id,policy.lease_seconds,
                               policy.max_attempts)
            if claim is None:
                break
            summary["claimed"]+=1
            try:
                task=pool.submit(
                    extract_range, str(source_path), claim.start_page,claim.end_page,
                    source["size"], source["mtime_ns"],policy.native_text_min_chars
                )
                still_owned=True
                heartbeat_seconds=max(5,policy.lease_seconds//4)
                while True:
                    try:
                        pages=task.result(timeout=heartbeat_seconds)
                        break
                    except concurrent.futures.TimeoutError:
                        # Do not block a long-running child while renewing.
                        if not ledger.renew(claim,policy.lease_seconds):
                            still_owned=False
                            # The child may still complete but its result is
                            # discarded. Fencing prevents stale publication.
                            pages=task.result()
                            break
                if not still_owned:
                    summary["LOST_CLAIM"]+=1
                    continue
                uri,digest=_write_artifact(
                    output_dir,job_key,claim,pages,source,artifact_uri_prefix
                )
                ocr=sum(bool(p.get("ocr_candidate")) for p in pages)
                if ledger.complete(claim,uri,digest,ocr):
                    summary["SUCCESS"]+=1
                    summary["pages_successful"]+=claim.end_page-claim.start_page
                    summary["ocr_candidates"]+=ocr
                else:
                    summary["LOST_CLAIM"]+=1
            except Exception as error:
                # One failed page range is recorded and isolated. If the
                # claim has been taken by another worker, fail() returns
                # None and does not overwrite that worker's status.
                code="NATIVE_RANGE_ERROR"
                state=ledger.fail(
                    claim,code,f"{type(error).__name__}: {error}",
                    max_attempts=policy.max_attempts,retry_delay_seconds=30
                )
                if state in ("FAILED","PENDING"):
                    summary["FAILED" if state=="FAILED" else "RETRIED"]+=1
                elif state is None:
                    summary["LOST_CLAIM"]+=1
    summary["wall_seconds"]=round(time.perf_counter()-started,3)
    summary["pages_per_second"]=round(
        summary["pages_successful"]/max(0.001,summary["wall_seconds"]),3
    )
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest="command",required=True)
    init=commands.add_parser("init-ledger")
    init.add_argument("--apply",action="store_true",
                      help="Required to create isolated ingestion_v2_runtime schema")
    register=commands.add_parser("register")
    register.add_argument("--pdf",required=True)
    register.add_argument("--source-uri",required=True)
    register.add_argument("--profile",required=True)
    register.add_argument("--confirm",action="store_true",help="Explicit DB write opt-in")
    work=commands.add_parser("worker")
    work.add_argument("--job-key",required=True)
    work.add_argument("--pdf",required=True)
    work.add_argument("--source-uri",required=True)
    work.add_argument("--output",required=True)
    work.add_argument("--artifact-uri-prefix",required=True)
    work.add_argument("--worker-id",default=None)
    work.add_argument("--max-claims",type=int,default=2,
                      help="Pilot processes at most two ranges by default")
    work.add_argument("--unlimited",action="store_true",
                      help="Explicitly process all available ranges")
    status=commands.add_parser("status")
    status.add_argument("--job-key",required=True)
    args=p.parse_args(argv)

    if args.command=="init-ledger" and not args.apply:
        p.error("No database changes made. Specify --apply after DBA review.")
    if args.command=="register" and not args.confirm:
        p.error("No database changes made. Specify --confirm for registration.")

    ledger=Ledger()
    if args.command=="init-ledger":
        ledger.create_schema()
        result={"created_schema":"ingestion_v2_runtime",
                "existing_production_tables_modified":False}
    elif args.command=="register":
        from .profile import load_profile,require_approved_for_pilot
        config=load_profile(args.profile)
        require_approved_for_pilot(config)
        policy=ExtractionPolicy(**config["extraction"])
        result=register_document(ledger,args.pdf,args.source_uri,policy)
    elif args.command=="status":
        result={"job_key":args.job_key,"source":ledger.get_job(args.job_key),
                "ranges":ledger.summary(args.job_key)}
    else:
        worker=args.worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        result=run_worker(
            ledger=ledger,job_key=args.job_key,source_path=args.pdf,
            source_uri=args.source_uri,output_dir=args.output,
            artifact_uri_prefix=args.artifact_uri_prefix,worker_id=worker,
            max_claims=None if args.unlimited else args.max_claims,
        )
    print(json.dumps(result,default=str,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
