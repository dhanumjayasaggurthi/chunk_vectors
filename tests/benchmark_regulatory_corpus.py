"""Read-only benchmark runner for an AUTHORIZED corpus of real regulatory PDFs.

Unlike synthetic stress tests, this script never creates test documents.
Run against >=100 user-provided PDFs; DOCX must be validated separately
until a native OOXML adapter is implemented. No external API calls are made.
This tests native text/layout/table/image inventory, NOT OCR/chart semantics.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import time
from pathlib import Path


def inspect(path, batch_size):
    from pdf_processor import PDFProcessor
    start = time.monotonic()
    with __import__("fitz").open(str(path)) as doc:
        expected = doc.page_count
    proc = PDFProcessor(str(path), path.stem, path.name,
                        strict=True, capture_rich_text=True)
    pages = 0
    mixed = 0
    styled = 0
    tables = 0
    images = 0
    failures = []
    for window in proc.iter_page_batches(batch_pages=batch_size):
        for page in window.pages:
            pages += 1
            if page.error:
                failures.append({"page": page.page_number, "error": page.error[:300]})
                continue
            has_style = any(bool(el.runs) for el in page.elements)
            styled += int(has_style)
            tables += int(page.has_tables)
            images += int(page.has_images)
            mixed += int(has_style and page.has_tables and page.has_images)
    return {
        "document": path.name,
        "expected_pages": expected,
        "extracted_pages": pages,
        "styled_pages": styled,
        "table_pages": tables,
        "image_pages": images,
        "mixed_pages": mixed,
        "errors": failures,
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--min-documents", type=int, default=100)
    parser.add_argument("--min-pages", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--page-batch-size", type=int, default=16)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if not (1 <= args.workers <= 32 and 1 <= args.page_batch_size <= 128
            and 1 <= args.min_documents <= 100000 and 1 <= args.min_pages <= 25000):
        parser.error("Invalid bounded benchmark settings")
    root = Path(args.corpus).resolve(strict=True)
    if not root.is_dir():
        parser.error("Corpus must be a directory")
    files = sorted(p for p in root.rglob("*") if p.is_file()
                   and p.suffix.lower() == ".pdf")
    docx_count = sum(p.suffix.lower() == ".docx" for p in root.rglob("*") if p.is_file())
    if len(files) < args.min_documents:
        parser.error(f"Need at least {args.min_documents} real PDFs; found {len(files)}")
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda p: inspect(p, args.page_batch_size), files))
    elapsed = time.monotonic() - started
    errors = sum(len(x["errors"]) for x in results)
    pages = sum(x["extracted_pages"] for x in results)
    too_small = sum(x["expected_pages"] < args.min_pages for x in results)
    report = {
        "kind": "real_corpus_native_layout_only",
        "corpus_root": str(root),
        "pdf_documents": len(files),
        "docx_documents_not_tested": docx_count,
        "documents_below_min_pages": too_small,
        "min_pages_per_document": args.min_pages,
        "total_pages": pages,
        "pages_with_tables": sum(x["table_pages"] for x in results),
        "pages_with_images": sum(x["image_pages"] for x in results),
        "pages_with_styled_text": sum(x["styled_pages"] for x in results),
        "pages_with_all_three": sum(x["mixed_pages"] for x in results),
        "page_errors": errors,
        "wall_seconds": round(elapsed, 3),
        "pages_per_second": round(pages / max(elapsed, .001), 3),
        "documents": results,
        "limitations": [
            "No OCR, chart description, cloud translation, embeddings or DB resume tested",
            "No semantic quality scoring without annotated ground truth",
            "No native DOCX adapter yet"
        ],
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "documents"},
                     indent=2))
    if errors or too_small or docx_count:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
