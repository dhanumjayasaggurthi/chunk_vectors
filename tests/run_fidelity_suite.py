from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layout_preserver import (  # noqa: E402
    extract_content_pdf,
    extract_document_layout,
    extract_section_pdf,
    find_text_matches,
    load_document_layout,
)

DATA = ROOT / "testdata"
RESULTS = ROOT / "results"
RESULTS.mkdir(parents=True, exist_ok=True)


def direct_exact_text(page: fitz.Page) -> str:
    flags = getattr(fitz, "TEXTFLAGS_RAWDICT", 0) | getattr(fitz, "TEXT_PRESERVE_WHITESPACE", 0)
    raw = page.get_text("rawdict", flags=flags)
    blocks = []
    for block in raw.get("blocks", []):
        if int(block.get("type", -1)) != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            line_text = "".join(
                "".join(ch.get("c", "") for ch in span.get("chars", []))
                for span in line.get("spans", [])
            )
            lines.append(line_text)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def pix_similarity(a: fitz.Pixmap, b: fitz.Pixmap) -> float:
    aw, ah, an = a.width, a.height, a.n
    bw, bh, bn = b.width, b.height, b.n
    w, h, n = min(aw, bw), min(ah, bh), min(an, bn)
    if w <= 0 or h <= 0 or n <= 0:
        return 0.0
    aa, bb = memoryview(a.samples), memoryview(b.samples)
    total = count = 0
    for y in range(h):
        oa, ob = y * aw * an, y * bw * bn
        for x in range(w):
            pa, pb = oa + x * an, ob + x * bn
            for c in range(n):
                total += abs(int(aa[pa + c]) - int(bb[pb + c]))
                count += 1
    score = max(0.0, 1.0 - (total / max(1, count)) / 255.0)
    shape_penalty = (w * h) / max(1, max(aw, bw) * max(ah, bh))
    return score * shape_penalty


def fragment_similarity(source_pdf: Path, section_pdf: Path, fragments) -> float:
    src = fitz.open(source_pdf)
    sec = fitz.open(section_pdf)
    scores = []
    try:
        if sec.page_count != len(fragments):
            return 0.0
        matrix = fitz.Matrix(1.5, 1.5)
        for i, fragment in enumerate(fragments):
            src_page = src[fragment.page_number - 1]
            clip = fitz.Rect(fragment.bbox) & src_page.rect
            a = src_page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
            b = sec[i].get_pixmap(matrix=matrix, alpha=False)
            scores.append(pix_similarity(a, b))
    finally:
        src.close()
        sec.close()
    return sum(scores) / len(scores) if scores else 0.0


def validate_fixture(path: Path) -> dict:
    asset_dir = RESULTS / "assets" / path.stem
    layout = extract_document_layout(path, asset_dir=asset_dir)
    manifest_path = RESULTS / f"{path.stem}.layout.json.gz"
    layout.save_json(manifest_path, compress=True)
    loaded = load_document_layout(manifest_path)

    src = fitz.open(path)
    exact_pages = 0
    chars_total = chars_in_bounds = 0
    bold = italic = tables = cells = cell_boxes = 0
    for i, page in enumerate(layout.pages):
        if page.exact_text == direct_exact_text(src[i]):
            exact_pages += 1
        tables += len(page.tables)
        for table in page.tables:
            cells += len(table.cells)
            cell_boxes += sum(1 for cell in table.cells if cell.bbox is not None)
        for block in page.text_blocks:
            for line in block.lines:
                for span in line.spans:
                    bold += int(span.bold)
                    italic += int(span.italic)
                    for ch in span.chars:
                        chars_total += 1
                        x0, y0, x1, y1 = ch.bbox
                        if (
                            x0 >= -1 and y0 >= -1
                            and x1 <= page.width + 1 and y1 <= page.height + 1
                            and x1 >= x0 and y1 >= y0
                        ):
                            chars_in_bounds += 1
    src.close()

    spacing_ok = "alpha  beta   gamma    delta" in layout.pages[0].exact_text
    section = layout.get_section("II. Requirements")
    section_path = RESULTS / f"{path.stem}.section-II.pdf"
    extract_section_pdf(path, layout, section, section_path)
    similarity = fragment_similarity(path, section_path, section.fragments)

    matches = find_text_matches(layout, "mandatory requirement")
    content_ok = False
    content_path = RESULTS / f"{path.stem}.content.pdf"
    if matches:
        match = matches[0]
        extract_content_pdf(path, match["page_number"], match["bbox"], content_path)
        d = fitz.open(content_path)
        try:
            text = "".join(p.get_text() for p in d)
        finally:
            d.close()
        content_ok = "mandatory requirement" in text.casefold()

    roundtrip_ok = loaded.to_dict() == layout.to_dict()
    return {
        "document": path.name,
        "pages": layout.page_count,
        "text_exact_pages": exact_pages,
        "text_fidelity_pct": round(exact_pages / max(1, layout.page_count) * 100, 3),
        "chars": chars_total,
        "bbox_valid_pct": round(chars_in_bounds / max(1, chars_total) * 100, 3),
        "bold_spans": bold,
        "italic_spans": italic,
        "tables": tables,
        "cells_with_bbox_pct": round(cell_boxes / max(1, cells) * 100, 3),
        "sections": len(layout.sections),
        "spacing_preserved": spacing_ok,
        "section_vector_similarity_pct": round(similarity * 100, 4),
        "content_clip_text_ok": content_ok,
        "manifest_roundtrip_ok": roundtrip_ok,
        "pass": (
            exact_pages == layout.page_count
            and chars_total > 0
            and chars_in_bounds == chars_total
            and bold > 0
            and italic > 0
            and tables >= 2
            and cell_boxes / max(1, cells) > 0.90
            and spacing_ok
            and similarity > 0.995
            and content_ok
            and roundtrip_ok
        ),
    }


def main() -> None:
    if not list(DATA.glob("regulatory_fixture_*.pdf")):
        from generate_regulatory_fixtures import main as generate
        generate()

    rows = [validate_fixture(p) for p in sorted(DATA.glob("regulatory_fixture_*.pdf"))]
    json_path = RESULTS / "fidelity_results.json"
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    csv_path = RESULTS / "fidelity_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "documents": len(rows),
        "passed": sum(1 for r in rows if r["pass"]),
        "failed": sum(1 for r in rows if not r["pass"]),
        "min_section_vector_similarity_pct": min(r["section_vector_similarity_pct"] for r in rows),
        "all_text_fidelity_pct": round(
            sum(r["text_exact_pages"] for r in rows) / sum(r["pages"] for r in rows) * 100,
            4,
        ),
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in rows:
        print(r["document"], "PASS" if r["pass"] else "FAIL", r["section_vector_similarity_pct"])
    raise SystemExit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
