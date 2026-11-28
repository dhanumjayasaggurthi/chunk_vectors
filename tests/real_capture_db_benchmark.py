"""Disposable CI: real full-PDF capture, evidence, DB and native chunk throughput.

No provider calls. Source-language chunks are NOT production English retrieval.
Never point this harness at a user database or existing evidence directory.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import configparser
from collections import Counter
import json
import logging
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from native_worker_scaling_benchmark import host_limits
from native_prepass_qualification import acquire_manifest
from online_public_regulatory_benchmark import candidates

# Same bytes as the first measured run, GitHub Actions 46047298590.
SOURCE_HASHES = {
    "CFR-2025-title7-vol13": "385c692ed98be5756b1aa7ef84b692803c278e6e237507b56d64d0f1b4221281",
    "CFR-2025-title40-vol17": "9aa74166eda7fc670dadcd7ec0e65963de98c374b8d435876931e3be4cc65ab0",
    "CFR-2025-title40-vol35": "77eb8fcd20ddc94ddf9286f5631039020fa66ad0dbbbba7df5eb553bb15c21ad",
    "CFR-2025-title7-vol8": "c32d4f4bf4c725355d991cd292703a99a8558ebb09e1127d34628d50d746f4c8",
    "CFR-2025-title7-vol11": "e9f1a10ceb58826bff7a308d0a967e59e36393400293bffd7046f0f5472e0730",
    "CFR-2025-title26-vol10": "9061144feadc5ba07986abbc4a6c43162758fd20dc886f3ab9b2dc2ac18189b5",
    "CFR-2025-title40-vol16": "bcfee8e1e09a1192218a21b8e52a5ef46f2376af257549b4f1b6a5221072ee74",
    "CFR-2025-title40-vol3": "d117e5af01c532bdbfe1b5ca181a5503dcf77309b2cce7005968c7a9498b8d71",
}


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
    batch.update(workers=workers,max_documents=len(SOURCE_HASHES))
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
            raise ValueError("Missing, duplicate or incomplete capture receipt")
        seen.add(sha);ids.add(row["doc_id"])
    if seen!=set(expected):
        raise ValueError("Capture did not cover the complete source manifest")
    return records


def evidence_timings(records, key):
    totals = Counter()
    for record in records:
        totals.update(record[key])
    return {phase: round(seconds, 6) for phase, seconds in totals.items()}


def reconcile_evidence(config, records, workers):
    from visual_evidence import verify

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        checks = [pool.submit(verify, config["root"], row["capture_id"], 1, row["pages"],
                              settings=config["visual_settings"], profile_timings=True) for row in records]
        reports = [check.result() for check in checks]
    for row, report in zip(records, reports):
        if (report["verified_image_pages"] != row["pages"]
                or report["source_pages"] != row["pages"]
                or report["image_bytes"] != row["evidence_image_bytes"]):
            raise ValueError("Independent verification disagrees with the capture receipt")
    return reports


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
                       expected_source_hash=record["source_sha256"],
                       source_batch_rows=d["limits"]["materialize_source_batch_rows"],
                       write_batch_rows=d["limits"]["materialize_write_batch_rows"])
        return {**result,"ocr_candidates_unprocessed":pending["candidates"],
                "ocr_discovery_seconds":round(discovery,3),
                "materialization_seconds":round(time.monotonic()-started,3)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(process,records))


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
        report["materialize_source_batch_rows"]=d["limits"]["materialize_source_batch_rows"]
        report["materialize_write_batch_rows"]=d["limits"]["materialize_write_batch_rows"]
        urls=dict(candidates())
        start=time.monotonic()
        items=acquire_manifest(root/"source",[{"name":name} for name in SOURCE_HASHES],urls)
        report["source_acquisition_seconds"]=round(time.monotonic()-start,3)
        report["sources"]=[{k:v for k,v in i.items() if k!="path"} for i in items]
        save()
        if any("path" not in i for i in items) or {i["name"]: i["sha256"] for i in items}!=SOURCE_HASHES:
            raise ValueError("Fixed corpus source bytes changed or could not be acquired")
        if sum(i["pages"] for i in items)!=6353:
            raise ValueError("Fixed corpus page count changed")
        (root/"inventory.jsonl").write_text("".join(json.dumps({"path":i["path"]})+"\n" for i in items),
                                            encoding="utf-8")
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
                      capture_evidence_worker_seconds=evidence_timings(records, "evidence_phase_timings_seconds"),
                      open_grid_counts=dict(grid),
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

        # These are separate workloads with separate wall clocks. The cold
        # capture's evidence time is already inside its native parse time.
        tick=time.monotonic()
        reconciled=reconcile_evidence(d["capture"]["extraction"]["retained_evidence"], records, args.workers)
        report["reconciliation_wall_seconds"]=round(time.monotonic()-tick,3)
        report["reconciled_evidence_pages"]=sum(row["verified_pages"] for row in reconciled)
        report["reconciliation_worker_seconds"]=evidence_timings(reconciled, "phase_timings_seconds")
        save()

        replay_receipt=out.with_suffix(out.suffix+".replay.jsonl")
        tick=time.monotonic()
        replay=run_batch(d["inventory"],replay_receipt,d["capture"],docx_config=d["docx"],config=d["batch"],
                         stream_receipts=True)
        report["replay_wall_seconds"]=round(time.monotonic()-tick,3)
        replayed=checked_receipts(replay_receipt,items,replay=True)
        captured = {r["doc_id"]: (r["source_sha256"], r["capture_id"], r["evidence_image_bytes"])
                    for r in records}
        for row in replayed:
            if captured.get(row["doc_id"]) != (row["source_sha256"], row["capture_id"], row["evidence_image_bytes"]):
                raise ValueError("Replay changed evidence identity")
        after_replay = snapshot(d,replayed,materialized=True)
        if after_replay["chunks"] != report["final_snapshot"]["chunks"]:
            raise ValueError("Replay changed the publication count")
        report["replay_evidence_worker_seconds"]=evidence_timings(replayed, "evidence_phase_timings_seconds")
        report["replay_verified_pages"]=sum(row["verified_pages"] for row in replayed)
        report["replay"]=replay
        tick=time.monotonic()
        materialization_replay=materialize(d,replayed,args.materialize_workers)
        report["materialization_replay_wall_seconds"]=round(time.monotonic()-tick,3)
        report["materialization_replay_results"]=materialization_replay
        if any(row["status"]!="SKIPPED" for row in materialization_replay):
            raise ValueError("Materialization replay was not idempotent")
        report["complete"]=True
        save()
        print("CAPTURE_DB_SUMMARY="+json.dumps({k:v for k,v in report.items()
              if k not in ("sources","materialization_results","capture")},sort_keys=True),flush=True)


if __name__=="__main__":
    main()
