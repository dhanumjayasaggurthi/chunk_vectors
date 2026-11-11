"""Deliberately separate ingestion-v2 pilot CLI; production main.py is untouched.

Examples:
  python -m ingestion_v2.cli plan --pdf sample.pdf
  python -m ingestion_v2.cli extract --pdf sample.pdf --output /tmp/v2-pilot --max-ranges 2
  python -m ingestion_v2.cli chunk --artifacts /tmp/v2-pilot/JOB_KEY --output out.json.gz

'chunk' requires a COMPLETE native extraction with no pending OCR pages.
No command writes to PostgreSQL or calls Cloud Vision.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .extract import pdf_page_count, run_local, _write_json_gzip_atomic
from .plan import ExtractionPolicy, page_ranges


def _policy(path, section, cls):
    if not path:
        return cls()
    settings=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(settings,dict) or not isinstance(settings.get(section),dict):
        raise ValueError(f"Profile must contain a {section} object")
    return cls(**settings[section])


def load_complete_pages(artifacts):
    """Reject gaps, obsolete artifacts, and OCR-required pages before chunking."""
    folder=Path(artifacts)
    with gzip.open(folder/"manifest.json.gz","rt",encoding="utf-8") as f:
        manifest=json.load(f)
    count=int(manifest["page_count"])
    planned=page_ranges(count,int(manifest["range_pages"]))
    if len(planned)!=int(manifest["selected_ranges"]):
        raise ValueError("Pilot only extracted selected ranges; cannot publish incomplete document")
    pages=[]
    for start,end in planned:
        path=folder/f"pages-{start+1:07d}-{end:07d}.json.gz"
        with gzip.open(path,"rt",encoding="utf-8") as f:
            obj=json.load(f)
        if (obj["job_key"]!=manifest["job_key"] or
            obj["source_sha256"]!=manifest["source_sha256"] or
            obj["range_start"]!=start or obj["range_end"]!=end):
            raise ValueError("Artifact/source version mismatch")
        pages.extend(obj["pages"])
    if [p["page"] for p in pages]!=list(range(1,count+1)):
        raise ValueError("Extraction has missing/duplicate pages")
    unresolved=[p["page"] for p in pages if p.get("ocr_candidate")]
    if unresolved:
        raise ValueError(
            f"{len(unresolved)} OCR-candidate pages remain (first: {unresolved[:5]}). "
            "Cannot publish native-only chunks; route these pages to the "
            "separate GCP OCR stage after applying project quotas."
        )
    return manifest,pages


def chunk_artifacts(artifacts,output,chunk_policy):
    from .chunking import LayoutElement, chunk_elements
    manifest,pages=load_complete_pages(artifacts)
    elements=[]
    for p in pages:
        for b in p["text_blocks"]:
            elements.append(LayoutElement(
                element_id=b["element_id"],page=p["page"],kind=b["kind"],
                text=b["text"],bbox=tuple(b["bbox"]),
                headings=tuple(b.get("headings") or ()),
                continuation_of=b.get("continuation_of"),
            ))
    if not elements:
        raise ValueError("No native text found; refuse empty chunk export")
    profile_key=json.dumps(asdict(chunk_policy),sort_keys=True)
    chunks=chunk_elements(manifest["source_sha256"],elements,chunk_policy,
                          profile_id=profile_key)
    payload={
        "schema_version":1,"source_sha256":manifest["source_sha256"],
        "extraction_job_key":manifest["job_key"],"chunk_policy":asdict(chunk_policy),
        "chunks":[asdict(chunk) for chunk in chunks],
    }
    _write_json_gzip_atomic(output,payload)
    return {"chunks":len(chunks),"source_pages":len(pages),"output":str(output)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    cmds=parser.add_subparsers(dest="command",required=True)
    plan=cmds.add_parser("plan",help="Read-only PDF page-range planning")
    plan.add_argument("--pdf",required=True)
    plan.add_argument("--range-pages",type=int,default=32)
    extract=cmds.add_parser("extract",help="Opt-in local multiprocessing native-PDF pilot")
    extract.add_argument("--pdf",required=True)
    extract.add_argument("--output",required=True)
    extract.add_argument("--profile")
    extract.add_argument("--max-ranges",type=int,default=2,
                         help="Pilot safety cap (default 2). Set 0 for all pages")
    chunk=cmds.add_parser("chunk",help="Chunk complete native-text artifact only")
    chunk.add_argument("--artifacts",required=True)
    chunk.add_argument("--output",required=True)
    chunk.add_argument("--profile")
    args=parser.parse_args(argv)
    if args.command=="plan":
        n=pdf_page_count(args.pdf)
        p=page_ranges(n,args.range_pages)
        result={"pages":n,"range_pages":args.range_pages,
                "range_tasks":len(p),"first_ranges":p[:5],
                "note":"Read-only; zero GCP requests and no DB writes"}
    elif args.command=="extract":
        p=_policy(args.profile,"extraction",ExtractionPolicy)
        result=run_local(args.pdf,args.output,p,
                         max_ranges=args.max_ranges or None)
    else:
        from .chunking import ChunkPolicy
        p=_policy(args.profile,"chunking",ChunkPolicy)
        result=chunk_artifacts(args.artifacts,args.output,p)
    print(json.dumps(result,indent=2,default=str))
    if isinstance(result,dict) and result.get("FAILED"):
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
