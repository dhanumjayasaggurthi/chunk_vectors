"""One config.ini per deployment. No implicit DDL or cloud calls."""
from __future__ import annotations

import argparse
import configparser
import copy
import json
import logging
import os
import re
import shutil
import sys
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
    value=section.getint(key)
    if not low<=value<=high:
        raise ValueError("Invalid deployment setting "+key)
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
            "output.schema":target_schema,"output.table":chunk,
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
            "max_chars":dep.getint("translation_max_chars",5000),
            "timeout_seconds":dep.getint("translation_timeout_seconds",60),
            "attempts":dep.getint("translation_attempts",3)})
    return {"translation":translation,"visual_llm":vision,"capture":capture,"retrieval":retrieval,"batch":batch,"docx":docx,
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


def finish_captured(d,receipt):
    """Process only the successful capture records from this invocation.

    Source-native text is never replaced by model content. OCR and model
    results become separately labeled, page-linked chunks in the same table.
    """
    from collections import Counter
    import deferred_ocr as ocr
    from reuse_materialize import run_one,run_docx,verify_output
    from visual_enrichment import run_pages,LocalQuota
    p,q=d["capture"],d["retrieval"]
    quota=LocalQuota(d["visual_llm"][1]) if d["visual_llm"] else None
    names=verify_output(q)
    from translation_stage import GoogleTranslation
    translator=GoogleTranslation(d["gcp_key"],d["translation"]) if d.get("translation") else None
    counts=Counter()
    with Path(receipt).open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row["status"]!="LOCAL_CAPTURE_VERIFIED":
                continue
            doc=row["doc_id"]
            try:
                if row["format"]=="docx":
                    result=run_docx(d["evidence"],row["capture_id"],q,config=d["docx"],
                                    doc_id=doc,file_name=row["file_name"],allow_logical_locators=True,translator=translator)
                else:
                    discovered=ocr.enqueue(p,doc_id=doc,max_docs=1,apply=True)
                    if discovered["candidates"]:
                        if not d["authorized_ocr"]:
                            raise PermissionError("OCR provider approval required for visual source")
                        ocr.run(p,max_jobs=d["limits"]["ocr_max_jobs"],doc_id=doc)
                    parts,visual_pages=ocr.retrieval_results(p,doc,row["source_sha256"],discovered["candidates"])
                    if visual_pages:
                        if not d["visual_llm"]:
                            raise PermissionError("Visual LLM authorization/configuration required")
                        model,execution=d["visual_llm"]
                        # Chunk the page list using the configured bounded run size.
                        limit=execution["max_pages"]
                        for start in range(0,len(visual_pages),limit):
                            selected=visual_pages[start:start+limit]
                            for outcome in run_pages(d["evidence"],[(row["capture_id"],n) for n in selected],
                                    visual_settings=p["extraction"]["retained_evidence"]["visual_settings"],
                                    config=model,execution=execution,include_result=True,quota=quota):
                                if outcome["status"] not in ("COMPLETED_UNVERIFIED","SKIPPED_VERIFIED"):
                                    raise RuntimeError("Visual model enrichment failed")
                                result=outcome["verified_result"]
                                parts.append({"page":outcome["source_page"],"kind":"visual_model",
                                    "text":"\n".join(
                                        ["[MODEL OBSERVATION — UNVERIFIED] "+item["observation"] for item in result["result"]["observations"]]
                                        +["[MODEL LIMITATION] "+item for item in result["result"]["limitations"]]),
                                    "provenance":{"request_id":result["request_id"],"capture_id":row["capture_id"],
                                                  "source_facts_verified":False}})
                    parts.sort(key=lambda item:(item["page"],item["kind"],json.dumps(item["provenance"],sort_keys=True)))
                    result=run_one(doc,q,names,enrichment=parts,expected_source_hash=row["source_sha256"],translator=translator)
                counts[result["status"]]+=1
            except Exception as exc:
                logging.getLogger(__name__).error("Post-capture document %s failed: %s",doc,type(exc).__name__)
                counts["FAILED"]+=1
    return dict(counts)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("plan","preflight","init","run","capture","materialize","docx-preview","docx-publish",
                                           "finish","ocr-init","ocr-discover","ocr-enqueue","ocr-run","ocr-status",
                                           "embed-preview","embed"))
    parser.add_argument("--before-init",action="store_true")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--doc-id")
    parser.add_argument("--all-docs",action="store_true")
    parser.add_argument("--receipt")
    parser.add_argument("--capture-id")
    parser.add_argument("--file-name")
    parser.add_argument("--allow-logical-locators",action="store_true")
    parser.add_argument("--retry-failed",action="store_true")
    parser.add_argument("--rescan-source",action="store_true")
    args=parser.parse_args(argv)
    if args.before_init and args.command!="preflight":
        parser.error("--before-init applies only to preflight")
    if (args.retry_failed or args.rescan_source) and args.command!="embed":
        parser.error("Embedding retry/rescan options apply only to embed")
    writes={"init","run","capture","finish","materialize","docx-publish","ocr-init","ocr-enqueue","ocr-run","embed"}
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
            if not d["authorized_ocr"]:
                parser.error("run requires authorized_ocr=true; use capture for native-only capture")
            if not _database(d)["ocr_table_exists"]:
                parser.error("Run init --apply once with authorized_ocr=true")
        from retention_batch import run_batch,run_root
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex
        receipt=d["receipts"]/(run_id+".jsonl")
        result=(run_batch(d["inventory"],receipt,p,docx_config=d["docx"],config=d["batch"]) if d["inventory"]
                else run_root(d["source"],receipt,p,docx_config=d["docx"],config=d["batch"]))
        result["receipt"]=str(receipt)
        code=int(not result["all_local_capture_verified"])
        if cmd=="run":
            finished=finish_captured(d,receipt)
            result["retrieval"]=finished
            code=int(bool(code or finished.get("FAILED") or finished.get("BUSY")))
            if d["authorized_embedding"] and not code:
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
        result=finish_captured(d,receipt)
        code=int(bool(result.get("FAILED") or result.get("BUSY")))
    elif cmd=="materialize":
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
            result=run_docx(d["evidence"],args.capture_id,q,allow_logical_locators=True,**kw)
            code=int(result["status"] not in ("SUCCESS","SKIPPED"))
    elif cmd.startswith("ocr-"):
        import deferred_ocr as ocr
        _database(d)
        if cmd=="ocr-init":
            result=ocr.init(p,apply=True)
        elif cmd=="ocr-status":
            result=ocr.status(p,doc_id=args.doc_id)
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
