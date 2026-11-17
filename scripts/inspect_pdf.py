"""Inspect one *local* PDF without Snowflake, S3 or Azure.

This is a diagnostic tool, not the production RIMdocs pipeline. It reports
unresolved evidence and OCR gaps for manual review.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import fitz

from backend.birth_dates import extract_pairs


def inspect(path: Path, case: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("Encrypted PDF requires authorized decryption")
        total=pdf.page_count
        scanned=min(total,25) if case=="IBD" else total
        pages=[]
        unreadable=[]
        for i in range(scanned):
            page=pdf[i]
            text=page.get_text("text",sort=True) or ""
            pages.append({"page":i+1,"text":text})
            if not text.strip() and page.get_images():
                unreadable.append(i+1)
    pairs, issues=extract_pairs(pages,case)
    if unreadable:
        issues.append("OCR_REQUIRED on PDF pages: "+", ".join(map(str,unreadable[:25])))
    return {
        "source_file":path.name,
        "use_case":case,
        "pdf_pages":total,
        "pages_examined":scanned,
        "status":"EXTRACTED_REQUIRES_QA" if pairs and not issues else "REVIEW_REQUIRED",
        "pairs":pairs,
        "issues":issues,
        "notice":"Diagnostic result: validate against original PDF and RIMdocs metadata before regulatory use",
    }


def main() -> None:
    parser=argparse.ArgumentParser(description="Inspect original local PBRER/DSUR PDF for birth dates")
    parser.add_argument("pdf",type=Path)
    parser.add_argument("--case",choices=("IBD","DIBD"),required=True)
    parser.add_argument("--output",type=Path,help="Optional local JSON output")
    args=parser.parse_args()
    result=inspect(args.pdf,args.case)
    encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.write_text(encoded+"\n",encoding="utf-8")
        print(f"Wrote {args.output} ({result['status']}, {len(result['pairs'])} pair(s))")
    else:
        print(encoded)


if __name__=="__main__":
    main()
