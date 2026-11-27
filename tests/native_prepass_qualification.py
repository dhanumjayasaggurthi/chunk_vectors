"""Full-document A/B qualification of the experimental table prepass.

No provider calls, retention, database writes or production settings. Compare
exact native page output digests, not only counts; baseline agreement is not
proof of semantic completeness. Downloads are reused between modes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from collections import Counter
from dataclasses import is_dataclass
from enum import Enum
from functools import partial
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import time

from online_public_regulatory_benchmark import candidates, download, run_extraction
from native_worker_scaling_benchmark import FIXTURES, host_limits


def _encode(value):
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        # Includes staged and dynamically attached source-provenance fields.
        return vars(value)
    raise TypeError(type(value).__name__)


def digest(value):
    data=json.dumps(value, default=_encode, sort_keys=True, ensure_ascii=False,
                    allow_nan=False, separators=(",",":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def extract_document(item, *, experimental):
    from pdf_processor import PDFProcessor
    from table_extractor import inject_table_text_into_elements
    started=time.monotonic()
    proc=PDFProcessor(item["path"],item["sha256"][:24],item["name"],
                      strict=True,capture_rich_text=True,capture_table_structure=True,
                      preserve_uninterpreted_visuals=True,empty_table_policy="preserve_defer",
                      experimental_table_prepass=experimental)
    pages=[]
    toc=None
    errors=[]
    tables=0
    for window in proc.iter_page_batches(batch_pages=16):
        if toc is None:
            toc=digest(window.toc)
        for page in window.pages:
            expected=len(pages)+1
            if page.page_number!=expected:
                raise ValueError("Noncontiguous physical page coverage")
            if page.error:
                errors.append(page.page_number)
            else:
                if page.has_tables:
                    inject_table_text_into_elements(page.elements,page.staged_table_results,strict=True)
                    tables+=1
            pages.append(digest(vars(page)))
    try:
        import resource
        rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        rss=None
    return {"name":item["name"],"source_sha256":item["sha256"],"source_pages":item["pages"],
            "processed_pages":len(pages),"page_digests":pages,"toc_digest":toc,
            "table_pages":tables,"error_pages":errors,
            "prepass_counts":proc.table_prepass_stats,"peak_worker_rss_kib":rss,
            "seconds":round(time.monotonic()-started,3)}


def load_manifest(path, urls):
    """An exact fixture list; no replacement of rejected or changed sources."""
    rows=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows,list) or not 1<=len(rows)<=180:
        raise ValueError("Invalid qualification manifest size")
    names=set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)!={"name","pages"}
                or not isinstance(row["name"],str) or row["name"] not in urls
                or row["name"] in names or type(row["pages"]) is not int
                or not 100<=row["pages"]<=25000):
            raise ValueError("Invalid or duplicate qualification source")
        names.add(row["name"])
    return sorted(rows,key=lambda row:row["name"])


def acquire_manifest(directory, rows, urls):
    def fetch(row):
        return download((row["name"],urls[row["name"]]),directory,min_pages=100,
                        max_bytes=32*1024*1024,timeout=40)
    # Bounded number of HTTP requests, no concurrent parser pools during acquisition.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(fetch,rows))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",required=True)
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--manifest",help="Exact public fixture names and expected original page counts")
    args=parser.parse_args()
    if not 1<=args.workers<=8:
        parser.error("workers must be 1..8")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    import pdfminer
    report={"host":host_limits(),"pdfminer":pdfminer.__version__,
            "workers":args.workers,"runs":[],"semantic_accuracy_verified":False,
            "cloud_calls":False,"complete":False}
    out=Path(args.report)
    def save():
        tmp=out.with_suffix(out.suffix+".tmp")
        tmp.write_text(json.dumps(report,indent=2),encoding="utf-8")
        tmp.replace(out)
    save()
    with tempfile.TemporaryDirectory(prefix="native-prepass-qualification-") as tmp:
        urls=dict(candidates(include_fda=True))
        expected=load_manifest(args.manifest,urls) if args.manifest else None
        requested=expected if expected is not None else [{"name":name} for name in FIXTURES]
        report["requested_documents"]=len(requested)
        report["expected_pages"]=sum(row["pages"] for row in expected) if expected is not None else None
        started=time.monotonic()
        items=acquire_manifest(Path(tmp),requested,urls)
        report["source_acquisition_seconds"]=round(time.monotonic()-started,3)
        report["sources"]=[{k:v for k,v in item.items() if k!="path"} for item in items]
        save()
        if any("path" not in item for item in items) or len({i["sha256"] for i in items})!=len(requested):
            raise SystemExit("Required qualification corpus unavailable or duplicated")
        if expected is not None and any(item["pages"]!=row["pages"] for row,item in zip(expected,items)):
            raise SystemExit("Source page count changed from the original benchmark")
        # These hashes identify the exact bytes used in both modes. Historic
        # manifest counts do not independently establish old-byte identity.
        report["source_bytes_reused_for_each_mode"]=True
        save()
        baseline=None
        for experimental in (False,True):
            started=time.monotonic()
            mode="optimized" if experimental else "baseline"
            progress_path=out.with_suffix(out.suffix+"."+mode+".progress.jsonl")
            with progress_path.open("w",encoding="utf-8") as stream:
                def progress(row,n):
                    stream.write(json.dumps(row,ensure_ascii=False)+"\n")
                    stream.flush()
                    logging.info("prepass=%s completed=%d/%d name=%s pages=%d errors=%d",
                                 experimental,n,len(items),row["name"],row["processed_pages"],len(row["error_pages"]))
                results=run_extraction(partial(extract_document,experimental=experimental),
                                       items,args.workers,process_pool=True,on_result=progress)
            elapsed=time.monotonic()-started
            counts=Counter()
            for row in results:
                counts.update(row["prepass_counts"])
            pages=sum(row["processed_pages"] for row in results)
            coverage=(len(results)==len(items)
                      and all(row["processed_pages"]==row["source_pages"]
                              and not row["error_pages"] for row in results))
            projection=lambda rows:[{k:v for k,v in row.items()
                                     if k not in ("seconds","peak_worker_rss_kib","prepass_counts")}
                                    for row in rows]
            mismatches=[]
            if baseline is None:
                baseline=projection(results)
            else:
                for old,new in zip(baseline,projection(results)):
                    if old!=new:
                        mismatches.append({"name":new["name"],
                            "pages":[i+1 for i,(a,b) in enumerate(zip(old["page_digests"],new["page_digests"])) if a!=b][:50],
                            "toc_mismatch":old["toc_digest"]!=new["toc_digest"]})
            parity=baseline==projection(results)
            summary={"experimental":experimental,"full_error_free_coverage":coverage,
                     "pages":pages,"wall_seconds":round(elapsed,3),
                     "pages_per_second":round(pages/elapsed,3),
                     "exact_baseline_output_parity":parity,"mismatches":mismatches,
                     "prepass_counts":dict(counts),
                     "peak_worker_rss_kib":max((x["peak_worker_rss_kib"] or 0 for x in results),default=0)}
            report["runs"].append({**summary,"documents":results})
            save()
            print("PREPASS_QUALIFICATION="+json.dumps(summary,sort_keys=True),flush=True)
            if not coverage or not parity or (experimental and counts["NEGATIVE_PREPASS"]==0):
                raise SystemExit("Coverage, parity or fast-path activation gate failed")
        report["complete"]=True
        save()


if __name__=="__main__":
    main()
