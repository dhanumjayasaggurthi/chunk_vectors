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
from .profile import load_profile, require_approved_for_pilot, profile_summary


def _policy(path, section, cls):
    if not path:
        return cls()
    profile=load_profile(path)
    settings=profile.get(section)
    if not isinstance(settings,dict):
        raise ValueError(f"Profile must contain a {section} object")
    return cls(**settings)


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


def chunk_artifacts(artifacts,output,chunk_policy,metadata_policy=None):
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
    chunks=chunk_elements(manifest["document_id"],elements,chunk_policy,
                          profile_id=profile_key)
    from .metadata import extract_metadata
    document_metadata=extract_metadata(elements,metadata_policy or {"enabled":False})
    payload={
        "schema_version":1,"document_id":manifest["document_id"],
        "source_sha256":manifest["source_sha256"],
        "extraction_job_key":manifest["job_key"],"chunk_policy":asdict(chunk_policy),
        "document_metadata":document_metadata,
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
    plan.add_argument("--profile",help="Optional YAML/JSON profile; read-only planning")
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
    validate=cmds.add_parser("validate",help="Validate consumer YAML without running any stage")
    validate.add_argument("--profile",required=True)
    args=parser.parse_args(argv)
    if args.command=="validate":
        profile=load_profile(args.profile)
        result={"valid":True, **profile_summary(profile)}
    elif args.command=="plan":
        n=pdf_page_count(args.pdf)
        range_pages=args.range_pages
        if args.profile:
            profile=load_profile(args.profile)
            range_pages=ExtractionPolicy(**profile["extraction"]).range_pages
        p=page_ranges(n,range_pages)
        result={"pages":n,"range_pages":range_pages,
                "range_tasks":len(p),"first_ranges":p[:5],
                "note":"Read-only; zero GCP requests and no DB writes"}
    elif args.command=="extract":
        if args.profile:
            require_approved_for_pilot(load_profile(args.profile))
        p=_policy(args.profile,"extraction",ExtractionPolicy)
        result=run_local(args.pdf,args.output,p,
                         max_ranges=args.max_ranges or None)
    else:
        from .chunking import ChunkPolicy
        profile=load_profile(args.profile) if args.profile else None
        if profile:
            require_approved_for_pilot(profile)
        p=_policy(args.profile,"chunking",ChunkPolicy)
        meta=profile.get("metadata",{"enabled":False}) if profile else {"enabled":False}
        result=chunk_artifacts(args.artifacts,args.output,p,metadata_policy=meta)
    print(json.dumps(result,indent=2,default=str))
    if isinstance(result,dict) and result.get("FAILED"):
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
