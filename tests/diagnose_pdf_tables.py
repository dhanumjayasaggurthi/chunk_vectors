"""Inspect suspicious table candidates in public regulatory PDFs.

Read-only diagnostics: no OCR, embeddings, extraction DB writes or API calls.
Provides geometry, extracted cell count and text coverage for manual triage.
Never treats a candidate as safe to ignore automatically.
"""
from __future__ import annotations

import argparse
from collections import Counter
from statistics import median
from bisect import bisect_right
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


def open_grid_probe(page,table):
    """Diagnostic only: physical lines under actual column rules, not semantic rows."""
    x0,_,x1,top=table.bbox
    xs=sorted(set(x for cell in table.cells for x in (cell[0],cell[2])))
    if len(xs)<4 or top>page.height*.25 or x1-x0<page.width*.4:
        return {'status':'NOT_CANDIDATE'}
    # Require all actual column rules extend continuously beyond the header.
    ends=[]
    for x in xs:
        matches=[e['bottom'] for e in page.vertical_edges
                 if abs(e['x0']-x)<=2 and e['top']<=top+2
                 and e['bottom']>=top+page.height*.15]
        if not matches:
            return {'status':'NO_CONTINUOUS_COLUMN_RULES'}
        ends.append(max(matches))
    bottom=min(ends)
    region=page.crop((x0,top,x1,bottom))
    # Independently segment words within each source column. The detector's
    # page-wide word builder can merge dot leaders and the adjacent column.
    from pdfplumber.utils import extract_words
    chars_by_column=[[] for _ in xs[:-1]]
    for ch in region.chars:
        if not ch['text'].strip():
            continue
        col=bisect_right(xs,(ch['x0']+ch['x1'])/2)-1
        if not 0<=col<len(chars_by_column):
            return {'status':'SOURCE_CHAR_OUTSIDE_GRID'}
        if ch['x0']<xs[col]-.5 or ch['x1']>xs[col+1]+.5:
            return {'status':'GLYPH_CROSSES_COLUMN'}
        chars_by_column[col].append(ch)
    words=[w for chars in chars_by_column for w in extract_words(chars)]
    if len(words)<10:
        return {'status':'INSUFFICIENT_BODY'}
    groups=[]
    for word in sorted(words,key=lambda w:(w['top'],w['x0'])):
        if not groups or word['top']-groups[-1][0]['top']>3:
            groups.append([])
        groups[-1].append(word)
    shift=median(w['height'] for w in words)/2
    ys=[top]+[(max(w['top'] for w in a)+min(w['top'] for w in b))/2+shift
              for a,b in zip(groups,groups[1:])]+[bottom]
    if any(b<=a for a,b in zip(ys,ys[1:])):
        return {'status':'AMBIGUOUS_ROW_BOUNDARIES'}
    grids=region.find_tables({'vertical_strategy':'explicit','explicit_vertical_lines':xs,
                       'horizontal_strategy':'explicit','explicit_horizontal_lines':ys,
                       'snap_tolerance':0,'intersection_tolerance':1})
    if len(grids)!=1:
        return {'status':'AMBIGUOUS_GRID'}
    grid=grids[0]
    matrix=grid.extract()
    if len(matrix)!=len(ys)-1 or any(len(row)!=len(xs)-1 for row in matrix):
        return {'status':'GRID_DIMENSION_MISMATCH'}
    # Assign each original character by its center; compare independently
    # against each cell. No character is silently dropped or counted twice.
    expected={}
    for ch in region.chars:
        if not ch['text'].strip():
            continue
        col=bisect_right(xs,(ch['x0']+ch['x1'])/2)-1
        row=bisect_right(ys,(ch['top']+ch['bottom'])/2)-1
        if not (0<=row<len(matrix) and 0<=col<len(xs)-1):
            return {'status':'SOURCE_CHAR_OUTSIDE_GRID'}
        expected.setdefault((row,col),Counter()).update(c for c in ch['text'] if c.strip())
    for i,row in enumerate(matrix):
        for j,value in enumerate(row):
            if Counter(c for c in (value or '') if c.strip())!=expected.get((i,j),Counter()):
                return {'status':'CELL_CHARACTER_MISMATCH'}
    return {'status':'PHYSICAL_LINES_VERIFIED_ONLY','physical_lines':len(matrix),'columns':len(xs)-1,
            'nonspace_characters':sum(sum(c.values()) for c in expected.values()),'logical_rows_verified':False}


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
    parser.add_argument("--probe-open-grid", action="store_true",
                        help="Test physical-line grids, without asserting logical table rows")
    parser.add_argument("--validate-recovery", action="store_true",
                        help="Also run production native extraction on these pages")
    args = parser.parse_args()
    rows = diagnose(args.pdf, args.pages)
    if args.candidate is not None:
        rows = [row for row in rows if row["candidate"] == args.candidate]
    Path(args.output).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("TABLE_CANDIDATE_DIAGNOSTICS=" + json.dumps(rows, separators=(",", ":")))
    if args.probe_open_grid:
        import pdfplumber
        from config import TABLE_SETTINGS
        probes=[]
        with pdfplumber.open(args.pdf) as pdf:
            for number in args.pages:
                pp=pdf.pages[number-1]
                try:
                    grids=pp.find_tables(TABLE_SETTINGS)
                    result=(open_grid_probe(pp,grids[0]) if len(grids)==1
                            else {"status":"NOT_SINGLE_GRID"})
                    probes.append({"page":number,**result})
                finally:
                    pp.close()
        print("OPEN_GRID_PROBES="+json.dumps(probes,separators=(",",":")))
    if args.validate_recovery:
        validation = validate_recovery(args.pdf,args.pages)
        print("NATIVE_RECOVERY_VALIDATION=" + json.dumps(validation,separators=(",",":")))
        if any(row["status"] != "SUCCESS" for row in validation):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
