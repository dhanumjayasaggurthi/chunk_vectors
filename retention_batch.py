"""Bounded single-host deadline-ordered mixed-format capture pilot.

An upstream authoritative inventory supplies absolute source paths, byte hashes,
and UTC purge deadlines. No mtime-derived deadlines, deletion, original copies,
new database tables, OCR/LLM, or automatic fleet discovery. Local receipts are
audit evidence, not purge authorization or a distributed queue.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import multiprocessing
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LIMITS={"workers":(1,32),"max_documents":(1,10_000_000),
        "admission_margin_seconds":(0,86400),"max_inventory_line_bytes":(256,65536),
        "receipt_flush_every":(1,10000)}
FIELDS=set(LIMITS)|{"authorized_receipt_storage","authorized_docx_derivatives"}


def settings(value):
    if not isinstance(value,dict) or not FIELDS<=set(value) or set(value)-FIELDS-{"resource_guard"}:
        raise ValueError("Expected complete retention batch profile")
    for key,(low,high) in LIMITS.items():
        if type(value[key]) is not int or not low<=value[key]<=high:
            raise ValueError("Invalid retention batch "+key)
    for key in ("authorized_receipt_storage","authorized_docx_derivatives"):
        if type(value[key]) is not bool:
            raise ValueError(key+" must be boolean")
    result=dict(value)
    if result.get("resource_guard") is not None:
        from resource_guard import settings as guard_settings
        result["resource_guard"]=guard_settings(result["resource_guard"],result["workers"])
    return result


def _utc():
    return datetime.now(timezone.utc)


def _deadline(value):
    if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",value):
        raise ValueError("Explicit UTC purge deadline required (YYYY-MM-DDTHH:MM:SSZ)")
    return datetime.fromisoformat(value[:-1]+"+00:00")


def _pairs(pairs):
    out={}
    for k,v in pairs:
        if k in out:
            raise ValueError("Duplicate intake inventory key")
        out[k]=v
    return out


def iter_inventory(stream,cfg):
    """One bounded line at a time; require an upstream globally sorted shard.

    Unique source paths across different deadlines are an upstream contract;
    strict order detects duplicate (deadline,path) and out-of-order entries.
    """
    previous=None
    count=0
    while True:
        raw=stream.readline(cfg["max_inventory_line_bytes"]+1)
        if not raw:
            return
        count+=1
        if count>cfg["max_documents"] or len(raw)>cfg["max_inventory_line_bytes"]:
            raise ValueError("Intake inventory exceeds configured limits")
        row=json.loads(raw,object_pairs_hook=_pairs)
        if not isinstance(row,dict) or set(row)!={"path","source_sha256","purge_at_utc"}:
            raise ValueError("Invalid intake inventory record")
        path=row["path"]
        sha=row["source_sha256"]
        if (not isinstance(path,str) or not Path(path).is_absolute()
                or "\x00" in path or Path(path).suffix.lower() not in (".pdf",".docx")
                or not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}",sha)):
            raise ValueError("Expected absolute PDF/DOCX path and source SHA-256")
        deadline=_deadline(row["purge_at_utc"])
        key=(deadline,path)
        if previous is not None and key<=previous:
            raise ValueError("Intake shard must be strictly sorted by deadline and path")
        previous=key
        yield row


def _state(stream):
    s=os.fstat(stream.fileno())
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)


def _task_id(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,separators=(",",":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _expired(row,cfg):
    return (_deadline(row["purge_at_utc"])-_utc()).total_seconds()<=cfg["admission_margin_seconds"]


def _receipt(row,status,**fields):
    return {"task_id":_task_id(row),"source_sha256":row["source_sha256"],
            "format":Path(row["path"]).suffix.lower()[1:],
            "purge_at_utc":row["purge_at_utc"],"status":status,
            "finished_at_utc":_utc().isoformat(),"purge_safe":False,
            "durable_storage_verified":False,**fields}


def capture_one(row,profile,n,docx_config,cfg):
    """Worker owns a single document, with no nested pools or provider calls."""
    if _expired(row,cfg):
        return _receipt(row,"NOT_ADMITTED_DEADLINE")
    started=_utc()
    timer=time.monotonic()
    path=row["path"]
    try:
        if Path(path).suffix.lower()==".pdf":
            from page_stage import stage_one
            report=stage_one(path,profile,n,None,expected_source_sha256=row["source_sha256"])
            ev=report.get("retained_evidence")
            checked=bool(ev and ev.get("local_content_verified")
                         and ev.get("source_pdf_sha256")==row["source_sha256"]
                         and ev.get("verified_pages")==report.get("pages"))
            complete=checked and report["status"] in ("SUCCESS","SKIPPED")
            fields={"native_status":report["status"],"pages":report.get("pages"),
                    "capture_id":ev.get("capture_id") if ev else None,
                    "verified_pages":ev.get("verified_pages",0) if ev else 0,
                    "rendered_pages":ev.get("rendered_pages",0) if ev else 0,
                    "skipped_verified_pages":ev.get("skipped_verified_pages",0) if ev else 0,
                    "native_reconciliation_required":ev.get("native_reconciliation_required",False) if ev else False,
                    "local_content_verified":checked}
            status="LOCAL_CAPTURE_VERIFIED" if complete else "INCOMPLETE_OR_BUSY"
        else:
            from docx_evidence import capture
            ev_cfg=profile["extraction"]["retained_evidence"]
            report=capture(path,ev_cfg["root"],policy_id=ev_cfg["policy_id"],
                           authorized_derivatives=cfg["authorized_docx_derivatives"],
                           config=docx_config,expected_source_sha256=row["source_sha256"])
            checked=report["local_content_verified"] and report["source_docx_sha256"]==row["source_sha256"]
            fields={"capture_id":report["capture_id"],"local_content_verified":checked,
                    "skipped_verified":report["skipped_verified"],
                    "unresolved_entries":len(report["unresolved"]),"story_parts":report["story_parts"]}
            status="LOCAL_CAPTURE_VERIFIED" if checked else "INCOMPLETE_OR_BUSY"
        if _utc()>=_deadline(row["purge_at_utc"]):
            fields["capture_status"]=status
            status="DEADLINE_OVERRUN"
        return _receipt(row,status,started_at_utc=started.isoformat(),
                        elapsed_seconds=round(time.monotonic()-timer,3),**fields)
    except Exception as exc:
        # No raw paths, source text or provider responses in audit receipts.
        return _receipt(row,"FAILED",error_type=type(exc).__name__,
                        started_at_utc=started.isoformat(),
                        elapsed_seconds=round(time.monotonic()-timer,3))


def run_batch(inventory,receipt_path,profile,*,docx_config,config):
    """Execute one immutable sorted shard; bounded in-flight documents.

    The caller must supply disjoint shards with one owner per source path.
    No claim of global scheduling, distributed leases, or hard deadline kill.
    A new receipt path is required on replay; existing extractors verify and
    reuse their checkpoints. Receipt loss does not invalidate those artifacts.
    """
    cfg=settings(config)
    if not cfg["authorized_receipt_storage"]:
        raise PermissionError("Explicit receipt storage approval required")
    from page_stage import settings as native_settings,require_initialized
    from docx_evidence import settings as docx_settings
    native=native_settings(profile)
    docx_config=docx_settings(docx_config)
    if not native["retained_evidence"] or native["worker_backend"]!="processes":
        raise ValueError("Retention batch requires approved native evidence process profile")
    # No nested pools: native settings already require visual workers=1.
    n=require_initialized(profile)
    counters=Counter()
    guard=None
    if cfg.get("resource_guard") is not None:
        from resource_guard import ResourceGuard
        guard=ResourceGuard(cfg["resource_guard"],cfg["workers"])
    log=logging.getLogger(__name__)
    start=time.monotonic()
    with Path(inventory).open("rb") as stream:
        initial=_state(stream)
        total=0
        has_docx=False
        for item in iter_inventory(stream,cfg):
            total+=1
            has_docx|=Path(item["path"]).suffix.lower()==".docx"
        if has_docx and not cfg["authorized_docx_derivatives"]:
            raise PermissionError("Explicit DOCX derivative storage approval required")
        if not total or _state(stream)!=initial:
            raise ValueError("Empty or modified intake inventory")
        stream.seek(0)
        receipt=Path(receipt_path)
        if receipt.resolve()==Path(inventory).resolve():
            raise ValueError("Receipt must not overwrite intake inventory")
        # No implicit creation of new directories or overwrite of previous logs.
        with receipt.open("x",encoding="utf-8",newline="\n") as output:
            pending={}
            written=0
            max_pending=0

            def append(record):
                nonlocal written
                output.write(json.dumps(record,ensure_ascii=True,allow_nan=False,sort_keys=True)+"\n")
                written+=1
                counters[record["status"]]+=1
                if written%cfg["receipt_flush_every"]==0:
                    output.flush()
                    os.fsync(output.fileno())

            try:
                with concurrent.futures.ProcessPoolExecutor(
                        max_workers=cfg["workers"],mp_context=multiprocessing.get_context("spawn")) as pool:
                    def collect(timeout=None):
                        done,_=concurrent.futures.wait(pending,timeout=timeout,
                            return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in done:
                            row=pending.pop(future)
                            try:
                                result=future.result()
                            except Exception as exc:
                                result=_receipt(row,"FAILED",error_type=type(exc).__name__)
                            append(result)
                    resource_aborted=False
                    last_limit=None
                    for row in iter_inventory(stream,cfg):
                        if _state(stream)!=initial:
                            raise RuntimeError("Intake inventory changed after preflight")
                        if _expired(row,cfg):
                            append(_receipt(row,"NOT_ADMITTED_DEADLINE"))
                            continue
                        if resource_aborted:
                            append(_receipt(row,"NOT_ADMITTED_RESOURCE"))
                            continue
                        paused_since=None
                        rejected=False
                        while True:
                            limit=guard.poll() if guard is not None else cfg["workers"]
                            if limit!=last_limit:
                                log.warning("retention admission_limit=%s active=%s reason=%s",
                                    limit,len(pending),guard.last_reason if guard else "FIXED")
                                last_limit=limit
                            if _expired(row,cfg):
                                append(_receipt(row,"NOT_ADMITTED_DEADLINE"))
                                rejected=True
                                break
                            if limit>0 and len(pending)<limit:
                                break
                            if limit==0:
                                if paused_since is None:
                                    paused_since=time.monotonic()
                                if time.monotonic()-paused_since>=cfg["resource_guard"]["max_pause_seconds"]:
                                    resource_aborted=True
                                    append(_receipt(row,"NOT_ADMITTED_RESOURCE"))
                                    break
                            else:
                                paused_since=None
                            if pending:
                                collect(cfg["resource_guard"]["poll_seconds"] if guard else None)
                            else:
                                time.sleep(cfg["resource_guard"]["poll_seconds"])
                        if resource_aborted or rejected:
                            continue
                        future=pool.submit(capture_one,row,profile,n,docx_config,cfg)
                        pending[future]=row
                        max_pending=max(max_pending,len(pending))
                    while pending:
                        collect()
                if written!=total or _state(stream)!=initial:
                    raise RuntimeError("Intake receipt reconciliation failed")
            finally:
                output.flush()
                os.fsync(output.fileno())
    return {"inventory_documents":total,"receipts":written,"outcomes":dict(counters),
            "max_in_flight_observed":max_pending,"elapsed_seconds":round(time.monotonic()-start,3),
            **({"resource_guard":guard.report()} if guard else {}),
            "all_local_capture_verified":counters["LOCAL_CAPTURE_VERIFIED"]==total,
            "purge_safe":False,"durable_storage_verified":False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ("inventory","receipt","profile","docx-profile","batch-profile"):
        parser.add_argument("--"+arg,required=True)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args(argv)
    if not args.apply:
        parser.error("Explicit --apply is required for capture and receipt writes")
    from processing_profile import load_profile
    from docx_evidence import _load_profile
    with Path(args.batch_profile).open("rb") as stream:
        raw=stream.read(65537)
    if len(raw)>65536:
        raise ValueError("Retention batch profile exceeds limit")
    result=run_batch(args.inventory,args.receipt,load_profile(args.profile),
                     docx_config=_load_profile(args.docx_profile),config=json.loads(raw,object_pairs_hook=_pairs))
    print(json.dumps(result,sort_keys=True))
    return 0 if result["all_local_capture_verified"] else 1


if __name__=="__main__":
    raise SystemExit(main())
