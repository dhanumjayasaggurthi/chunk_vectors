"""Read-only full original PDF detector and physical-body scan (no cloud or DB)."""
from __future__ import annotations
import argparse
from collections import Counter
import concurrent.futures
import csv
import hashlib
import json
import multiprocessing
from pathlib import Path
import re
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

HTS_SHA256 = "e52e377f0234c60d5c98947129040a0706958438377868abddeff876cbf56068"
HTS_REFERENCE_SHA256 = "f6e95f2d39ef07c84fd3f5ffe987b1bab9b271d50638655eb6083d09355336ed"
# Body-line and zero-based column indices in the pinned USITC original.
HTS_GOLD = {
    912: ((1, 2, "Live horses, asses, mules and hinnies:"), (9, 0, "0101.30.00"),
          (9, 4, "6.8%1/"), (9, 6, "15%")),
    913: ((1, 2, "Live bovine animals: (con.)"), (4, 0, "0102.29.20"),
          (4, 4, "Free1/"), (4, 6, "6.6¢/kg")),
}
# Manually reviewed physical line spans. The sampled body pages identify
# themselves as Revision 2 even though the PDF cover says Revision 1.
HTS_REFERENCE_SPANS = {
    912: (("0101.30.00.00", 9, 12),),
    913: (("0102.29.20", 4, 4), ("0102.29.20.11", 5, 6)),
    914: (("0102.39.00", 6, 9), ("0102.39.00.10", 10, 11), ("0102.90.00.00", 43, 46)),
    1050: (("0602.10.00.00", 3, 6), ("0602.20.00.00", 7, 8),
           ("0602.90.30", 18, 22), ("0602.90.30.10", 23, 23)),
    2600: (("7228.10.00.30", 7, 8), ("7228.20.10.00", 12, 12), ("7228.40.00.00", 32, 32)),
}
HTS_REFERENCE_FIELDS = (
    "HTS Number", "Description", "Unit of Quantity", "General Rate of Duty",
    "Special Rate of Duty", "Column 2 Rate of Duty",
)


def read_hts_reference(path):
    wanted = {code for cases in HTS_REFERENCE_SPANS.values() for code, _, _ in cases}
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != HTS_REFERENCE_SHA256:
            raise ValueError("Pinned USITC Revision 2 CSV hash mismatch")
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row["HTS Number"]
            if code in wanted:
                if code in result:
                    raise ValueError(f"Duplicate USITC reference code {code}")
                result[code] = tuple(row[field] for field in HTS_REFERENCE_FIELDS)
    if set(result) != wanted:
        raise ValueError("Missing USITC reference codes")
    return result


def verify_hts_reference(page_number, lines, reference):
    """Check explicit spans against the official CSV without projecting rows."""
    def column(span, index):
        return " ".join(" ".join(re.sub(r"\.{2,}$", "", line[index]).split())
                        for line in span if line[index]).strip()

    def unit(text):
        return re.sub(r"\bNo\.(?=\s|$)", "No", text)

    def rate(text):
        # The CSV omits superscript footnote references and comma spacing.
        return re.sub(r",\s+", ",", re.sub(r"\s+\d+(?:/\d+)*/$", "", text))

    checked = []
    for code, first, last in HTS_REFERENCE_SPANS[page_number]:
        span = lines[first - 1:last]
        if (len(span) != last - first + 1 or any(len(line) != 7 for line in span)
                or any(line[0] or line[1] for line in span[1:])):
            raise ValueError(f"Invalid USITC reference span for {code}")
        prefix, suffix = span[0][:2]
        if not prefix and suffix:
            prefix = next((line[0] for line in reversed(lines[:first - 1]) if line[0]), "")
        observed_code = prefix + ("." + suffix if suffix else "")
        values = (observed_code, column(span, 2), unit(column(span, 3)),
                  rate(column(span, 4)), rate(column(span, 5)), rate(column(span, 6)))
        ref_code, ref_description, ref_units, *ref_rates = reference[code]
        expected = (ref_code, ref_description, unit(" ".join(json.loads(ref_units or "[]"))),
                    *(rate(value) for value in ref_rates))
        if values != expected:
            differences = [field for field, got, want in zip(HTS_REFERENCE_FIELDS, values, expected)
                           if got != want]
            raise ValueError(f"USITC reference mismatch on physical page {page_number}, {code}: {differences}")
        checked.append({"code": code, "physical_page": page_number, "physical_lines": [first, last],
                        "matched_fields": len(HTS_REFERENCE_FIELDS)})
    return checked


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


def scan_native_rows(path, bounds, source_sha256, page_count, reference=None):
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
    checked = []
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
                if reference is not None and number in HTS_REFERENCE_SPANS:
                    if len(bodies) != 1 or "Revision 2 (2026)" not in page.raw_text:
                        raise ValueError("Missing Revision 2 body on pinned USITC reference page")
                    checked.extend(verify_hts_reference(number, bodies[0]["physical_cells"], reference))
            except (KeyError, ValueError, RuntimeError) as error:
                failure = {"page": number, "stage": "row_encoding", "error_type": type(error).__name__}
                if source_sha256 == HTS_SHA256:
                    failure["message"] = str(error)
                errors.append(failure)
                details.append({"page": number, "status": "ERROR"})
                continue
            details.append({"page": number, "status": "BODY_IN_ROW" if bodies else "NO_BODY_IN_ROW",
                            "bodies_with_native_spacing": sum(body["format"] == SPACED_FORMAT for body in bodies),
                            "physical_lines": sum(len(body["physical_cells"]) for body in bodies),
                            "nonspace_characters": sum(body["nonspace_characters"] for body in bodies)})
        if len(details) % 256 == 0:
            print(f"worker {lo}-{hi} encoded {len(details)}/{hi-lo+1} pages", flush=True)
    return {"bounds": bounds, "errors": errors, "detail": details, "hts_reference_cases": checked,
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
    parser.add_argument("--reference-csv", type=Path,
                        help="Pinned USITC Revision 2 reference for the sampled physical spans")
    args=parser.parse_args()
    if not 1<=args.workers<=8:
        parser.error("workers must be 1..8")
    path=Path(args.pdf).resolve(strict=True)
    with path.open("rb") as f:
        digest=hashlib.file_digest(f,"sha256").hexdigest()
    if args.reference_csv and (not args.native_rows or digest != HTS_SHA256):
        parser.error("--reference-csv requires the pinned USITC PDF and --native-rows")
    reference = read_hts_reference(args.reference_csv) if args.reference_csv else None
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
            futures = [pool.submit(scan_native_rows, str(path), bounds, digest, total, reference)
                       for bounds in ranges]
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
    if reference is not None:
        cases = [case for row in rows for case in row["hts_reference_cases"]]
        report["hts_reference_audit"] = {
            "csv_sha256": HTS_REFERENCE_SHA256, "csv_revision": 2,
            "checked_records": len(cases), "matched_fields": sum(case["matched_fields"] for case in cases),
            "cases": cases,
        }
    Path(args.report).write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("FULL_OPEN_GRID="+json.dumps({k:v for k,v in report.items() if k!="workers"}))
    if counts["ERROR"] or (reference is not None and len(cases) != len(reference)):
        raise SystemExit(2)


if __name__=="__main__":
    main()
