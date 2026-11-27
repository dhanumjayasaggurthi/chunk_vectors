"""One config.ini per deployment. No implicit DDL or cloud calls."""
from __future__ import annotations

import argparse
import configparser
import concurrent.futures
import contextlib
import copy
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime,timezone
from pathlib import Path

IDENT=re.compile(r"^[a-z_][a-z0-9_]*$")
ROOT=Path(__file__).resolve().parent


def _ident(value,limit):
    if not isinstance(value,str) or not IDENT.fullmatch(value) or len(value)>limit:
        raise ValueError("Use lowercase safe PostgreSQL identifiers within the configured length limits")
    return value


def _json_profile(path):
    with Path(path).open("rb") as f:
        data=f.read(131073)
    if len(data)>131072:
        raise ValueError("JSON profile exceeds 128 KiB")
    return json.loads(data)


def _integer(section,key,low,high):
    label="["+section.name+"] "+key
    if key not in section or not section.get(key,"").strip():
        raise ValueError("Missing required configuration setting "+label)
    try:
        value=section.getint(key)
    except ValueError:
        raise ValueError("Configuration setting "+label+" must be an integer") from None
    if not low<=value<=high:
        raise ValueError("Configuration setting "+label+f" must be in {low}..{high}")
    return value


def _absolute(section,key):
    path=Path(section[key])
    if not path.is_absolute():
        raise ValueError(key+" must be absolute")
    return path


def resolve_configuration(path="config.ini"):
    """Resolve the canonical INI into existing validated profiles. No DB or RPC."""
    from processing_profile import load_profile
    from visual_evidence import load_visual_profile
    from docx_evidence import settings as docx_settings
    from retention_batch import settings as batch_settings
    cfg=configparser.ConfigParser()
    if not cfg.read(path,encoding="utf-8"):
        raise ValueError("Missing local config.ini; copy/merge config.ini.example")
    db=cfg["POSTGRES"]
    dep=cfg["DEPLOYMENT"]
    ret=cfg["RETENTION"]
    for key in ("host","database","user","password"):
        if not db.get(key,"").strip() or db[key].startswith("REPLACE_WITH_"):
            raise ValueError("Configure [POSTGRES] "+key)
    schema=_ident(db["schema"],45)
    target_schema=_ident(db.get("chunk_schema",schema),45)
    native=_ident(db["native_table"],45)
    chunk=_ident(db["chunk_table"],48)
    relations=[(schema,native+s) for s in ("","_work","_docs","_errors","_visual_ocr")]
    relations.extend((target_schema,chunk+s) for s in ("","_status","_failures"))
    if len(set(relations))!=len(relations):
        raise ValueError("Native, OCR and retrieval table names/companions must not collide")
    dims=_integer(cfg["AZURE_OPENAI_EMBEDDING"],"dimensions",1,16000)
    evidence=_absolute(ret,"root")
    source=_absolute(dep,"source_root") if dep.get("source_root","").strip() else _absolute(cfg["PATHS"],"docs_root")
    receipts=_absolute(dep,"receipt_dir")
    inventory=_absolute(dep,"inventory") if dep.get("inventory","").strip() else None
    visual=load_visual_profile(ret["visual_profile"])
    if visual["selection_mode"]!="all_pages":
        raise ValueError("Production capture requires all_pages until selective completeness is validated")
    retained={"root":str(evidence),"policy_id":ret["policy_id"],"renderer":ret["renderer"],
              "authorized_page_derivatives":ret.getboolean("authorized_page_derivatives"),
              "visual_settings":visual}
    read_workers=_integer(dep,"ocr_read_workers",1,32)
    ocr_retained={k:v for k,v in retained.items() if k!="authorized_page_derivatives"}
    ocr_retained["read_workers"]=read_workers
    shared={"extraction.schema":schema,"extraction.table":native,
            "extracted_content.schema":schema,"extracted_content.table":native,
            "output.schema":target_schema,"output.table":chunk,"output.text_contract":"english_v1",
            "existing_chunks.schema":target_schema,"existing_chunks.table":chunk,
            "existing_chunks.id_column":"chunk_id","existing_chunks.text_column":"chunk_text",
            "existing_chunks.vector_column":"chunk_vector",
            "existing_chunks.note_column":"embedding_processing_note",
            "embedding.dimensions":dims}
    def load(extra):
        return load_profile(dep["policy"],[k+"="+json.dumps(v) for k,v in {**shared,**extra}.items()])
    # The materialization identity excludes host-specific capture/reader paths.
    # Both profiles retain the common configured semantic policy.
    retrieval=load({})
    if retrieval["extraction"].get("retained_evidence") is not None or retrieval.get("deferred_ocr",{}).get("retained_evidence") is not None:
        raise ValueError("Configure evidence paths only in [RETENTION], not processing policy")
    capture=load({"extraction.retained_evidence":retained,
                  "deferred_ocr.render_dpi":visual["dpi"],
                  "deferred_ocr.retained_evidence":ocr_retained})
    batch=_json_profile(dep["batch_profile"])
    batch["authorized_receipt_storage"]=ret.getboolean("authorized_receipt_storage")
    batch["authorized_docx_derivatives"]=ret.getboolean("authorized_docx_derivatives")
    batch=batch_settings(batch)
    docx=docx_settings(_json_profile(dep["docx_profile"]))
    limits={k:_integer(dep,k,lo,hi) for k,lo,hi in (
        ("preflight_sample_files",1,100),("preflight_min_free_gib",1,100000),
        ("preflight_statement_timeout_ms",100,120000),("materialize_workers",1,32),
        ("materialize_max_docs",1,10000000),("ocr_max_docs",1,100000),
        ("ocr_max_jobs",1,1000000),("embedding_workers",1,128),
        ("embedding_max_chunks",0,1000000000))}
    if retrieval["embedding"]["enabled"] is not False:
        raise ValueError("Deployment policy embedding.enabled must remain false; use explicit embed command")
    vision=None
    if dep.getboolean("authorized_visual_llm",fallback=False):
        from visual_enrichment import settings as model_settings,execution_settings
        model=_json_profile(dep["visual_llm_profile"])
        execution=_json_profile(dep["visual_llm_execution"])
        model["authorized_provider"]=True
        execution["authorized_result_storage"]=True
        model=model_settings(model)
        execution=execution_settings(execution,model)
        vision=(model,execution)
    translation=None
    if dep.getboolean("authorized_translation",fallback=False):
        from translation_stage import production_settings
        translation=production_settings({
            "model":dep.get("translation_model","nmt"),
            "rpm":dep.getint("translation_rpm",30),
            "workers":dep.getint("translation_workers",1),
            "max_chars":dep.getint("translation_max_chars",5000),
            "timeout_seconds":dep.getint("translation_timeout_seconds",60),
            "attempts":dep.getint("translation_attempts",3)})
    from optional_enrichment import settings as optional_settings
    optional=optional_settings(cfg["ENRICHMENT"] if cfg.has_section("ENRICHMENT") else cfg["DEFAULT"])
    overlap=dep.getboolean("overlap_enrichment",fallback=False)
    enable_embeddings=cfg["PIPELINE"].getboolean("enable_embeddings",fallback=False)
    if enable_embeddings and not dep.getboolean("authorized_embedding"):
        raise PermissionError("Embedding switch requires explicit provider authorization")
    return {"optional_enrichment":optional,"overlap_enrichment":overlap,"enable_embeddings":enable_embeddings,"translation":translation,"visual_llm":vision,"capture":capture,"retrieval":retrieval,"batch":batch,"docx":docx,
            "evidence":evidence,"source":source,"receipts":receipts,"inventory":inventory,
            "limits":limits,"relations":relations,
            "authorized_ocr":dep.getboolean("authorized_ocr"),
            "authorized_embedding":dep.getboolean("authorized_embedding"),
            "gcp_key":cfg["GCP"]["gcp_key_path"],
            "pool_enabled":db.getboolean("pool_enabled",fallback=False),
            "pool_max_connections":db.getint("pool_max_connections",fallback=8)}


def plan(d):
    from page_stage import names,extraction_fingerprint
    from reuse_materialize import identifiers
    from deferred_ocr import tables
    native=names(d["capture"])
    output=identifiers(d["retrieval"])
    return {"native_tables":{k:native[k] for k in ("published","work","docs","errors")},
            "ocr_table":tables(d["capture"])["ocr"],
            "retrieval_tables":{k:output[k] for k in ("table","status","failures")},
            "native_fingerprint":extraction_fingerprint(d["capture"]),
            "retrieval_profile_hash":d["retrieval"]["_fingerprint"],
            "workers_max":d["batch"]["workers"],"writes":False,
            "cloud_calls":False}


def _storage_and_inventory(d):
    from retention_batch import iter_inventory,iter_source_files
    import fitz
    if not d["batch"]["authorized_receipt_storage"]:
        raise PermissionError("Explicit receipt storage approval required")
    expected=d["capture"]["extraction"]["retained_evidence"]["renderer"]
    if expected!=str(fitz.VersionBind):
        raise ValueError("Configured renderer does not match installed PyMuPDF")
    source=d["source"].resolve(strict=True)
    evidence=d["evidence"].resolve(strict=True)
    receipts=d["receipts"].resolve(strict=True)
    if not source.is_dir() or not os.access(source,os.R_OK|os.X_OK):
        raise ValueError("Source root not readable")
    for target in (evidence,receipts):
        if not target.is_dir() or not os.access(target,os.W_OK|os.X_OK):
            raise ValueError("Approved output directory not writable")
        if target==source or source in target.parents:
            raise ValueError("Output directories must be outside the source purge tree")
        if shutil.disk_usage(target).free<d["limits"]["preflight_min_free_gib"]*1024**3:
            raise ValueError("Output filesystem below configured free-space floor")
    total=sampled=docx_count=expired=0
    from retention_batch import _deadline
    now=datetime.now(timezone.utc)
    from contextlib import nullcontext
    with (d["inventory"].open("rb") if d["inventory"] else nullcontext()) as f:
        rows=iter_inventory(f,d["batch"]) if f else ({"path":str(path)} for path in iter_source_files(source,d["batch"]["max_documents"]))
        for row in rows:
            total+=1
            path=Path(row["path"])
            # Windows temp directories can have both short and long path aliases.
            if ".." in path.parts or not path.resolve().is_relative_to(source):
                raise ValueError("Inventory path outside configured source root")
            if path.suffix.lower()==".docx":
                docx_count+=1
            if "purge_at_utc" in row and _deadline(row["purge_at_utc"])<=now:
                expired+=1
                continue
            if sampled<d["limits"]["preflight_sample_files"]:
                resolved=path.resolve(strict=True)
                if not resolved.is_relative_to(source) or not resolved.is_file() or not os.access(resolved,os.R_OK):
                    raise ValueError("Sample source is unreadable or resolves outside source root")
                if path.suffix.lower()==".pdf":
                    with fitz.open(resolved) as pdf:
                        if pdf.is_encrypted or pdf.page_count<1:
                            raise ValueError("Sample PDF is encrypted or empty")
                else:
                    import zipfile
                    with zipfile.ZipFile(resolved) as z:
                        if "word/document.xml" not in z.namelist():
                            raise ValueError("Sample DOCX lacks document part")
                sampled+=1
    if not total or not sampled:
        raise ValueError("No readable nonexpired inventory sample")
    if docx_count and not d["batch"]["authorized_docx_derivatives"]:
        raise PermissionError("DOCX derivative policy approval required")
    return {"discovered_documents":total,"source_mode":"inventory" if d["inventory"] else "directory","inventory_records":total,"sampled_readable_sources":sampled,"expired_records":expired,
            "docx_records":docx_count,"free_evidence_gib":round(shutil.disk_usage(evidence).free/1024**3,2),
            "free_receipt_gib":round(shutil.disk_usage(receipts).free/1024**3,2),
            "renderer":expected,"source_hashes_verified_by":"capture_worker",
            "scope":"source discovery and sampled readability only; no deadline required"}


def _database(d,*,before_init=False):
    from db import get_conn
    from page_stage import require_initialized
    from reuse_materialize import verify_output
    from profile_store import _safe_identifier
    conn=get_conn()
    present={}
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('statement_timeout',%s,true)",
                        (str(d["limits"]["preflight_statement_timeout_ms"]),))
            cur.execute("SELECT current_database(),current_setting('max_connections')::int,"
                        " EXISTS(SELECT 1 FROM pg_type WHERE typname='vector')")
            database,max_connections,vector=cur.fetchone()
            if not vector:
                raise ValueError("DBA must install pgvector before initializing retrieval tables")
            for schema,table in d["relations"]:
                name=_safe_identifier(schema)+"."+_safe_identifier(table)
                cur.execute("SELECT to_regclass(%s)",(name,))
                present[(schema,table)]=cur.fetchone()[0] is not None
                if present[(schema,table)]:
                    for privilege in ("SELECT","INSERT","UPDATE","DELETE"):
                        cur.execute("SELECT has_table_privilege(%s,%s)",(name,privilege))
                        if not cur.fetchone()[0]:
                            raise PermissionError("Missing "+privilege+" on configured relation "+name)
            core=[present[x] for x in d["relations"][:4]]
            chunks=[present[x] for x in d["relations"][5:]]
            if (any(core) and not all(core)) or (any(chunks) and not all(chunks)):
                raise ValueError("Partial table initialization; DBA review required")
            if not before_init and not (all(core) and all(chunks)):
                raise ValueError("Required tables absent; explicit init --apply required")
    finally:
        conn.rollback()
        conn.close()
    # Existing validators perform read-only catalog/profile checks.
    if all(core):
        require_initialized(d["capture"])
    if all(chunks):
        verify_output(d["retrieval"],logical_locators=True)
    return {"database":database,"max_connections":max_connections,"pgvector":vector,
            "native_tables_ready":all(core),"retrieval_tables_ready":all(chunks),
            "ocr_table_exists":present[d["relations"][4]],
            "note":"Catalog and table DML privilege checks; no write probe, sequence grants, storage or SLA certification"}


def preflight(d,*,before_init=False):
    if d["batch"].get("resource_guard") and not sys.platform.startswith("linux"):
        raise ValueError("Adaptive resource profile requires Linux; use fixed profile for Windows validation")
    from page_stage import settings as native_settings
    native_settings(d["capture"])
    if d["pool_enabled"] and d["pool_max_connections"]<2*d["limits"]["materialize_workers"]+1:
        raise ValueError("Shared materialization pool needs 2*workers+1 connections; lower materialize_workers or raise pool limit")
    report=plan(d)
    report["host"]={"cpu_count":os.cpu_count(),"resource_guard_enabled":bool(d["batch"].get("resource_guard"))}
    if sys.platform.startswith("linux"):
        from resource_guard import LinuxMetrics
        _,available,group=LinuxMetrics()()
        report["host"].update(available_memory_gib=round(available/1024**3,2),
                              current_cgroup_memory_gib=round(group/1024**3,2) if group is not None else None)
    report.update(storage=_storage_and_inventory(d),database=_database(d,before_init=before_init),
                  status="PREFLIGHT_PASSED",purge_safe=False)
    return report


def status(d):
    """Read-only operational counts from existing manifests and the OCR queue.

    No page/chunk payload scans, provider calls, schema initialization, or
    assumption that retrieval's materialization digest equals the PDF hash.
    Counts cover the configured tables, not source inventory or semantic QA.
    """
    from db import get_conn
    from page_stage import names as native_names, extraction_fingerprint
    from reuse_materialize import identifiers
    from deferred_ocr import tables as ocr_tables, settings as ocr_settings, ocr_fingerprint
    native=native_names(d["capture"])
    retrieval=identifiers(d["retrieval"])
    ocr=ocr_tables(d["capture"])["ocr"]
    native_hash=extraction_fingerprint(d["capture"])
    retrieval_hash=d["retrieval"]["_fingerprint"]
    current_ocr_hash=ocr_fingerprint(ocr_settings(d["capture"]))
    report={"writes":False,"cloud_calls":False,"native":None,"retrieval":None,
            "ocr":None,"scope":"Stored manifest totals and current-version OCR jobs; not semantic quality or source freshness certification"}
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT set_config('statement_timeout',%s,true)",
                        (str(d["limits"]["preflight_statement_timeout_ms"]),))
            def exists(relation):
                cur.execute("SELECT to_regclass(%s)",(relation,))
                return cur.fetchone()[0] is not None
            if exists(native["docs"]):
                cur.execute(f"""SELECT extraction_hash,COUNT(*),COALESCE(SUM(page_count),0)
                    FROM {native['docs']} GROUP BY extraction_hash ORDER BY extraction_hash""")
                versions=[{"extraction_hash":h,"documents":int(n),"pages":int(v)}
                          for h,n,v in cur.fetchall()]
                report["native"]={"documents":sum(v["documents"] for v in versions),
                                  "pages":sum(v["pages"] for v in versions),
                                  "configured_extraction_hash":native_hash,
                                  "versions":versions}
            if exists(retrieval["status"]):
                cur.execute(f"""SELECT profile_hash,COUNT(*),COALESCE(SUM(chunk_count),0)
                    FROM {retrieval['status']} GROUP BY profile_hash ORDER BY profile_hash""")
                versions=[{"profile_hash":h,"documents":int(n),"chunks":int(v)}
                          for h,n,v in cur.fetchall()]
                report["retrieval"]={"documents":sum(v["documents"] for v in versions),
                    "chunks":sum(v["chunks"] for v in versions),
                    "configured_profile_hash":retrieval_hash,
                    "profile_matches":all(v["profile_hash"]==retrieval_hash for v in versions),
                    "versions":versions}
            if report["native"] is not None and exists(ocr):
                cur.execute(f"""SELECT j.status,COUNT(*) FROM {ocr} j
                    JOIN {native['docs']} n ON n.doc_id=j.doc_id
                     AND n.source_hash=j.source_hash AND n.extraction_hash=j.extraction_hash
                    WHERE n.extraction_hash=%s AND j.ocr_hash=%s
                    GROUP BY j.status ORDER BY j.status""",(native_hash,current_ocr_hash))
                report["ocr"]={"ocr_hash":current_ocr_hash,"statuses":dict(cur.fetchall())}
    finally:
        conn.rollback()
        conn.close()
    return report


def finish_captured(d,receipt,*,records=None,admit=None):
    """Process only the successful capture records from this invocation.

    Source-native text is never replaced by model content. OCR and model
    results become separately labeled, page-linked chunks in the same table.
    """
    from collections import Counter
    import deferred_ocr as ocr
    from reuse_materialize import run_one,run_docx,verify_output
    from visual_enrichment import run_pages,LocalQuota,safe_failure_code
    p,q=d["capture"],d["retrieval"]
    quota=LocalQuota(d["visual_llm"][1]) if d["visual_llm"] else None
    names=verify_output(q)
    from translation_stage import GoogleTranslation
    if not d.get("translation"):
        raise PermissionError("English retrieval requires authorized_translation=true")
    translator=GoogleTranslation(d["gcp_key"],d["translation"])
    counts=Counter()
    log=logging.getLogger(__name__)
    with (Path(receipt).open(encoding="utf-8") if records is None else contextlib.nullcontext(records)) as stream:
        for line in stream:
            row=json.loads(line)
            if row["status"]!="LOCAL_CAPTURE_VERIFIED":
                continue
            doc=row["doc_id"]
            started=time.monotonic()
            stage="admission"
            failure_code=None
            log.info("finish doc=%s started",doc[:12])
            try:
                if admit is not None and not admit():
                    counts["NOT_ADMITTED_RESOURCE"]+=1
                    continue
                if row["format"]=="docx":
                    stage="docx_materialize"
                    result=run_docx(d["evidence"],row["capture_id"],q,config=d["docx"],
                                    doc_id=doc,file_name=row["file_name"],allow_logical_locators=True,translator=translator)
                else:
                    stage="ocr_discovery"
                    tick=time.monotonic()
                    discovered=ocr.enqueue(p,doc_id=doc,max_docs=1,apply=True)
                    log.info("finish doc=%s stage=%s candidates=%s inserted=%s elapsed=%.2fs",
                             doc[:12],stage,discovered["candidates"],discovered["inserted"],time.monotonic()-tick)
                    stage="ocr"
                    tick=time.monotonic()
                    # Complete OCR does not need another provider pool on replay.
                    try:
                        parts,visual_pages=ocr.retrieval_results(p,doc,row["source_sha256"],discovered["candidates"])
                        log.info("finish doc=%s OCR coverage already complete",doc[:12])
                    except ValueError as exc:
                        if str(exc)!="OCR coverage incomplete; retry before enriched publication":
                            raise
                        if not d["authorized_ocr"]:
                            raise PermissionError("OCR provider approval required for visual source")
                        report=ocr.run(p,max_jobs=d["limits"]["ocr_max_jobs"],doc_id=doc)
                        log.info("finish doc=%s OCR processed=%s statuses=%s",
                                 doc[:12],report["processed"],report["statuses"])
                        parts,visual_pages=ocr.retrieval_results(p,doc,row["source_sha256"],discovered["candidates"])
                    log.info("finish doc=%s stage=%s elapsed=%.2fs visual_pages=%s",
                             doc[:12],stage,time.monotonic()-tick,len(visual_pages))
                    stage="visual_llm"
                    tick=time.monotonic()
                    if visual_pages:
                        if not d["visual_llm"]:
                            raise PermissionError("Visual LLM authorization/configuration required")
                        model,execution=d["visual_llm"]
                        # One bounded pool for the selected pages of this document.
                        # Explicitly close on error so no new work is admitted.
                        observed_pages=set()
                        outcomes=run_pages(d["evidence"],((row["capture_id"],n) for n in visual_pages),
                                visual_settings=p["extraction"]["retained_evidence"]["visual_settings"],
                                config=model,execution=execution,include_result=True,quota=quota,
                                document_pages=len(visual_pages))
                        with contextlib.closing(outcomes) as stream:
                            for outcome in stream:
                                if outcome["status"] not in ("COMPLETED_UNVERIFIED","SKIPPED_VERIFIED"):
                                    failure_code=outcome.get("error_code",outcome.get("error_type","VISUAL_FAILED"))
                                    log.error("finish doc=%s stage=visual_llm page=%s code=%s",
                                              doc[:12],outcome["source_page"],failure_code)
                                    raise RuntimeError("Visual model enrichment failed")
                                observed_pages.add(outcome["source_page"])
                                result=outcome["verified_result"]
                                parts.append({"page":outcome["source_page"],"kind":"visual_model",
                                    "text":"\n".join(
                                        ["[MODEL OBSERVATION — UNVERIFIED] "+item["observation"] for item in result["result"]["observations"]]
                                        +["[MODEL LIMITATION] "+item for item in result["result"]["limitations"]]),
                                    "provenance":{"request_id":result["request_id"],"capture_id":row["capture_id"],
                                                  "source_facts_verified":False}})
                        if observed_pages!=set(visual_pages):
                            raise RuntimeError("Visual page coverage incomplete")
                        log.info("finish doc=%s visual_pages_completed=%s",doc[:12],len(observed_pages))
                    log.info("finish doc=%s stage=%s elapsed=%.2fs",doc[:12],stage,time.monotonic()-tick)
                    stage="translation_materialize"
                    tick=time.monotonic()
                    parts.sort(key=lambda item:(item["page"],item["kind"],json.dumps(item["provenance"],sort_keys=True)))
                    result=run_one(doc,q,names,enrichment=parts,expected_source_hash=row["source_sha256"],translator=translator)
                counts[result["status"]]+=1
                log.info("finish doc=%s stage=%s status=%s total_elapsed=%.2fs",
                         doc[:12],stage,result["status"],time.monotonic()-started)
            except Exception as exc:
                code=failure_code or safe_failure_code(exc)
                log.error("finish doc=%s stage=%s code=%s elapsed=%.2fs",
                          doc[:12],stage,code,time.monotonic()-started)
                counts["FAILED"]+=1
                # These indicate a provider-wide authorization/deployment problem.
                # Stop additional documents rather than spend more on doomed work.
                if stage=="visual_llm" and code in ("HTTP_401","HTTP_403","HTTP_404",
                                                     "MODEL_DEPLOYMENT_MISMATCH"):
                    log.error("finish stopped: systemic visual provider error; fix configuration before retry")
                    break
    return dict(counts)


def _overlap_batch(d):
    """Reserve reader processes from the existing native CPU process budget."""
    cfg=copy.deepcopy(d["batch"])
    readers=d["capture"]["deferred_ocr"]["retained_evidence"]["read_workers"]
    if d["visual_llm"]:
        readers=max(readers,d["visual_llm"][1]["read_workers"])
    if cfg["workers"]<=readers:
        raise ValueError("Overlap requires worker budget greater than OCR/LLM reader processes")
    cfg["workers"]-=readers
    if cfg.get("resource_guard"):
        cfg["resource_guard"]["initial_workers"]=min(cfg["workers"],cfg["resource_guard"]["initial_workers"])
    return cfg,readers


def _live_receipts(path,done):
    """Read complete, flushed JSONL records; disk is the bounded-memory backlog."""
    path=Path(path)
    while not path.exists():
        if done.wait(.1):
            if not path.exists():
                return
            break
    with path.open("rb") as stream:
        while True:
            start=stream.tell()
            line=stream.readline()
            if line.endswith(b"\n"):
                yield line
                continue
            if done.is_set():
                # Writer has closed/fsynced. Retry once to cover the race
                # between the previous read and producer completion.
                stream.seek(start)
                line=stream.readline()
                if line.endswith(b"\n"):
                    yield line
                    continue
                if line:
                    raise ValueError("Incomplete capture receipt record")
                return
            stream.seek(start)
            done.wait(.1)


def _enrichment_admission(config):
    """One enrichment document at a time; memory pause before new work."""
    if not config.get("resource_guard"):
        return None
    from resource_guard import ResourceGuard
    values={**config["resource_guard"],"initial_workers":1}
    guard=ResourceGuard(values,1)
    abandoned=False
    def admit():
        nonlocal abandoned
        if abandoned:
            return False
        start=time.monotonic()
        while guard.poll()==0:
            if time.monotonic()-start>=values["max_pause_seconds"]:
                abandoned=True
                return False
            time.sleep(values["poll_seconds"])
        return True
    return admit


def run_capture_and_finish(d,receipt,*,overlap=False,finish=False):
    """Existing capture and provider workers, with optional one-consumer overlap.

    No in-memory document queue or additional database table. Native capture
    keeps priority; provider errors do not stop capturing other documents.
    Embeddings run only after both stages finish.
    """
    from retention_batch import run_batch,run_root
    cfg,readers=_overlap_batch(d) if overlap and finish else (d["batch"],0)
    streaming=bool(overlap and finish)
    def capture():
        kw=dict(docx_config=d["docx"],config=cfg,stream_receipts=streaming)
        if d["inventory"]:
            return run_batch(d["inventory"],receipt,d["capture"],**kw)
        return run_root(d["source"],receipt,d["capture"],**kw)
    if not streaming:
        result=capture()
        if finish:
            result["retrieval"]=finish_captured(d,receipt)
        return result
    done=threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(finish_captured,d,receipt,records=_live_receipts(receipt,done),
                           admit=_enrichment_admission(cfg))
        try:
            result=capture()
        finally:
            done.set()
        result["retrieval"]=future.result()
    result["overlap"]={"native_worker_max":cfg["workers"],"reserved_reader_processes":readers,
                       "enrichment_documents_in_flight":1}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("plan","status","preflight","init","run","capture","materialize","docx-preview","docx-publish",
                                           "finish","enrich-preview","enrich","ocr-init","ocr-discover","ocr-enqueue","ocr-run","ocr-status","ocr-recover-crops","visual-plan","visual-probe",
                                           "embed-preview","embed"))
    parser.add_argument("--before-init",action="store_true")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--doc-id")
    parser.add_argument("--all-docs",action="store_true")
    parser.add_argument("--receipt")
    parser.add_argument("--capture-id")
    parser.add_argument("--page",type=int)
    parser.add_argument("--file-name")
    parser.add_argument("--allow-logical-locators",action="store_true")
    parser.add_argument("--retry-failed",action="store_true")
    parser.add_argument("--rescan-source",action="store_true")
    args=parser.parse_args(argv)
    if args.page is not None and (args.command!="visual-probe" or args.page<1):
        parser.error("--page requires visual-probe and a positive physical page number")
    if args.before_init and args.command!="preflight":
        parser.error("--before-init applies only to preflight")
    if (args.retry_failed or args.rescan_source) and args.command!="embed":
        parser.error("Embedding retry/rescan options apply only to embed")
    writes={"init","run","capture","finish","materialize","docx-publish","ocr-init","ocr-enqueue","ocr-run","embed","enrich"}
    if args.command in writes and not args.apply:
        parser.error("Explicit --apply required for writes or provider calls")
    # All existing modules and spawned workers read config.ini in the working
    # directory. Do not support an alternate path that only the wrapper sees.
    if Path.cwd()!=ROOT:
        parser.error("Run from the repository directory containing local config.ini")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    d=resolve_configuration()
    p=d["capture"]
    q=d["retrieval"]
    lim=d["limits"]
    cmd=args.command
    code=0
    if cmd=="plan":
        result=plan(d)
    elif cmd=="status":
        result=status(d)
    elif cmd=="preflight":
        result=preflight(d,before_init=args.before_init)
    elif cmd=="init":
        preflight(d,before_init=True)
        from page_stage import init
        from reuse_materialize import init_output
        result={"native":init(p,apply=True),"retrieval":init_output(q,apply=True)}
        if d["authorized_ocr"]:
            from deferred_ocr import init as init_ocr
            result["ocr"]=init_ocr(p,apply=True)
        result["verified"]=_database(d)
    elif cmd in ("run","capture"):
        preflight(d)
        if cmd=="run":
            if not d["translation"]:
                parser.error("English retrieval requires authorized_translation=true; use capture for evidence only")
            if not d["authorized_ocr"]:
                parser.error("run requires authorized_ocr=true; use capture for native-only capture")
            if not _database(d)["ocr_table_exists"]:
                parser.error("Run init --apply once with authorized_ocr=true")
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex
        receipt=d["receipts"]/(run_id+".jsonl")
        result=run_capture_and_finish(d,receipt,overlap=d["overlap_enrichment"],finish=cmd=="run")
        result["receipt"]=str(receipt)
        code=int(not result["all_local_capture_verified"])
        if cmd=="run":
            finished=result["retrieval"]
            code=int(bool(code or finished.get("FAILED") or finished.get("BUSY") or finished.get("NOT_ADMITTED_RESOURCE")))
            if d["enable_embeddings"] and not code:
                from existing_embeddings import embed_existing
                ep=copy.deepcopy(q)
                ep["embedding"]["enabled"]=True
                result["embedding"]=embed_existing(ep,max_chunks=lim["embedding_max_chunks"] or None,
                    workers=lim["embedding_workers"],rescan_source=True)
    elif cmd=="finish":
        if not args.receipt or not d["authorized_ocr"]:
            parser.error("finish requires --receipt and authorized_ocr=true")
        _database(d)
        receipt=Path(args.receipt).resolve(strict=True)
        if not receipt.is_relative_to(d["receipts"].resolve(strict=True)):
            parser.error("Receipt must be in approved receipt directory")
        if args.doc_id:
            if not re.fullmatch(r"[0-9a-f]{64}",args.doc_id):
                parser.error("finish --doc-id requires the full 64-character ID")
            with receipt.open(encoding="utf-8") as stream:
                selected=[line for line in stream if (row:=json.loads(line)).get("doc_id")==args.doc_id
                          and row.get("status")=="LOCAL_CAPTURE_VERIFIED"]
            if len(selected)!=1:
                parser.error("Expected exactly one verified capture record for --doc-id")
            result=finish_captured(d,receipt,records=selected)
        else:
            result=finish_captured(d,receipt)
        code=int(bool(result.get("FAILED") or result.get("BUSY") or result.get("NOT_ADMITTED_RESOURCE")))
    elif cmd=="visual-plan":
        import deferred_ocr as ocr
        if args.apply:
            parser.error("visual-plan is read-only")
        result=ocr.visual_plan(p,doc_id=args.doc_id)
    elif cmd=="visual-probe":
        import deferred_ocr as ocr
        from visual_evidence import capture_identity,_capture_id
        from visual_enrichment import probe_page
        if not args.doc_id or args.all_docs or not d["visual_llm"]:
            parser.error("visual-probe requires one full --doc-id and approved visual configuration")
        selected=ocr.visual_plan(p,doc_id=args.doc_id)["documents"]
        if len(selected)!=1:
            parser.error("No current OCR-selected visual page for this document")
        item=selected[0]
        page=args.page if args.page is not None else item["first_page"]
        if not ocr.visual_page_selected(p,args.doc_id,item["source_hash"],page):
            parser.error("Page is not selected by the current visual policy")
        retained=p["extraction"]["retained_evidence"]
        cid=_capture_id(capture_identity(item["source_hash"],retained["visual_settings"],
                            retained["policy_id"],retained["renderer"]))
        model,execution=d["visual_llm"]
        result=probe_page(d["evidence"],cid,page,visual_settings=retained["visual_settings"],
                          config=model,execution=execution,apply=args.apply)
        code=int(result["status"]=="FAILED")
    elif cmd in ("enrich-preview","enrich"):
        if not args.doc_id or args.all_docs:
            parser.error("Optional enrichment requires one full --doc-id")
        from optional_enrichment import run as enrich
        result=enrich(d,args.doc_id,apply=cmd=="enrich")
        code=int(bool(result["outcomes"].get("FAILED") or result["outcomes"].get("BUSY")))
    elif cmd=="materialize":
        if q["output"].get("text_contract")=="english_v1":
            parser.error("Use run or finish for English-only retrieval; native-only publication is disabled")
        _database(d)
        from reuse_materialize import run,run_one,verify_output
        if args.doc_id:
            result=run_one(args.doc_id,q,verify_output(q))
            code=int(result["status"] not in ("SUCCESS","SKIPPED"))
        else:
            if not args.all_docs:
                parser.error("materialize requires --doc-id or --all-docs")
            result=run(q,max_docs=lim["materialize_max_docs"],workers=lim["materialize_workers"])
            code=int(bool(result.get("FAILED") or result.get("BUSY")))
    elif cmd in ("docx-preview","docx-publish"):
        if not all((args.doc_id,args.capture_id,args.file_name)):
            parser.error("DOCX commands require --doc-id, --capture-id, --file-name")
        from reuse_materialize import prepare_docx_document,run_docx
        kw=dict(config=d["docx"],doc_id=args.doc_id,file_name=args.file_name)
        if cmd=="docx-preview":
            _,result=prepare_docx_document(d["evidence"],args.capture_id,q,**kw)
        else:
            if not args.allow_logical_locators:
                parser.error("DOCX publication requires --allow-logical-locators")
            _database(d)
            if not d["translation"]:
                parser.error("English retrieval requires authorized_translation=true")
            from translation_stage import GoogleTranslation
            result=run_docx(d["evidence"],args.capture_id,q,allow_logical_locators=True,
                            translator=GoogleTranslation(d["gcp_key"],d["translation"]),**kw)
            code=int(result["status"] not in ("SUCCESS","SKIPPED"))
    elif cmd.startswith("ocr-"):
        import deferred_ocr as ocr
        _database(d)
        if cmd=="ocr-init":
            result=ocr.init(p,apply=True)
        elif cmd=="ocr-status":
            result=ocr.status(p,doc_id=args.doc_id)
        elif cmd=="ocr-recover-crops":
            if not args.doc_id or args.all_docs:
                parser.error("Crop recovery requires one full --doc-id")
            result=ocr.recover_boundary_crops(p,args.doc_id,apply=args.apply,
                                             limit=min(1000,lim["ocr_max_jobs"]))
            code=int(bool(result["errors"]))
        elif cmd in ("ocr-discover","ocr-enqueue"):
            if bool(args.doc_id)==bool(args.all_docs):
                parser.error("Choose exactly one of --doc-id or --all-docs")
            result=ocr.enqueue(p,doc_id=args.doc_id,max_docs=lim["ocr_max_docs"],apply=cmd=="ocr-enqueue")
        else:
            if not d["authorized_ocr"]:
                raise PermissionError("Explicit OCR provider authorization required")
            if bool(args.doc_id)==bool(args.all_docs):
                parser.error("Choose exactly one of --doc-id or --all-docs")
            if not Path(d["gcp_key"]).is_file():
                raise ValueError("Configured GCP credential file not found")
            result=ocr.run(p,max_jobs=lim["ocr_max_jobs"],doc_id=args.doc_id)
            code=int(any(result.get("statuses",{}).get(k,0) for k in ("FAILED_TERMINAL","RETRY_WAIT")))
    else:
        _database(d)
        if cmd=="embed" and not d["authorized_embedding"]:
            raise PermissionError("Explicit embedding provider authorization required")
        from existing_embeddings import embed_existing
        ep=copy.deepcopy(q)
        ep["embedding"]["enabled"]=True
        result=embed_existing(ep,dry_run=cmd=="embed-preview",
                              max_chunks=lim["embedding_max_chunks"] or None,
                              workers=lim["embedding_workers"],retry_failed=args.retry_failed,
                              rescan_source=args.rescan_source)
    print(json.dumps(result,indent=2,ensure_ascii=True,default=str))
    return code


if __name__=="__main__":
    raise SystemExit(main())
