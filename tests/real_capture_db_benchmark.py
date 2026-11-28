"""Disposable CI: real full-PDF capture, evidence, DB and native chunk throughput.

No provider calls. Source-language chunks are NOT production English retrieval.
Never point this harness at a user database or existing evidence directory.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import hashlib
from collections import Counter
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from native_worker_scaling_benchmark import FIXTURES,host_limits
from native_prepass_qualification import acquire_manifest
from online_public_regulatory_benchmark import candidates


# Exact original bytes and page counts from successful run 46047298590.
# A changed upstream file is a new corpus, not a comparable repeat.
QUALIFIED_SOURCES = {
    "CFR-2025-title7-vol13": (281, "385c692ed98be5756b1aa7ef84b692803c278e6e237507b56d64d0f1b4221281"),
    "CFR-2025-title40-vol17": (382, "9aa74166eda7fc670dadcd7ec0e65963de98c374b8d435876931e3be4cc65ab0"),
    "CFR-2025-title40-vol35": (533, "77eb8fcd20ddc94ddf9286f5631039020fa66ad0dbbbba7df5eb553bb15c21ad"),
    "CFR-2025-title7-vol8": (686, "c32d4f4bf4c725355d991cd292703a99a8558ebb09e1127d34628d50d746f4c8"),
    "CFR-2025-title7-vol11": (827, "e9f1a10ceb58826bff7a308d0a967e59e36393400293bffd7046f0f5472e0730"),
    "CFR-2025-title26-vol10": (974, "9061144feadc5ba07986abbc4a6c43162758fd20dc886f3ab9b2dc2ac18189b5"),
    "CFR-2025-title40-vol16": (1149, "bcfee8e1e09a1192218a21b8e52a5ef46f2376af257549b4f1b6a5221072ee74"),
    "CFR-2025-title40-vol3": (1521, "d117e5af01c532bdbfe1b5ca181a5503dcf77309b2cce7005968c7a9498b8d71"),
}


def write_benchmark_inventory(path, items, batch_config):
    """Use the existing path-only contract for CI's non-purging fixtures.

    Acquisition and both sets of receipts bind to the exact source hashes.
    Never invent a regulatory purge deadline or relax the production parser.
    """
    from retention_batch import iter_inventory
    actual = {i["name"]: (i.get("pages"), i.get("sha256")) for i in items}
    if (len(items) != len(QUALIFIED_SOURCES) or actual != QUALIFIED_SOURCES
            or any("path" not in i for i in items)
            or len({i["path"] for i in items}) != len(items)):
        raise ValueError("Fixed corpus source identity changed or incomplete")
    rows = [{"path": i["path"]} for i in sorted(items, key=lambda i: i["path"])]
    path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
    # Cheap contract check before any DB setup or expensive capture.
    with path.open("rb") as stream:
        if list(iter_inventory(stream, batch_config)) != rows:
            raise ValueError("Benchmark inventory reconciliation failed")


def verify_ci_database():
    if (os.environ.get("GITHUB_ACTIONS")!="true"
            or os.environ.get("INGESTION_REUSE_TEST_DB")!="1"
            or os.environ.get("PGDATABASE")!="embedding_test"):
        raise PermissionError("Only disposable GitHub CI PostgreSQL is supported")
    from config import POSTGRES
    if (POSTGRES["host"]!="localhost" or POSTGRES["port"]!=5432
            or POSTGRES["database"]!="embedding_test" or POSTGRES["user"]!="postgres"):
        raise PermissionError("Refusing non-CI database configuration")
    from db import get_conn
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(),current_user")
            if cur.fetchone()!=("embedding_test","postgres"):
                raise PermissionError("Unexpected connected database or role")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
    finally:
        conn.close()


def deployment(root,workers):
    import fitz
    from production import resolve_configuration
    from processing_profile import load_profile
    c=configparser.ConfigParser()
    c.read(REPO/"config.ini.example")
    schema="cv_real_"+uuid.uuid4().hex[:12]
    c["POSTGRES"].update(host="localhost",port="5432",database="embedding_test",user="postgres",
                        password="test",schema=schema,native_table="pages",chunk_table="native_chunks",
                        pool_enabled="true",pool_max_connections="5")
    dep=c["DEPLOYMENT"]
    dep.update(policy=str(REPO/"profiles/deployment_policy.example.yml"),
               docx_profile=str(REPO/"profiles/docx_evidence_example.json"),
               source_root=str(root/"source"),inventory=str(root/"inventory.jsonl"),
               receipt_dir=str(root/"receipts"),native_table_prepass="true",
               native_open_grid_body="true",overlap_enrichment="false",
               authorized_ocr="false",authorized_translation="false",
               authorized_visual_llm="false",authorized_embedding="false")
    batch=json.loads((REPO/"profiles/retention_batch_example.json").read_text())
    batch.update(workers=workers,max_documents=len(FIXTURES))
    batch_path=root/"batch.json"
    batch_path.write_text(json.dumps(batch),encoding="utf-8")
    dep["batch_profile"]=str(batch_path)
    c["RETENTION"].update(root=str(root/"evidence"),policy_id="public-ci-only",
                         renderer=fitz.VersionBind,authorized_page_derivatives="true",
                         authorized_receipt_storage="true",
                         visual_profile=str(REPO/"profiles/visual_evidence_native.example.json"))
    c["PIPELINE"]["enable_embeddings"]="false"
    path=root/"deployment.ini"
    with path.open("w") as f:
        c.write(f)
    d=resolve_configuration(path)
    # Deliberate separate source-language test profile, never the production
    # English contract or a dummy translator. Recompute normal profile identity.
    policy={k:v for k,v in d["retrieval"].items() if k!="_fingerprint"}
    policy["output"]["text_contract"]="legacy"
    policy["chunking"]["physical_table_body_mode"]="lines"
    policy_path=root/"native-retrieval.json"
    policy_path.write_text(json.dumps(policy),encoding="utf-8")
    d["retrieval"]=load_profile(str(policy_path))
    return d,schema


def checked_receipts(path,items,*,replay=False):
    records=[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected={i["sha256"]:i["pages"] for i in items}
    seen=set()
    ids=set()
    for row in records:
        sha=row.get("source_sha256")
        if (sha not in expected or sha in seen or row.get("doc_id") in ids
                or not row.get("doc_id")
                or row.get("status")!="LOCAL_CAPTURE_VERIFIED"
                or row.get("native_status")!=("SKIPPED" if replay else "SUCCESS")
                or row.get("pages")!=expected[sha]
                or row.get("verified_pages")!=expected[sha]
                or row.get("rendered_pages")!=(0 if replay else expected[sha])
                or row.get("skipped_verified_pages")!=(expected[sha] if replay else 0)
                or row.get("local_content_verified") is not True
                or row.get("purge_safe") is not False
                or row.get("durable_storage_verified") is not False):
            raise ValueError("Missing, duplicate or incomplete fresh capture")
        seen.add(sha);ids.add(row["doc_id"])
    if seen!=set(expected):
        raise ValueError("Capture did not cover the complete source manifest")
    return records


def evidence_metrics(records, *, replay=False):
    """Reject absent, overlapping or incomplete profiling, including on replay."""
    totals=Counter()
    reconciled=0.0
    for row in records:
        phases=row.get("evidence_timings_seconds")
        required={"other_capture"}
        prefix="replay_verify" if replay else "fresh_verify"
        required.update(prefix+"_"+s for s in ("manifest_read","image_checksum",
                        "image_decode","raster_inventory","vector_inventory","figure_inventory"))
        if not replay:
            required.update(("render","png_encode","png_write","manifest_write","image_checksum",
                             "raster_capture_including_asset_writes","vector_inventory","figure_inventory"))
        if (not isinstance(phases,dict) or not required.issubset(phases)
                or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in phases.values())
                or (replay and set(phases)!=required)
                or abs(sum(phases.values())-row["evidence_elapsed_seconds"])>0.002):
            raise ValueError("Missing or inconsistent retention timing breakdown")
        value=row["evidence_reconciliation_seconds"]
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError("Invalid evidence reconciliation timing")
        reconciled+=value
        totals.update(phases)
    return {"capture_worker_seconds":dict(totals),"report_reconciliation_worker_seconds":reconciled}


def snapshot(d,records,*,materialized=False):
    from page_stage import names,extraction_fingerprint
    from reuse_materialize import identifiers
    from db import get_conn
    n=names(d["capture"]);q=identifiers(d["retrieval"])
    expected={r["doc_id"]:(r["source_sha256"],r["pages"]) for r in records}
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT doc_id,source_hash,page_count,extraction_hash FROM {n['docs']}")
            actual={doc:(sha,count) for doc,sha,count,fp in cur.fetchall()
                    if fp==extraction_fingerprint(d["capture"])}
            if actual!=expected:
                raise ValueError("Native manifest mismatch")
            cur.execute(f"SELECT doc_id,count(*),min(page_number),max(page_number),"
                        f"count(distinct source_hash),min(source_hash),count(distinct extraction_hash),"
                        f"min(extraction_hash) FROM {n['published']} GROUP BY doc_id")
            seen=set()
            for doc,count,lo,hi,versions,sha,profiles,fp in cur.fetchall():
                if (doc not in expected or expected[doc]!=(sha,count) or lo!=1 or hi!=count
                        or versions!=1 or profiles!=1 or fp!=extraction_fingerprint(d["capture"])):
                    raise ValueError("Native published page coverage or identity mismatch")
                seen.add(doc)
            if seen!=set(expected):
                raise ValueError("Missing native published document")
            cur.execute(f"SELECT count(*) FROM {n['errors']}")
            errors=cur.fetchone()[0]
            if errors:
                raise ValueError("Native error events present")
            cur.execute(f"SELECT pg_total_relation_size(%s::regclass)",(n["published"],))
            report={"native_documents":len(seen),"native_pages":sum(x[1] for x in expected.values()),
                    "published_page_relation_bytes":cur.fetchone()[0],"native_errors":errors}
            if materialized:
                cur.execute(f"SELECT doc_id,profile_hash,chunk_count FROM {q['status']}")
                declared={doc:count for doc,fp,count in cur.fetchall()
                          if fp==d["retrieval"]["_fingerprint"]}
                cur.execute(f"SELECT doc_id,count(*),count(DISTINCT chunk_index),"
                            f"min(chunk_index),max(chunk_index),count(chunk_vector) "
                            f"FROM {q['table']} GROUP BY doc_id")
                observed={}
                for doc,count,unique,lo,hi,vectors in cur.fetchall():
                    if not count or unique!=count or lo!=0 or hi!=count-1 or vectors:
                        raise ValueError("Invalid native-only chunk coverage")
                    observed[doc]=count
                if observed!=declared or set(observed)!=set(expected):
                    raise ValueError("Chunk publication manifest mismatch")
                cur.execute(f"SELECT count(*) FROM {q['failures']}")
                if cur.fetchone()[0]:
                    raise ValueError("Materialization failure events present")
                cur.execute(f"SELECT pg_total_relation_size(%s::regclass)",(q["table"],))
                report.update(materialized_documents=len(observed),chunks=sum(observed.values()),
                              chunk_relation_bytes=cur.fetchone()[0],embedded_chunks=0)
            cur.execute("SELECT pg_database_size(current_database())")
            report["database_bytes"]=cur.fetchone()[0]
            return report
    finally:
        conn.rollback()
        conn.close()


def materialize(d,records,workers):
    from reuse_materialize import run_one,verify_output
    from deferred_ocr import enqueue
    names=verify_output(d["retrieval"])
    def process(record):
        # Discovery is read-only; no OCR jobs or provider request are created.
        started=time.monotonic()
        pending=enqueue(d["capture"],doc_id=record["doc_id"],max_docs=1,apply=False)
        discovery=time.monotonic()-started
        started=time.monotonic()
        result=run_one(record["doc_id"],d["retrieval"],names,
                       expected_source_hash=record["source_sha256"])
        return {**result,"ocr_candidates_unprocessed":pending["candidates"],
                "ocr_discovery_seconds":round(discovery,3),
                "materialization_seconds":round(time.monotonic()-started,3)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(process,records))


def verify_relocated_projection(d, records):
    """Compare all actual published text/boundaries to an alternate path identity.

    Reproject existing native source rows in memory one document at a time.
    No native re-extraction, duplicate publication, vector writes or provider
    calls. Document-specific IDs and provenance metadata deliberately differ.
    """
    from db import get_conn
    from extracted_source import load_extracted_document
    from reuse_materialize import prepare_document, identifiers
    profile=d["retrieval"]
    names=identifiers(profile)
    def add(digest, row):
        digest.update(json.dumps(list(row),sort_keys=True,ensure_ascii=False,
                                 separators=(",",":"),allow_nan=False).encode("utf-8"))
        digest.update(b"\n")
    results=[]
    for record in records:
        source=record["source_sha256"]
        doc=record["doc_id"]
        expected=hashlib.sha256()
        count=0
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT chunk_index,source_chunk_text,chunk_text,page_start,page_end,"
                            f"section_title,headers,content_types FROM {names['table']} "
                            "WHERE doc_id=%s ORDER BY chunk_index",(doc,))
                while True:
                    batch=cur.fetchmany(256)
                    if not batch:
                        break
                    for row in batch:
                        if row[0]!=count:
                            raise ValueError("Published chunk sequence mismatch")
                        add(expected,row)
                        count+=1
        finally:
            conn.rollback()
            conn.close()
        struct,_=load_extracted_document(profile,doc)
        struct.file_path="/alternate-intake/"+struct.file_name
        struct.doc_id=hashlib.sha256(("relocated:"+doc).encode()).hexdigest()
        rows=prepare_document(struct,profile)
        actual=hashlib.sha256()
        for row in rows:
            add(actual,(row[3],row[6],row[7],row[8],row[9],row[10],
                        json.loads(row[11]),json.loads(row[14])))
        if not count or count!=len(rows) or expected.digest()!=actual.digest():
            raise ValueError("Relocated projection differs for source "+source)
        results.append({"source_sha256":source,"chunks":count,
                        "text_boundary_embedding_input_sha256":actual.hexdigest()})
        del rows,struct
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",required=True)
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--materialize-workers",type=int,default=2)
    args=parser.parse_args()
    if not 1<=args.workers<=4 or not 1<=args.materialize_workers<=2:
        parser.error("CI capture workers 1..4, materialization workers 1..2")
    verify_ci_database()
    from page_stage import init
    from reuse_materialize import init_output
    from retention_batch import run_batch
    from native_table_prepass import qualified_runtime
    if not qualified_runtime():
        raise RuntimeError("Expected qualified table detector dependency pair")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    out=Path(args.report).resolve()
    report={"host":host_limits(),"complete":False,"scope":"native_capture_retention_db_and_native_only_chunks",
            "provider_calls":False,"english_retrieval_complete":False,"purge_safe":False,
            "semantic_accuracy_verified":False,"capture_workers":args.workers,
            "materialize_workers":args.materialize_workers}
    def save():
        temp=out.with_suffix(out.suffix+".tmp")
        temp.write_text(json.dumps(report,indent=2),encoding="utf-8")
        temp.replace(out)
    save()
    with tempfile.TemporaryDirectory(prefix="cv-real-capture-") as tmp:
        root=Path(tmp)
        for folder in ("source","receipts","evidence"):
            (root/folder).mkdir()
        report["initial_free_bytes"]=shutil.disk_usage(root).free
        if report["initial_free_bytes"]<10*1024**3:
            save()
            raise RuntimeError("Insufficient free disk for full 300-DPI evidence pilot")
        d,schema=deployment(root,args.workers)
        report["schema"]=schema
        urls=dict(candidates())
        start=time.monotonic()
        items=acquire_manifest(root/"source",[{"name":x} for x in FIXTURES],urls)
        report["source_acquisition_seconds"]=round(time.monotonic()-start,3)
        report["sources"]=[{k:v for k,v in i.items() if k!="path"} for i in items]
        save()
        write_benchmark_inventory(root/"inventory.jsonl",items,d["batch"])
        init(d["capture"],apply=True)
        init_output(d["retrieval"],apply=True)
        receipt=out.with_suffix(out.suffix+".receipts.jsonl")
        tick=time.monotonic()
        capture=run_batch(d["inventory"],receipt,d["capture"],docx_config=d["docx"],config=d["batch"],
                          stream_receipts=True)
        elapsed=time.monotonic()-tick
        report["capture"]=capture
        report["capture_wall_seconds"]=round(elapsed,3)
        save()
        records=checked_receipts(receipt,items)
        totals=Counter();prepass=Counter();grid=Counter()
        for rec in records:
            totals.update(rec.get("stage_timings_seconds",{}))
            prepass.update(rec.get("native_table_prepass_counts",{}))
            grid.update(rec.get("native_open_grid_counts",{}))
        pages=sum(rec["pages"] for rec in records)
        report.update(capture_pages_per_second=round(pages/elapsed,3),
                      capture_documents_per_minute=round(60*len(records)/elapsed,3),
                      stage_worker_seconds=dict(totals),native_prepass_counts=dict(prepass),
                      open_grid_counts=dict(grid),
                      evidence_profile=evidence_metrics(records),
                      evidence_file_bytes=sum(f.stat().st_size for f in (root/"evidence").rglob("*") if f.is_file()),
                      native_snapshot=snapshot(d,records))
        save()
        if not prepass["NEGATIVE_PREPASS"]:
            raise RuntimeError("Fast path did not activate")
        tick=time.monotonic()
        results=materialize(d,records,args.materialize_workers)
        report["materialization_and_discovery_wall_seconds"]=round(time.monotonic()-tick,3)
        report["materialization_results"]=results
        report["ocr_candidates_unprocessed"]=sum(x["ocr_candidates_unprocessed"] for x in results)
        save()
        if len(results)!=len(records) or any(x["status"]!="SUCCESS" for x in results):
            raise RuntimeError("Native-only materialization failed")
        report["final_snapshot"]=snapshot(d,records,materialized=True)
        save()
        tick=time.monotonic()
        report["relocated_projection"]=verify_relocated_projection(d,records)
        report["relocated_projection_wall_seconds"]=round(time.monotonic()-tick,3)
        save()
        # Separate warm replay on the same immutable source/evidence and native
        # destination; never count this duration as first-pass throughput.
        replay_receipt=out.with_suffix(out.suffix+".replay.jsonl")
        tick=time.monotonic()
        report["replay"]=run_batch(d["inventory"],replay_receipt,d["capture"],
                                   docx_config=d["docx"],config=d["batch"],stream_receipts=True)
        report["replay_wall_seconds"]=round(time.monotonic()-tick,3)
        save()
        replayed=checked_receipts(replay_receipt,items,replay=True)
        def identities(rows):
            return {r["source_sha256"]:(r["doc_id"],r["capture_id"],r["evidence_image_bytes"])
                    for r in rows}
        if identities(replayed)!=identities(records):
            raise ValueError("Replay evidence identity changed")
        if sum(f.stat().st_size for f in (root/"evidence").rglob("*") if f.is_file())!=report["evidence_file_bytes"]:
            raise ValueError("Replay evidence footprint changed")
        replay_snapshot=snapshot(d,replayed,materialized=True)
        for key in ("native_documents","native_pages","native_errors","materialized_documents",
                    "chunks","embedded_chunks"):
            if replay_snapshot[key]!=report["final_snapshot"][key]:
                raise ValueError("Replay changed publication coverage")
        report["replay_profile"]=evidence_metrics(replayed,replay=True)
        report["replay_verified_pages"]=sum(r["skipped_verified_pages"] for r in replayed)
        report["complete"]=True
        save()
        print("CAPTURE_DB_SUMMARY="+json.dumps({k:v for k,v in report.items()
              if k not in ("sources","materialization_results","capture")},sort_keys=True),flush=True)


if __name__=="__main__":
    main()
