"""Bounded public regulatory PDF download + native extraction benchmark.

Sources are official CFR volumes hosted by the U.S. Government Publishing
Office, plus selected FDA reports. A successful URL response is not assumed
to be a valid PDF; each file is SHA-256 checked and parsed before inclusion.
No paid OCR, translation or embedding requests. Only public, approved URLs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import multiprocessing
from functools import partial
import logging
import math
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOG = logging.getLogger("online_regulatory_benchmark")
# GovInfo PDF edition / volume URL layout is documented by govinfo.gov.
CFR_VOLUMES = [
    (21, 9), (40, 40), (26, 20), (7, 15), (12, 12),
    (29, 10), (50, 15), (46, 10), (42, 6), (49, 12),
    (48, 8), (17, 4), (32, 7), (10, 6), (14, 5),
]
FDA_REPORTS = [
    "https://www.fda.gov/media/187276/download",
    "https://www.fda.gov/media/190779/download?attachment=",
    "https://nvlpubs.nist.gov/nistpubs/ir/2024/NIST.IR.8524.pdf",
    "https://www.gao.gov/assets/gao-25-107743.pdf",
]
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_ALLOWED_HOSTS = {"www.govinfo.gov", "govinfo.gov", "www.fda.gov", "fda.gov",
                  "nvlpubs.nist.gov", "www.gao.gov", "gao.gov", "learning.usitc.gov"}


def candidates(*, include_fda=False):
    if include_fda:
        for idx, url in enumerate(FDA_REPORTS):
            yield ("FDA-" + str(idx + 1), url)
    for title, volumes in CFR_VOLUMES:
        for vol in range(1, volumes + 1):
            name = f"CFR-2025-title{title}-vol{vol}"
            yield (name, f"https://www.govinfo.gov/content/pkg/{name}/pdf/{name}.pdf")


def download(item, directory, *, min_pages, max_bytes, timeout):
    """Stream bounded files to a private temp dir; report rejected responses."""
    import fitz
    import requests
    name, url = item
    if urlsplit(url).hostname not in _ALLOWED_HOSTS:
        return {"name": name, "url": url, "error": "Unexpected source host"}
    target = directory / (name + ".pdf")
    error = ""
    for attempt in range(3):
        try:
            digest = hashlib.sha256()
            size = 0
            with requests.get(url, stream=True, timeout=(10, timeout),
                              headers={"User-Agent": "PublicRegulatoryExtractionBenchmark/1.0"}) as response:
                response.raise_for_status()
                if urlsplit(response.url).hostname not in _ALLOWED_HOSTS:
                    raise ValueError("Unexpected redirected download host")
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length > max_bytes:
                    raise ValueError("Download too large")
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError("Download byte budget exceeded")
                            digest.update(chunk)
                            handle.write(chunk)
            with target.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise ValueError("HTTP response not a PDF")
            with fitz.open(target) as pdf:
                if pdf.is_encrypted:
                    raise ValueError("Encrypted PDF")
                page_count = pdf.page_count
            if page_count < min_pages:
                target.unlink(missing_ok=True)
                return {"name": name, "url": url, "error": "Below minimum pages",
                        "pages": page_count}
            return {"name": name, "url": url, "path": str(target),
                    "sha256": digest.hexdigest(), "bytes": size,
                    "pages": page_count}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:250]
            target.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(min(2 ** attempt, 4))
    return {"name": name, "url": url, "error": error}


def extract(item, *, max_pages, batch_size, production_tables=False):
    """Full-document extraction unless max_pages imposes an explicit smoke cap."""
    from pdf_processor import PDFProcessor
    start = time.monotonic()
    options=dict(strict=True,capture_rich_text=True)
    if production_tables:
        options.update(capture_table_structure=True,preserve_uninterpreted_visuals=True,
                       empty_table_policy="preserve_defer")
    proc = PDFProcessor(item["path"], item["sha256"][:24], item["name"],**options)
    processed = style_pages = table_pages = image_pages = mixed_pages = 0
    bold_runs = italic_runs = 0
    table_fragments=source_rows=continuation_candidates=0
    tariff_header_pages=header_only_candidates=0
    header_only_examples=[]
    prior_page=prior_table=None
    if production_tables:
        from table_extractor import inject_table_text_into_elements
        from table_continuity import _candidate
    errors = []
    # Process the full PDF for production evidence; use a configurable page
    # cap only for deliberately labeled fast-smoke runs.
    for window in proc.iter_page_batches(batch_pages=batch_size):
        for page in window.pages:
            if max_pages and processed >= max_pages:
                break
            processed += 1
            if processed%500==0:
                LOG.info("Native benchmark pages attempted=%s/%s elapsed=%.2fs errors=%s",
                         processed,item["pages"],time.monotonic()-start,len(errors))
            if page.error:
                errors.append({"page": page.page_number, "error": page.error[:250]})
                continue
            if production_tables:
                try:
                    if page.has_tables:
                        inject_table_text_into_elements(page.elements,page.staged_table_results,strict=True)
                    fragments=[el for el in page.elements if getattr(el,"source_table",None)]
                    if fragments and prior_table is not None and page.page_number==prior_page.page_number+1:
                        continuation_candidates+=int(_candidate(prior_page,prior_table,page,fragments[0],
                                                                edge_fraction=.25,x_tolerance=.05))
                    prior_page,prior_table=page,fragments[-1] if fragments else None
                    table_fragments+=len(fragments)
                    source_rows+=sum(len(el.source_table["source_cells"]) for el in fragments)
                    # Fixture-specific diagnostic, not production classification.
                    # HTS pages use vertical rules without horizontal body rows.
                    native=" ".join(page.raw_text.split()).casefold()
                    if ("rates of duty" in native and "article description" in native
                            and ("stat." in native or "unit of quantity" in native)):
                        tariff_header_pages+=1
                        if len(native)>500 and not any(el.bbox.y1>page.height*.25 for el in fragments):
                            header_only_candidates+=1
                            if len(header_only_examples)<20:
                                header_only_examples.append(page.page_number)
                except Exception as exc:
                    prior_page=prior_table=None
                    errors.append({"page":page.page_number,"error":str(exc)[:250]})
                    continue
            text_runs = [run for element in page.elements
                         for run in getattr(element, "runs", [])]
            style_pages += int(bool(text_runs))
            table_pages += int(page.has_tables)
            image_pages += int(page.has_images)
            mixed_pages += int(bool(text_runs) and page.has_tables and page.has_images)
            bold_runs += sum(bool(r.get("bold")) for r in text_runs)
            italic_runs += sum(bool(r.get("italic")) for r in text_runs)
        if max_pages and processed >= max_pages:
            break
    try:
        import resource
        peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        peak_rss_kib = None
    return {
        "name": item["name"], "source_url": item["url"],
        "peak_worker_rss_kib": peak_rss_kib,
        "source_sha256": item["sha256"], "bytes": item["bytes"],
        "source_pages": item["pages"], "extracted_pages": processed,
        "style_pages": style_pages, "table_pages": table_pages,
        "production_table_validation":production_tables,
        "structured_table_fragments":table_fragments,"source_table_rows":source_rows,
        "adjacent_continuation_candidates":continuation_candidates,
        "tariff_header_pages":tariff_header_pages,
        "suspected_header_only_table_pages":header_only_candidates,
        "suspected_header_only_examples":header_only_examples,
        "image_pages": image_pages, "same_page_text_table_image": mixed_pages,
        "bold_runs": bold_runs, "italic_runs": italic_runs,
        "errors": errors, "seconds": round(time.monotonic() - start, 3),
    }


def gather_downloads(directory, *, target_documents, max_candidates,
                     min_pages, max_bytes, download_workers, timeout,
                     include_fda, shard_index=0, shard_count=1):
    """Keep only bounded pending downloads; stop once target unique PDFs pass."""
    universe = list(candidates(include_fda=include_fda))[:max_candidates]
    items = [item for idx, item in enumerate(universe)
             if idx % shard_count == shard_index]
    accepted = []
    skipped = Counter()
    rejected = []
    seen_sha = set()
    # This is intentionally kept smaller than per-host bulk crawl concurrency.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=download_workers)
    try:
        pending = {}
        itr = iter(items)
        while len(accepted) < target_documents:
            while len(pending) < download_workers * 2:
                try:
                    item = next(itr)
                except StopIteration:
                    break
                future = pool.submit(download, item, directory, min_pages=min_pages,
                                     max_bytes=max_bytes, timeout=timeout)
                pending[future] = item
            if not pending:
                break
            done, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                name, url = pending.pop(future)
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"name": name, "url": url,
                           "error": f"Unexpected: {type(exc).__name__}: {exc}"[:250]}
                if "path" in row:
                    if row["sha256"] in seen_sha:
                        skipped["duplicate_sha256"] += 1
                        Path(row["path"]).unlink(missing_ok=True)
                    elif len(accepted) < target_documents:
                        seen_sha.add(row["sha256"])
                        accepted.append(row)
                        LOG.info("Accepted %d/%d: %s (%d pages)",
                                 len(accepted), target_documents, name, row["pages"])
                    else:
                        Path(row["path"]).unlink(missing_ok=True)
                else:
                    reason = row.get("error", "unknown")
                    skipped[reason.split(":")[0]] += 1
                    if len(rejected) < 250:
                        rejected.append(row)
        for future in pending:
            future.cancel()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return accepted, {"rejections_by_reason": dict(skipped),
                      "rejected_sample": rejected}


def performance_summary(results, seconds, *, full_documents):
    """Separate successful native pages from attempted pages and source quality."""
    attempted=sum(r["extracted_pages"] for r in results)
    page_errors=sum(len(r["errors"]) for r in results)
    success_pages=attempted-page_errors
    complete=[r for r in results if not r["errors"] and r["extracted_pages"]==r["source_pages"]]
    durations=sorted(r["seconds"] for r in results)
    def percentile(p):
        return durations[max(0,math.ceil(p*len(durations))-1)] if durations else None
    return {"native_extraction_wall_seconds":round(seconds,3),
            "successful_native_pages":success_pages,
            "fully_processed_error_free_documents":len(complete),
            "native_success_pages_per_second":round(success_pages/seconds,3) if seconds>0 else None,
            "complete_documents_per_minute":round(60*len(complete)/seconds,3) if seconds>0 and full_documents else None,
            "document_latency_p50_seconds":percentile(.50),
            "document_latency_p95_seconds":percentile(.95),
            "document_latency_max_seconds":max(durations,default=None),
            "max_source_pages":max((r["source_pages"] for r in results),default=0),
            "structured_table_fragments":sum(r["structured_table_fragments"] for r in results),
            "detected_source_table_rows":sum(r["source_table_rows"] for r in results),
            "suspected_header_only_table_pages":sum(r["suspected_header_only_table_pages"] for r in results),
            "native_semantic_accuracy_verified":False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=5)
    parser.add_argument("--min-pages", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=24,
                        help="0=full PDF; nonzero=smoke sample, not full document")
    parser.add_argument("--max-candidates", type=int, default=190)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-download-mb", type=int, default=32)
    parser.add_argument("--download-timeout", type=int, default=40)
    parser.add_argument("--include-fda", action="store_true")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--process-pool", action="store_true",
                        help="Spawn isolated PDF worker processes instead of threads")
    parser.add_argument("--local-pdf", action="append", default=[],
                        help="Use original local approved public PDF(s), no download")
    parser.add_argument("--production-tables", action="store_true",
                        help="Use strict production native table preservation for online PDFs too")
    parser.add_argument("--large-hts", action="store_true",
                        help="Full original USITC tariff PDF, at least 4001 pages")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not (1 <= args.documents <= 180
            and 1 <= args.min_pages <= 5000 and 0 <= args.max_pages <= 25000
            and 1 <= args.workers <= 16 and 1 <= args.download_workers <= 6
            and 1 <= args.batch_size <= 32 and 1 <= args.max_download_mb <= 256
            and 1 <= args.download_timeout <= 120
            and 1 <= args.max_candidates <= 190
            and 1 <= args.shard_count <= 16
            and 0 <= args.shard_index < args.shard_count):
        parser.error("Invalid bounded online benchmark settings")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="regulatory-online-") as temp:
        if args.large_hts and (args.documents!=1 or args.max_pages!=0 or args.min_pages<4001):
            parser.error("Large HTS requires --documents 1 --min-pages 4001 --max-pages 0")
        if args.local_pdf:
            import fitz
            if len(args.local_pdf)!=args.documents:
                parser.error("--documents must equal the number of --local-pdf arguments")
            accepted=[]
            for name in args.local_pdf:
                path=Path(name).resolve(strict=True)
                if path.stat().st_size>args.max_download_mb*1024*1024:
                    parser.error("Local PDF exceeds configured byte limit")
                with fitz.open(path) as pdf:
                    if pdf.is_encrypted or pdf.page_count<args.min_pages:
                        parser.error("Local PDF encrypted or below required page count")
                    count=pdf.page_count
                sha=hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda:stream.read(1024*1024),b""):
                        sha.update(block)
                accepted.append({"name":path.name,"url":None,"path":str(path),
                                 "pages":count,"bytes":path.stat().st_size,"sha256":sha.hexdigest()})
            rejected={"rejected_sample":[],"rejections_by_reason":{}}
        elif args.large_hts:
            item=download(("USITC-HTS",
                "https://learning.usitc.gov/hts-docs/documents/finalCopy.pdf"),
                Path(temp),min_pages=args.min_pages,
                max_bytes=args.max_download_mb*1024*1024,timeout=args.download_timeout)
            accepted=[item] if "path" in item else []
            rejected={"rejected_sample":[] if accepted else [item],
                      "rejections_by_reason":{} if accepted else {"large_fixture_rejected":1}}
        else:
            accepted, rejected = gather_downloads(
            Path(temp), target_documents=args.documents,
            max_candidates=args.max_candidates, min_pages=args.min_pages,
            max_bytes=args.max_download_mb * 1024 * 1024,
            download_workers=args.download_workers, timeout=args.download_timeout,
            include_fda=args.include_fda, shard_index=args.shard_index,
            shard_count=args.shard_count)
        if len({r["sha256"] for r in accepted})!=len(accepted):
            parser.error("Benchmark inputs must have distinct source hashes")
        LOG.info("Accepted %d unique original PDFs", len(accepted))
        acquisition_seconds=time.monotonic()-started
        extraction_started=time.monotonic()
        # Processes isolate PDF native caches/GIL for CPU-bound native parsing;
        # worker recycling caps long-lived allocator growth on giant corpora.
        if args.process_pool:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=args.workers,
                mp_context=multiprocessing.get_context("spawn"),
                max_tasks_per_child=8,
            )
        else:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
        with executor:
            work=partial(extract, max_pages=args.max_pages,
                         batch_size=args.batch_size,production_tables=bool(args.production_tables or args.large_hts or args.local_pdf))
            results=list(executor.map(work, accepted, chunksize=1))
        report = {
            "kind": "public_usitc_large_native_extraction" if args.large_hts else ("local_public_native_extraction" if args.local_pdf else "public_official_cfr_fda_native_extraction"),
            "tested_full_documents": args.max_pages == 0,
            "production_table_validation":bool(args.production_tables or args.large_hts or args.local_pdf),
            "source_acquisition_seconds":round(acquisition_seconds,3),
            "performance":performance_summary(results,time.monotonic()-extraction_started,full_documents=args.max_pages==0),
            "requested_documents": args.documents,
            "shard_index": args.shard_index,
            "shard_count": args.shard_count,
            "isolated_process_workers": args.process_pool,
            "worker_count": args.workers,
            "peak_worker_rss_kib": max(
                (x.get("peak_worker_rss_kib") or 0 for x in results),default=0),
            "accepted_documents": len(accepted),
            "min_source_pages": args.min_pages,
            "sample_page_limit_per_document": args.max_pages,
            "total_source_pages": sum(x["pages"] for x in accepted),
            "total_extracted_pages": sum(x["extracted_pages"] for x in results),
            "styled_pages": sum(x["style_pages"] for x in results),
            "table_pages": sum(x["table_pages"] for x in results),
            "image_pages": sum(x["image_pages"] for x in results),
            "pages_with_same_page_text_table_image": sum(
                x["same_page_text_table_image"] for x in results),
            "bold_runs": sum(x["bold_runs"] for x in results),
            "italic_runs": sum(x["italic_runs"] for x in results),
            "page_errors": sum(len(x["errors"]) for x in results),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "documents": results,
            "rejected_sources": rejected,
            "limitations": [
                "No paid OCR, image-to-text recognition, chart interpretation, translation or embeddings",
                "No semantic gold-label scoring; observed content counts are not accuracy measures",
                "No PostgreSQL resumability in this benchmark",
                "A nonzero max-pages samples each document rather than processing it in full"
            ],
        }
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        if rejected["rejected_sample"]:
            print("REJECTION_SAMPLES=" + json.dumps(rejected["rejected_sample"][:8]))
        print("BENCHMARK_SUMMARY=" + json.dumps(
            {k: v for k, v in report.items()
             if k not in ("documents", "rejected_sources", "limitations")},
            sort_keys=True))
        if (len(accepted) < args.documents or report["page_errors"]
                or (args.max_pages==0 and report["performance"]["fully_processed_error_free_documents"]!=args.documents)
                or (args.large_hts and any(item["suspected_header_only_table_pages"] for item in results))):
            raise SystemExit(2)


if __name__ == "__main__":
    main()
