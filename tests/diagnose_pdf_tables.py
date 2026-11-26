"""Inspect suspicious table candidates in public regulatory PDFs.

Read-only diagnostics: no OCR, embeddings, extraction DB writes or API calls.
Provides geometry, extracted cell count and text coverage for manual triage.
Never treats a candidate as safe to ignore automatically.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def diagnose(file_path: str, pages: list[int]) -> list[dict]:
    import fitz
    import pdfplumber
    from config import TABLE_SETTINGS
    from table_extractor import TableExtractor

    out = []
    extractor = TableExtractor(file_path, summarise=False)
    with fitz.open(file_path) as native, pdfplumber.open(file_path) as pdf:
        for number in pages:
            if not 1 <= number <= native.page_count:
                raise ValueError(f"Page {number} is out of bounds")
            fp = pdf.pages[number - 1]
            try:
                tables = fp.find_tables(TABLE_SETTINGS)
                for index, table in enumerate(tables):
                    cells = table.extract()
                    parsed = extractor._process_table(
                        cells, number, index, "", allow_short=True)
                    box = tuple(float(x) for x in table.bbox)
                    native_text = native[number - 1].get_textbox(fitz.Rect(*box))
                    raw_nonempty = sum(
                        bool(str(v).strip()) for row in (cells or [])
                        for v in (row or []) if v is not None
                    )
                    cell_count = sum(len(row or []) for row in (cells or []))
                    region_area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
                    all_words = fp.crop(box).extract_words()
                    out.append({
                        "page": number,
                        "candidate": index,
                        "bbox": [round(x, 2) for x in box],
                        "bbox_area": round(region_area, 2),
                        "raw_row_count": len(cells or []),
                        "raw_cell_count": cell_count,
                        "raw_nonempty_cell_count": raw_nonempty,
                        "pdfplumber_words_in_bbox": len(all_words),
                        "native_text_chars_in_bbox": len(native_text.strip()),
                        "parsed_as_table": parsed is not None,
                        "parsed_rows": parsed.row_count if parsed else 0,
                        "parsed_cols": parsed.col_count if parsed else 0,
                        "page_native_text_chars": len(native[number - 1].get_text().strip()),
                        "page_image_xrefs": len(native[number - 1].get_images(full=True)),
                    })
            finally:
                fp.close()
    return out


def validate_recovery(file_path: str, pages: list[int]) -> list[dict]:
    """Run the production native page path on selected pages, without DB/API."""
    import fitz
    import pdfplumber
    from pdf_processor import PDFProcessor, ElementType
    from table_extractor import inject_table_text_into_elements
    processor=PDFProcessor(
        file_path,"diagnostic",Path(file_path).name,strict=True,
        capture_rich_text=True,capture_table_structure=True,
        preserve_uninterpreted_visuals=True,empty_table_policy="preserve_defer",
    )
    results=[]
    with fitz.open(file_path) as pdf, pdfplumber.open(file_path) as plumb:
        for number in pages:
            if not 1 <= number <= pdf.page_count:
                raise ValueError(f"Page {number} is out of bounds")
            pp=plumb.pages[number-1]
            try:
                page=processor._process_page(pdf,number-1,plumb_page=pp)
                error=page.error
                if not error and page.has_tables:
                    try:
                        inject_table_text_into_elements(
                            page.elements,page.staged_table_results,strict=True)
                    except (ValueError,RuntimeError) as exc:
                        error=str(exc)
                results.append({
                    "page":number,"status":"FAILED" if error else "SUCCESS",
                    "error":error,
                    "native_chars":sum(len(e.text) for e in page.elements
                                       if e.element_type==ElementType.TEXT),
                    "structured_tables":sum(e.source_table is not None for e in page.elements),
                    "deferred_candidates":[e.unresolved_region["candidate_index"]
                        for e in page.elements if e.unresolved_region],
                })
            finally:
                pp.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--pages", nargs="+", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate", type=int, help="Only print/store this candidate index")
    parser.add_argument("--validate-recovery", action="store_true",
                        help="Also run production native extraction on these pages")
    args = parser.parse_args()
    rows = diagnose(args.pdf, args.pages)
    if args.candidate is not None:
        rows = [row for row in rows if row["candidate"] == args.candidate]
    Path(args.output).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("TABLE_CANDIDATE_DIAGNOSTICS=" + json.dumps(rows, separators=(",", ":")))
    if args.validate_recovery:
        validation = validate_recovery(args.pdf,args.pages)
        print("NATIVE_RECOVERY_VALIDATION=" + json.dumps(validation,separators=(",",":")))
        if any(row["status"] != "SUCCESS" for row in validation):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
