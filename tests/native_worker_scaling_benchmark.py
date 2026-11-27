"""Compare native process counts on identical complete public PDF bytes.

No database, paid providers or production source changes. This is a worker
scaling experiment, not semantic table accuracy or end-to-end qualification.
"""
from __future__ import annotations

import argparse
from functools import partial
import json
import logging
import os
from pathlib import Path
import platform
import tempfile
import time

from online_public_regulatory_benchmark import (
    candidates, download, extract, performance_summary, run_extraction,
)

# Selected from the successful 100-original-document run 45953594852,
# spanning source page counts 281..1521, rather than duplicated copies.
FIXTURES=(
    "CFR-2025-title7-vol13",
    "CFR-2025-title40-vol17",
    "CFR-2025-title40-vol35",
    "CFR-2025-title7-vol8",
    "CFR-2025-title7-vol11",
    "CFR-2025-title26-vol10",
    "CFR-2025-title40-vol16",
    "CFR-2025-title40-vol3",
)


def host_limits():
    report={"platform":platform.platform(),"python":platform.python_version(),
            "logical_cpu_count":os.cpu_count()}
    if hasattr(os,"sched_getaffinity"):
        report["affinity_cpu_count"]=len(os.sched_getaffinity(0))
    for field,path in (("cpu_max","/sys/fs/cgroup/cpu.max"),
                       ("memory_max","/sys/fs/cgroup/memory.max")):
        try:
            report[field]=Path(path).read_text().strip()
        except OSError:
            report[field]=None
    import fitz
    import pdfplumber
    report.update(pymupdf=fitz.VersionBind,pdfplumber=pdfplumber.__version__)
    return report


def comparable(results):
    return [{k:v for k,v in row.items() if k not in ("seconds","peak_worker_rss_kib")}
            for row in sorted(results,key=lambda r:r["name"])]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",required=True)
    parser.add_argument("--workers",nargs="+",type=int,default=[1,2,4,8])
    args=parser.parse_args()
    if not args.workers or len(set(args.workers))!=len(args.workers) or any(not 1<=n<=16 for n in args.workers):
        parser.error("Select distinct worker counts in 1..16")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    log=logging.getLogger(__name__)
    report={"host":host_limits(),"requested_documents":len(FIXTURES),"runs":[],
            "semantic_accuracy_verified":False,"cloud_calls":False,
            "source_bytes_reused_for_each_configuration":True}
    out=Path(args.report)
    def save():
        temp=out.with_suffix(out.suffix+".tmp")
        temp.write_text(json.dumps(report,indent=2),encoding="utf-8")
        temp.replace(out)
    save()
    with tempfile.TemporaryDirectory(prefix="native-scaling-") as tmp:
        urls=dict(candidates())
        start=time.monotonic()
        items=[download((name,urls[name]),Path(tmp),min_pages=100,
                        max_bytes=32*1024*1024,timeout=40) for name in FIXTURES]
        report["source_acquisition_seconds"]=round(time.monotonic()-start,3)
        report["sources"]=[{k:v for k,v in item.items() if k!="path"} for item in items]
        save()
        if any("path" not in item for item in items) or len({item["sha256"] for item in items})!=len(FIXTURES):
            raise SystemExit("Required scaling corpus unavailable or duplicated")
        baseline=None
        work=partial(extract,max_pages=0,batch_size=16,production_tables=True)
        for workers in args.workers:
            start=time.monotonic()
            def progress(row,n):
                log.info("workers=%d completed=%d/%d name=%s pages=%d errors=%d",
                         workers,n,len(items),row["name"],row["extracted_pages"],len(row["errors"]))
            results=run_extraction(work,items,workers,process_pool=True,on_result=progress)
            measured=performance_summary(results,time.monotonic()-start,full_documents=True)
            current=comparable(results)
            parity=baseline is None or current==baseline
            if baseline is None:
                baseline=current
            row={"workers":workers,"observed_counter_parity":parity,**measured,
                 "peak_worker_rss_kib":max((x["peak_worker_rss_kib"] or 0 for x in results),default=0),
                 "documents":results}
            report["runs"].append(row)
            save()
            print("SCALING_RESULT="+json.dumps({k:v for k,v in row.items() if k!="documents"},sort_keys=True),flush=True)
            if not parity or measured["fully_processed_error_free_documents"]!=len(items):
                raise SystemExit("Native scaling coverage or counter parity failure")
    report["complete"]=True
    save()


if __name__=="__main__":
    main()
