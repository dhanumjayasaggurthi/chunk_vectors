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

HTS_SHA256 = "e52e377f0234c60d5c98947129040a0706958438377868abddeff876cbf56068"
# Body-line and zero-based column indices in the pinned USITC original.
HTS_GOLD = {
    912: ((1, 2, "Live horses, asses, mules and hinnies:"), (9, 0, "0101.30.00"),
          (9, 4, "6.8%1/"), (9, 6, "15%")),
    913: ((1, 2, "Live bovine animals: (con.)"), (4, 0, "0102.29.20"),
          (4, 4, "Free1/"), (4, 6, "6.6¢/kg")),
}


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


def scan_native_rows(path, bounds, source_sha256, page_count):
    """Run the window parser, validated table injection and staged row encoding."""
    from page_stage import _page_row, _tables
    from pdf_processor import PDFProcessor
    from open_grid_recovery import SPACED_FORMAT

    lo, hi = bounds
    started = time.monotonic()
    parser = PDFProcessor(path, source_sha256, Path(path).name, strict=True,
                          capture_rich_text=True, capture_table_structure=True,
                          preserve_uninterpreted_visuals=True, empty_table_policy="preserve_defer",
                          experimental_table_prepass=True, recover_open_grid_body=True)
    skip = set(range(1, lo)) | set(range(hi + 1, page_count + 1))
    details = []
    errors = []
    for window in parser.iter_page_batches(batch_pages=32, completed_pages=skip):
        table_errors = _tables(window.pages, path, {"table_summaries_enabled": False})
        for page in window.pages:
            number = page.page_number
            if page.error or number in table_errors:
                errors.append({"page": number, "stage": "parse" if page.error else "table_injection"})
                details.append({"page": number, "status": "ERROR"})
                continue
            try:
                row = _page_row(page, window, source_sha256, "public-validation")
                bodies = [entry["physical_table_body"] for entry in json.loads(row[9])
                          if "physical_table_body" in entry]
                if any(body["logical_rows_verified"] is not False for body in bodies):
                    raise ValueError("Physical source lines were labeled as logical rows")
                if source_sha256 == HTS_SHA256 and number in HTS_GOLD:
                    body, = bodies
                    for line, column, expected in HTS_GOLD[number]:
                        value = body["physical_cells"][line-1][column]
                        # A superscript marker may be separated by a space.
                        if column != 2:
                            value = "".join(value.split())
                        if value != expected:
                            raise ValueError("USITC physical layout gold cell mismatch")
            except (KeyError, ValueError, RuntimeError) as error:
                errors.append({"page": number, "stage": "row_encoding", "error_type": type(error).__name__})
                details.append({"page": number, "status": "ERROR"})
                continue
            details.append({"page": number, "status": "BODY_IN_ROW" if bodies else "NO_BODY_IN_ROW",
                            "bodies_with_native_spacing": sum(body["format"] == SPACED_FORMAT for body in bodies),
                            "physical_lines": sum(len(body["physical_cells"]) for body in bodies),
                            "nonspace_characters": sum(body["nonspace_characters"] for body in bodies)})
        if len(details) % 256 == 0:
            print(f"worker {lo}-{hi} encoded {len(details)}/{hi-lo+1} pages", flush=True)
    return {"bounds": bounds, "errors": errors, "detail": details,
            "native_open_grid_counts": parser.open_grid_stats, "native_prepass_counts": parser.table_prepass_stats,
            "seconds": time.monotonic() - started}


def main():
    import fitz
    import pdfplumber
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf",required=True)
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--report",required=True)
    parser.add_argument("--native-rows", action="store_true",
                        help="Use strict native extraction and stage row encoding instead of the raw detector")
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
        if args.native_rows:
            futures = [pool.submit(scan_native_rows, str(path), bounds, digest, total) for bounds in ranges]
        else:
            futures = [pool.submit(scan, str(path), bounds) for bounds in ranges]
        rows=[f.result() for f in futures]
    detail=[p for row in rows for p in row["detail"]]
    if [p["page"] for p in detail]!=list(range(1,total+1)):
        raise RuntimeError("Full physical page coverage failed")
    counts=Counter(p["status"] for p in detail)
    with path.open("rb") as f:
        if hashlib.file_digest(f,"sha256").hexdigest()!=digest:
            raise RuntimeError("Original PDF changed during scan")
    report={"kind":"native_parse_and_row_encoding" if args.native_rows else "detector_and_body_probe_only",
            "source_name":path.name,
            "source_sha256":digest,"source_bytes":path.stat().st_size,
            "pages":total,"process_workers":workers,"counts":dict(counts),
            "physical_lines":sum(p.get("physical_lines",0) for p in detail),
            "bodies_with_native_spacing":sum(p.get("bodies_with_native_spacing",0) for p in detail),
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
