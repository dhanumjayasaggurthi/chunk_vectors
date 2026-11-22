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
import logging
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
]
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_ALLOWED_HOSTS = {"www.govinfo.gov", "govinfo.gov", "www.fda.gov", "fda.gov"}


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


def extract(item, *, max_pages, batch_size):
    """Full-document extraction unless max_pages imposes an explicit smoke cap."""
    from pdf_processor import PDFProcessor
    start = time.monotonic()
    proc = PDFProcessor(item["path"], item["sha256"][:24], item["name"],
                        strict=True, capture_rich_text=True)
    processed = style_pages = table_pages = image_pages = mixed_pages = 0
    bold_runs = italic_runs = 0
    errors = []
    # Process the full PDF for production evidence; use a configurable page
    # cap only for deliberately labeled fast-smoke runs.
    for window in proc.iter_page_batches(batch_pages=batch_size):
        for page in window.pages:
            if max_pages and processed >= max_pages:
                break
            processed += 1
            if page.error:
                errors.append({"page": page.page_number, "error": page.error[:250]})
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
    return {
        "name": item["name"], "source_url": item["url"],
        "source_sha256": item["sha256"], "bytes": item["bytes"],
        "source_pages": item["pages"], "extracted_pages": processed,
        "style_pages": style_pages, "table_pages": table_pages,
        "image_pages": image_pages, "same_page_text_table_image": mixed_pages,
        "bold_runs": bold_runs, "italic_runs": italic_runs,
        "errors": errors, "seconds": round(time.monotonic() - start, 3),
    }


def gather_downloads(directory, *, target_documents, max_candidates,
                     min_pages, max_bytes, download_workers, timeout,
                     include_fda):
    """Keep only bounded pending downloads; stop once target unique PDFs pass."""
    items = list(candidates(include_fda=include_fda))[:max_candidates]
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
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not (1 <= args.documents <= 180
            and 1 <= args.min_pages <= 5000 and 0 <= args.max_pages <= 25000
            and 1 <= args.workers <= 16 and 1 <= args.download_workers <= 6
            and 1 <= args.batch_size <= 32 and 1 <= args.max_download_mb <= 64
            and 1 <= args.download_timeout <= 120
            and 1 <= args.max_candidates <= 190):
        parser.error("Invalid bounded online benchmark settings")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="regulatory-online-") as temp:
        accepted, rejected = gather_downloads(
            Path(temp), target_documents=args.documents,
            max_candidates=args.max_candidates, min_pages=args.min_pages,
            max_bytes=args.max_download_mb * 1024 * 1024,
            download_workers=args.download_workers, timeout=args.download_timeout,
            include_fda=args.include_fda)
        LOG.info("Downloaded %d unique official PDFs", len(accepted))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            results = list(executor.map(
                lambda item: extract(item, max_pages=args.max_pages,
                                     batch_size=args.batch_size), accepted))
        report = {
            "kind": "public_official_cfr_fda_native_extraction",
            "tested_full_documents": args.max_pages == 0,
            "requested_documents": args.documents,
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
        print("BENCHMARK_SUMMARY=" + json.dumps(
            {k: v for k, v in report.items()
             if k not in ("documents", "rejected_sources", "limitations")},
            sort_keys=True))
        if len(accepted) < args.documents or report["page_errors"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
