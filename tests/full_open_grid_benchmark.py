"""Read-only full original PDF detector and physical-body scan (no cloud or DB)."""
from __future__ import annotations
import argparse
from collections import Counter
import concurrent.futures
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def scan(path,bounds):
    import fitz
    import pdfplumber
    from open_grid_recovery import recover_open_grid
    from config import TABLE_SETTINGS
    lo,hi=bounds
    started=time.monotonic()
    counts=Counter()
    details=[]
    errors=[]
    with pdfplumber.open(path,pages=list(range(lo,hi+1))) as doc,fitz.open(path) as native:
        for page in doc.pages:
            n=page.page_number
            try:
                tables=page.find_tables(TABLE_SETTINGS)
                fp=native[n-1]
                if fp.rotation or fp.cropbox!=fp.mediabox:
                    status,body="UNSUPPORTED_GEOMETRY",None
                elif not tables:
                    status,body="NO_TABLE",None
                else:
                    status,body=recover_open_grid(page,tables)
                details.append({"page":n,"status":status,
                                "physical_lines":len(body["physical_cells"]) if body else 0,
                                "nonspace_characters":body["nonspace_characters"] if body else 0})
            except Exception as exc:
                status="ERROR"
                errors.append({"page":n,"error_type":type(exc).__name__})
                details.append({"page":n,"status":status})
            finally:
                page.close()
            counts[status]+=1
            if (n-lo+1)%200==0:
                print(f"worker {lo}-{hi} completed {n-lo+1}/{hi-lo+1}",flush=True)
    return {"bounds":bounds,"counts":dict(counts),"errors":errors,
            "detail":details,"seconds":time.monotonic()-started}


def main():
    import fitz
    import pdfplumber
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf",required=True)
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--report",required=True)
    args=parser.parse_args()
    if not 1<=args.workers<=8:
        parser.error("workers must be 1..8")
    path=Path(args.pdf).resolve(strict=True)
    with path.open("rb") as f:
        digest=hashlib.file_digest(f,"sha256").hexdigest()
    with fitz.open(path) as doc:
        total=doc.page_count
        if not total or doc.is_encrypted:
            raise ValueError("Invalid original PDF")
    workers=min(total,args.workers)
    ranges=[(1+i*total//workers,(i+1)*total//workers) for i in range(workers)]
    started=time.monotonic()
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,
            mp_context=multiprocessing.get_context("spawn")) as pool:
        futures=[pool.submit(scan,str(path),bounds) for bounds in ranges]
        rows=[f.result() for f in futures]
    detail=[p for row in rows for p in row["detail"]]
    if [p["page"] for p in detail]!=list(range(1,total+1)):
        raise RuntimeError("Full physical page coverage failed")
    counts=Counter(p["status"] for p in detail)
    with path.open("rb") as f:
        if hashlib.file_digest(f,"sha256").hexdigest()!=digest:
            raise RuntimeError("Original PDF changed during scan")
    report={"kind":"detector_and_body_probe_only","source_name":path.name,
            "source_sha256":digest,"source_bytes":path.stat().st_size,
            "pages":total,"process_workers":workers,"counts":dict(counts),
            "physical_lines":sum(p.get("physical_lines",0) for p in detail),
            "nonspace_characters":sum(p.get("nonspace_characters",0) for p in detail),
            "seconds":round(time.monotonic()-started,3),"pdfplumber":pdfplumber.__version__,
            "pymupdf":fitz.VersionBind,"logical_rows_verified":False,
            "native_pipeline_benchmark":False,"workers":rows}
    Path(args.report).write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("FULL_OPEN_GRID="+json.dumps({k:v for k,v in report.items() if k!="workers"}))
    if counts["ERROR"]:
        raise SystemExit(2)


if __name__=="__main__":
    main()
