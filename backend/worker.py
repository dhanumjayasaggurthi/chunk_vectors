"""Run as independent processes: `python -m backend.worker --concurrency 4`.
One worker = one document in-flight. Scale processes/containers by backlog.
"""
from __future__ import annotations
import argparse
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from . import config,store
from .integrations import DemoResolver,SnowflakeResolver,S3Reader,PermanentError,SnowflakeEntitlements
from .extract import extract_document
from .azure_llm import JnJAzureLLM
log=logging.getLogger("evidenceflow.worker")

DEMO_FIELDS={
 "document_date":dict(value="2022-08-24",display="24 Aug 2022",page=11,excerpt="Status: Approved, Date: 24 August 2022",method="SCREENSHOT",validation="SCREENSHOT_ONLY"),
 "international_birth_date":dict(value="2008-12-12",display="12 Dec 2008",page=12,excerpt="The International Birth Date is 12 December 2008.",method="SCREENSHOT",validation="SCREENSHOT_ONLY"),
 "product":dict(value="STELARA",display="STELARA®",page=12,excerpt="STELARA® (Ustekinumab)",method="SCREENSHOT",validation="SCREENSHOT_ONLY"),
 "active_ingredient":dict(value="Ustekinumab",display="Ustekinumab",page=12,excerpt="STELARA® (Ustekinumab)",method="SCREENSHOT",validation="SCREENSHOT_ONLY")}
DEMO_SUMMARY="This safety report reviews infections in infants exposed to ustekinumab in utero following maternal use of STELARA during pregnancy."

_resolver=None
_acl_resolver=None

def resolver_instance():
    global _resolver
    if _resolver is None:
        _resolver=DemoResolver() if config.MODE=="demo" else SnowflakeResolver()
    return _resolver

def resolve(limit=200):
    global _acl_resolver
    tasks=store.claim_pending(limit)
    if not tasks:return 0
    resolver=resolver_instance()
    try:
        # Snowflake queries are bounded at 500 IDs. Isolate by job only for tracking.
        ids=list(dict.fromkeys(id for _,id in tasks))
        found=resolver.lookup(ids)
    except Exception as e:
        # Lookup is retryable; reset claimed rows (not falsely marked missing).
        log.exception("Resolver failed")
        with store.Session.begin() as s:
            for jid,rid in tasks:
                d=s.get(store.Document,(jid,rid))
                if d and d.status=="RESOLVING":d.status="PENDING"
        return 0
    security={jid:store.job_security(jid) for jid,_ in tasks}
    permitted_by_job={}
    if config.MODE=='live':
        if config.SF_ENTITLEMENTS_TABLE and _acl_resolver is None:_acl_resolver=SnowflakeEntitlements()
        acl=_acl_resolver
        try:
            for jid in security:
                info=security[jid]
                if not info:permitted_by_job[jid]=set();continue
                if info['read_all']:
                    permitted_by_job[jid]={rid for j,rid in tasks if j==jid}
                elif acl:
                    permitted_by_job[jid]=acl.allowed([rid for j,rid in tasks if j==jid],info['owner'],info['tenant'])
                else:permitted_by_job[jid]=set()
        except Exception:
            log.exception('Entitlement lookup unavailable, will retry instead of guessing')
            with store.Session.begin() as s:
                for jid,rid in tasks:
                    d=s.get(store.Document,(jid,rid))
                    if d and d.status=='RESOLVING':d.status='PENDING'
            return 0
    decisions=[]
    for jid,rid in tasks:
        if config.MODE=='live' and rid not in permitted_by_job.get(jid,set()):
            decisions.append((jid,rid,None,'Not found or not authorized',True));continue
        meta=found.get(rid)
        if not meta:decisions.append((jid,rid,None,'ID not found in metadata source',True))
        elif meta.get('ambiguous'):decisions.append((jid,rid,None,'Multiple distinct metadata paths; manual resolution required',False))
        elif str(meta.get('a_status','')).lower()!='approved':
            decisions.append((jid,rid,meta,'Source document is not approved',False))
        elif str(meta.get('ingestion_status','')).upper()!='SUCCESS':
            decisions.append((jid,rid,meta,'Source ingestion status is not SUCCESS',False))
        elif not meta.get('file_path'):
            decisions.append((jid,rid,meta,'S3 file path is missing',False))
        else:decisions.append((jid,rid,meta,None,False))
    store.resolve_results_batch(decisions)
    return len(tasks)

def process_one():
    task=store.claim_document()
    if not task:return False
    stop=threading.Event()
    def keep_alive():
        while not stop.wait(max(5,min(60,config.LEASE_SECONDS//3))):
            try:store.heartbeat(task)
            except Exception:log.exception("Could not renew worker lease")
    if config.MODE=="live":
        threading.Thread(target=keep_alive,daemon=True).start()
    try:
        if config.MODE=="demo":
            if task["r_object_id"]=="0901bacc84b20d46":
                store.finish(task,DEMO_FIELDS,DEMO_SUMMARY,"COMPLETED",
                             "Screenshot-only fixture; not independently extracted from a source PDF.","demo-screenshot")
            else:
                store.finish(task,{},None,"REVIEW_REQUIRED","Demo metadata exists; real PDF and AI extraction are not connected")
        else:
            data,version=S3Reader().read(task["s3_uri"])
            store.heartbeat(task)
            fields,summary,status,note=extract_document(data,JnJAzureLLM())
            store.finish(task,fields,summary,status,note,version)
    except PermanentError as e:
        store.fail(task,str(e),retryable=False)
    except Exception as e:
        log.exception("Document processing failed: job=%s doc=%s",task["job_id"],task["r_object_id"])
        store.fail(task,type(e).__name__,retryable=True)  # no sensitive text in persistent error
    finally:
        stop.set()
    return True

def once(concurrency=4):
    resolved=resolve()
    # Thread execution reuses the same Python worker logic; each thread has its own DB session.
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        handled=sum(pool.map(lambda _: process_one(),range(concurrency)))
    return resolved+handled

def serve(concurrency=4,interval=1):
    if config.MODE=="live" and not config.DATABASE_URL.startswith("postgresql"):
        raise RuntimeError("Live mode requires PostgreSQL for concurrent worker claims")
    store.init_db();log.info("EvidenceFlow worker mode=%s parallelism=%s",config.MODE,concurrency)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while True:
            resolved=resolve()
            handled=sum(pool.map(lambda _:process_one(),range(concurrency)))
            if not resolved and not handled:time.sleep(interval)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--concurrency",type=int,default=4)
    a=p.parse_args();logging.basicConfig(level=logging.INFO)
    serve(max(1,min(a.concurrency,32)))
