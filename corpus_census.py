"""Read-only workload sizing from PDF page trees, not a content-quality check."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
import multiprocessing
from pathlib import Path
import time


def _inspect(path):
    import fitz
    try:
        path=Path(path)
        if path.is_symlink() or not path.is_file():
            return "INVALID_SOURCE",0,0
        size=path.stat().st_size
        if path.suffix.casefold()==".docx":
            return "DOCX",0,size
        if path.suffix.casefold()!=".pdf":
            return "INVALID_SOURCE",0,size
        with fitz.open(path) as pdf:
            if not pdf.is_pdf:
                return "INVALID_PDF",0,size
            if pdf.is_encrypted:
                return "ENCRYPTED_PDF",0,size
            if pdf.is_repaired or pdf.page_count<1:
                return "PDF_REVIEW_REQUIRED",0,size
            return "PDF",pdf.page_count,size
    except Exception as exc:
        return type(exc).__name__,0,0


def census_paths(paths,workers):
    if type(workers) is not int or not 1<=workers<=32:
        raise ValueError("Census workers must be 1..32")
    started=time.monotonic()
    counts=Counter()
    histogram=Counter()
    pages=total_bytes=largest=0
    def collect(results):
        nonlocal pages,total_bytes,largest
        for status,n,size in results:
            counts[status]+=1
            total_bytes+=size
            if status=="PDF":
                pages+=n
                largest=max(largest,n)
                bucket=next((str(limit) for limit in (10,50,100,500,1000,4000) if n<=limit),">4000")
                histogram[bucket]+=1
    if workers==1:
        collect(map(_inspect,paths))
    else:
        # Bounded submission; PyMuPDF handles never cross process boundaries.
        with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context("spawn")) as pool:
            it=iter(paths)
            while window:=list(islice(it,workers*32)):
                collect(pool.map(_inspect,window,chunksize=8))
    total=sum(counts.values())
    errors=total-counts["PDF"]-counts["DOCX"]
    return {"documents":total,"pdf_documents":counts["PDF"],"docx_documents":counts["DOCX"],
            "errors":errors,"statuses":dict(counts),"pdf_pages":pages,
            "average_pdf_pages":round(pages/counts["PDF"],2) if counts["PDF"] else None,
            "max_pdf_pages":largest,"pdf_page_count_upper_bound_histogram":dict(histogram),
            "observed_source_bytes":total_bytes,"elapsed_seconds":round(time.monotonic()-started,3),
            "page_count_complete_for_all_documents":bool(total and not errors and not counts["DOCX"]),
            "writes":False,"cloud_calls":False,"content_quality_verified":False}


def run(d):
    from retention_batch import iter_inventory,iter_source_files
    limit=10_000_000
    if d["inventory"] is not None:
        with d["inventory"].open(encoding="utf-8") as stream:
            return census_paths((row["path"] for row in iter_inventory(stream,{**d["batch"],"max_documents":limit})),d["batch"]["workers"])
    def paths():
        for i,path in enumerate(iter_source_files(d["source"],limit+1)):
            if i==limit:
                raise ValueError("Census exceeds maximum document count")
            yield path
    return census_paths(paths(),d["batch"]["workers"])
