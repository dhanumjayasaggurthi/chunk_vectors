"""Disposable CI qualification for one public 4,496-page PDF materialization.

This is a native-only source-fidelity and resource test. It makes no provider
calls and does not claim logical tariff-row accuracy or production throughput.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))

SOURCE_SHA256="e52e377f0234c60d5c98947129040a0706958438377868abddeff876cbf56068"
DOCUMENT_ID=hashlib.sha256(("public-usitc-finalcopy-v1\x00"+SOURCE_SHA256).encode()).hexdigest()


def peak_process_rss_kib():
    """Return Linux peak RSS for the current executable image."""
    # getrusage(RUSAGE_SELF).ru_maxrss can retain the pre-exec RSS of the
    # staging process across subprocess launch. VmHWM starts fresh at exec.
    with Path("/proc/self/status").open(encoding="ascii") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                _, value, unit = line.split()
                if unit == "kB":
                    return int(value)
    raise RuntimeError("Missing Linux VmHWM RSS measurement")


class _DigestWriter:
    def __init__(self):
        self.hash=hashlib.sha256()
        self.bytes=0
    def write(self,value):
        data=value.encode("utf-8") if isinstance(value,str) else value
        self.hash.update(data)
        self.bytes+=len(data)


def verify_ci_database():
    if (os.environ.get("GITHUB_ACTIONS")!="true"
            or os.environ.get("INGESTION_REUSE_TEST_DB")!="1"
            or os.environ.get("PGDATABASE")!="embedding_test"):
        raise PermissionError("Only disposable GitHub CI PostgreSQL is supported")
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(),current_user")
            if cur.fetchone()!=("embedding_test","postgres"):
                raise PermissionError("Unexpected PostgreSQL target")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
    finally:
        conn.close()


def build_profiles(root):
    from processing_profile import load_profile
    schema="cv_giant_"+uuid.uuid4().hex[:12]
    common=[
        'extraction.schema="'+schema+'"','extraction.table="pages"',
        'extracted_content.schema="'+schema+'"','extracted_content.table="pages"',
        'output.schema="'+schema+'"','output.table="native_chunks"',
        'existing_chunks.schema="'+schema+'"','existing_chunks.table="native_chunks"',
        'output.text_contract="legacy"','embedding.dimensions=1536',
        'extraction.native_table_prepass=true','extraction.native_open_grid_body=true',
    ]
    capture=load_profile(str(REPO/"profiles/deployment_policy.example.yml"),common)
    raw=copy.deepcopy({key:value for key,value in capture.items() if key!="_fingerprint"})
    raw["chunking"]["physical_table_body_mode"]="lines"
    retrieval_path=root/"retrieval.json"
    retrieval_path.write_text(json.dumps(raw),encoding="utf-8")
    retrieval=load_profile(str(retrieval_path))
    return schema,capture,retrieval,retrieval_path


def output_snapshot(profile,doc_id):
    from db import get_conn
    from reuse_materialize import identifiers
    names=identifiers(profile)
    writer=_DigestWriter()
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            query=cur.mogrify(f"""COPY (
                SELECT chunk_id,doc_id,profile_hash,chunk_index,file_path,file_name,
                       source_chunk_text,chunk_text,page_start,page_end,section_title,
                       headers::text,document_metadata::text,chunk_metadata::text,
                       content_types::text,metadata_evidence::text
                FROM {names['table']} WHERE doc_id=%s ORDER BY chunk_index
            ) TO STDOUT WITH (FORMAT csv)""",(doc_id,)).decode("utf-8")
            cur.copy_expert(query,writer)
            cur.execute(f"""SELECT count(*),count(*) FILTER (
                    WHERE chunk_metadata ? 'physical_table_body'),count(chunk_vector)
                    FROM {names['table']} WHERE doc_id=%s""",(doc_id,))
            rows,physical,vectors=cur.fetchone()
            cur.execute(f"SELECT source_hash,chunk_count FROM {names['status']} WHERE doc_id=%s",
                        (doc_id,))
            status=cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return {"rows":rows,"physical_body_rows":physical,"vectors":vectors,
            "copy_bytes":writer.bytes,"sha256":writer.hash.hexdigest(),"status":status}


def child(profile_path,mode,source_batch_rows,write_batch_rows):
    from processing_profile import load_profile
    from reuse_materialize import run_one,verify_output
    profile=load_profile(str(profile_path))
    names=verify_output(profile)
    started=time.monotonic()
    result=run_one(
        DOCUMENT_ID,profile,names,expected_source_hash=SOURCE_SHA256,
        source_batch_rows=source_batch_rows,write_batch_rows=write_batch_rows,
    )
    report={"mode":mode,"result":result,"wall_seconds":round(time.monotonic()-started,3),
            "peak_python_rss_kib":peak_process_rss_kib(),
            "source_batch_rows":source_batch_rows,"write_batch_rows":write_batch_rows,
            "output":output_snapshot(profile,DOCUMENT_ID)}
    expected="SUCCESS" if mode=="cold" else "SKIPPED"
    if result["status"]!=expected:
        raise RuntimeError("Unexpected materialization outcome: "+repr(result))
    print("GIANT_MATERIALIZATION_CHILD="+json.dumps(report,sort_keys=True),flush=True)


def run_child(profile_path,mode,source_batch_rows,write_batch_rows):
    proc=subprocess.run([
        sys.executable,str(Path(__file__).resolve()),"--child",mode,
        "--profile",str(profile_path),"--source-batch-rows",str(source_batch_rows),
        "--write-batch-rows",str(write_batch_rows),
    ],cwd=Path.cwd(),text=True,capture_output=True,timeout=600)
    if proc.returncode:
        raise RuntimeError("Materialization child failed\n"+proc.stdout+"\n"+proc.stderr)
    marker="GIANT_MATERIALIZATION_CHILD="
    lines=[line for line in proc.stdout.splitlines() if line.startswith(marker)]
    if len(lines)!=1:
        raise RuntimeError("Missing materialization child report\n"+proc.stdout)
    return json.loads(lines[0][len(marker):])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf",type=Path)
    parser.add_argument("--report",type=Path)
    parser.add_argument("--source-batch-rows",type=int,default=128)
    parser.add_argument("--write-batch-rows",type=int,default=200)
    parser.add_argument("--child",choices=("cold","replay"))
    parser.add_argument("--profile",type=Path)
    args=parser.parse_args()
    if args.child:
        if not args.profile:
            parser.error("--profile is required for child mode")
        child(args.profile,args.child,args.source_batch_rows,args.write_batch_rows)
        return
    if not args.pdf or not args.report:
        parser.error("--pdf and --report are required")
    if not 1<=args.source_batch_rows<=4096 or not 1<=args.write_batch_rows<=4096:
        parser.error("Batch rows must be 1..4096")
    verify_ci_database()
    from page_stage import init,names as native_names,stage_one
    from reuse_materialize import init_output
    from db import get_conn
    source=args.pdf.resolve()
    source_hash=hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            source_hash.update(block)
    if source_hash.hexdigest()!=SOURCE_SHA256:
        raise ValueError("Pinned public PDF hash mismatch")
    with tempfile.TemporaryDirectory(prefix="cv-giant-materialization-") as temp:
        root=Path(temp)
        schema,capture,retrieval,profile_path=build_profiles(root)
        init(capture,apply=True)
        init_output(retrieval,apply=True)
        started=time.monotonic()
        staged=stage_one(
            source,capture,native_names(capture),doc_id=DOCUMENT_ID,
            expected_source_sha256=SOURCE_SHA256,
        )
        native_seconds=time.monotonic()-started
        if staged["status"]!="SUCCESS" or staged["pages"]!=4496:
            raise RuntimeError("Giant native stage incomplete: "+repr(staged))
        native_rss=peak_process_rss_kib()
        cold=run_child(profile_path,"cold",args.source_batch_rows,args.write_batch_rows)
        replay=run_child(profile_path,"replay",args.source_batch_rows,args.write_batch_rows)
        native=native_names(capture)
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {native['published']} WHERE doc_id=%s",
                            (DOCUMENT_ID,))
                pages=cur.fetchone()[0]
                cur.execute(f"SELECT count(*) FROM {native['errors']} WHERE doc_id=%s",
                            (DOCUMENT_ID,))
                errors=cur.fetchone()[0]
                cur.execute(f"""SELECT count(*) FROM {native['published']}
                    WHERE doc_id=%s AND EXISTS (
                        SELECT 1 FROM jsonb_array_elements(elements) element
                        WHERE element ? 'physical_table_body')""",(DOCUMENT_ID,))
                physical_body_pages=cur.fetchone()[0]
            conn.rollback()
        finally:
            conn.close()
        status=cold["output"]["status"]
        checks={
            "publication_replay_equal":(
                cold["output"]==replay["output"]
                and bool(cold["output"]["rows"])
                and bool(cold["output"]["physical_body_rows"])
                and cold["output"]["vectors"]==0
                and bool(status)
                and status[1]==cold["output"]["rows"]
            ),
            "cold_rss_at_most_800000_kib":cold["peak_python_rss_kib"]<=800_000,
            "replay_rss_at_most_200000_kib":replay["peak_python_rss_kib"]<=200_000,
            "native_source_coverage":(
                pages==4496 and errors==0 and physical_body_pages==2084
            ),
        }
        report={"complete":all(checks.values()),
                "scope":"public_native_stage_and_native_only_materialization",
                "source_sha256":SOURCE_SHA256,"document_id":DOCUMENT_ID,"schema":schema,
                "pages":pages,"native_errors":errors,"physical_body_pages":physical_body_pages,
                "native_stage_seconds":round(native_seconds,3),
                "native_stage_pages_per_minute":round(4496*60/native_seconds,1),
                "rss_measurement":"linux_proc_self_status_vmhwm",
                "native_stage_peak_python_rss_kib":native_rss,
                "provider_calls":False,"retained_evidence":False,"vectors":False,
                "logical_tariff_rows_verified":False,"acceptance":checks,
                "cold":cold,"replay":replay}
        # Preserve measurements for failed gates as well as successful runs.
        # CI uploads this report under ``if: always()`` for diagnosis.
        args.report.write_text(json.dumps(report,indent=2),encoding="utf-8")
        failed=[name for name,passed in checks.items() if not passed]
        if failed:
            raise RuntimeError("Giant qualification failed: "+", ".join(failed))
        print("GIANT_MATERIALIZATION_SUMMARY="+json.dumps(report,sort_keys=True),flush=True)


if __name__=="__main__":
    main()
