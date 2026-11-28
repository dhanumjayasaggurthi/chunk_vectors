"""Validate and aggregate all independent public regulatory corpus CI shards.

A 100-document claim is only valid when all requested full PDFs have been
parsed, source checksums are unique, and no page was silently dropped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def aggregate(paths: list[Path], *, shards: int, minimum_documents: int) -> dict:
    errors = []
    docs = []
    shard_indices = set()
    reports = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        reports.append(data)
        index = data.get("shard_index")
        if type(index) is not int or index not in range(shards) or index in shard_indices:
            errors.append(f"Invalid or duplicate shard index in {path.name}")
        shard_indices.add(index)
        if data.get("shard_count") != shards:
            errors.append(f"Incompatible shard_count in {path.name}")
        if not data.get("tested_full_documents"):
            errors.append(f"Not a full-document benchmark: {path.name}")
        if data.get("accepted_documents") != len(data.get("documents", [])):
            errors.append(f"Inconsistent document count in {path.name}")
        if data.get("page_errors"):
            errors.append(f"Page errors reported in {path.name}")
        docs.extend(data.get("documents", []))
    if len(reports) != shards or len(shard_indices) != shards:
        errors.append(f"Expected {shards} shard reports, received {len(reports)}")
    if len(docs) < minimum_documents:
        errors.append(f"Expected at least {minimum_documents} full source PDFs, found {len(docs)}")
    hashes = set()
    for row in docs:
        sha = row.get("source_sha256", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            errors.append(f"Invalid source digest for {row.get('name')}")
        if sha in hashes:
            errors.append(f"Duplicate document hash {sha}")
        hashes.add(sha)
        if row.get("errors"):
            errors.append(f"Extraction failed on {row.get('name')}")
        if (not isinstance(row.get("source_pages"), int)
                or row.get("source_pages", 0) < 100
                or row.get("extracted_pages") != row["source_pages"]):
            errors.append(f"Missing source pages for {row.get('name')}")
    summary = {
        "kind": "verified_full_public_regulatory_native_extraction",
        "target_documents": minimum_documents,
        "actual_documents": len(docs),
        "shards": shards,
        "total_source_pages": sum(max(0, x.get("source_pages", 0)) for x in docs),
        "total_processed_pages": sum(max(0, x.get("extracted_pages", 0)) for x in docs),
        "pages_with_styled_text": sum(x.get("style_pages", 0) for x in docs),
        "pages_with_detected_tables": sum(x.get("table_pages", 0) for x in docs),
        "pages_with_detected_images": sum(x.get("image_pages", 0) for x in docs),
        "pages_with_same_page_text_table_image": sum(
            x.get("same_page_text_table_image", 0) for x in docs),
        "page_errors": sum(len(x.get("errors", [])) for x in docs),
        "per_shard": [{
            "shard_index": row.get("shard_index"),
            "documents": row.get("accepted_documents"),
            "wall_seconds": row.get("elapsed_seconds"),
            "peak_worker_rss_kib": row.get("peak_worker_rss_kib"),
        } for row in reports],
        "verified": not errors,
        "verification_errors": errors,
        "limitations": [
            "Native PDF text/style/table detection only; no OCR provider calls",
            "No semantic table/chart accuracy labels, no DOCX, no translation",
            "No DB checkpoint/restart or 25,000-page single-file soak",
        ],
    }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--min-documents", type=int, default=100)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.shards <= 16 or not 1 <= args.min_documents <= 10000:
        parser.error("Invalid aggregation bounds")
    paths = sorted(Path(args.reports).rglob("real_regulatory_shard_*.json"))
    result = aggregate(paths, shards=args.shards, minimum_documents=args.min_documents)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    if not result["verified"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
