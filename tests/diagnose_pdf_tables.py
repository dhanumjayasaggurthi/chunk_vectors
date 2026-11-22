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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--pages", nargs="+", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = diagnose(args.pdf, args.pages)
    Path(args.output).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("TABLE_CANDIDATE_DIAGNOSTICS=" + json.dumps(rows, separators=(",", ":")))


if __name__ == "__main__":
    main()
