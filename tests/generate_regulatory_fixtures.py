from __future__ import annotations

import json
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "testdata"
OUT.mkdir(parents=True, exist_ok=True)


def put(page, xy, text, font="tiro", size=11, color=(0,0,0)):
    page.insert_text(xy, text, fontname=font, fontsize=size, color=color)


def box(page, rect, text, font="tiro", size=11, align=0):
    page.insert_textbox(rect, text, fontname=font, fontsize=size, align=align, lineheight=1.25)


def draw_table(page, x, y, widths, rows, row_h=24, header=True, italic_header=False):
    total_w = sum(widths)
    total_h = row_h * len(rows)
    # grid
    page.draw_rect(fitz.Rect(x, y, x + total_w, y + total_h), width=0.8)
    xx = x
    for w in widths[:-1]:
        xx += w
        page.draw_line((xx, y), (xx, y + total_h), width=0.6)
    for r in range(1, len(rows)):
        yy = y + r * row_h
        page.draw_line((x, yy), (x + total_w, yy), width=0.6)
    # cells
    for r, row in enumerate(rows):
        xx = x
        for c, value in enumerate(row):
            font = "tibo" if header and r == 0 else "tiro"
            if italic_header and r == 0:
                font = "tibi"
            rect = fitz.Rect(xx + 4, y + r*row_h + 4, xx + widths[c] - 4, y + (r+1)*row_h - 2)
            page.insert_textbox(rect, value, fontname=font, fontsize=9, lineheight=1.0)
            xx += widths[c]
    return [x, y, x+total_w, y+total_h]


def make_fixture(idx: int):
    path = OUT / f"regulatory_fixture_{idx:02d}.pdf"
    doc = fitz.open()
    # Slightly vary size/margins to exercise coordinates.
    w = 612 + (idx % 3) * 18
    h = 792 + (idx % 2) * 36

    p1 = doc.new_page(width=w, height=h)
    put(p1, (w-255, 45), "Conformed to Regulatory Register version", font="tiit", size=10)
    put(p1, (54, 78), "REGULATORY COMMISSION", font="tibo", size=14)
    put(p1, (54, 105), f"17 CFR Parts {200+idx}, {230+idx}, and {240+idx}", font="tibo", size=12)
    put(p1, (54, 132), f"[Release No. RC-{2026}-{1000+idx}]", font="tibo", size=12)
    put(p1, (54, 166), f"Technical Amendments and Requirements - Test {idx}", font="tibo", size=13)

    y = 198
    labels = [
        ("AGENCY:", " Regulatory Commission."),
        ("ACTION:", " Final rule; technical amendment."),
        ("SUMMARY:", f" This document establishes structured extraction requirements for fixture {idx}."),
        ("DATES:", " Effective October 1, 2026."),
    ]
    for label, rest in labels:
        put(p1, (54, y), label, font="tibo", size=11)
        put(p1, (124, y), rest, font="tiro", size=11)
        y += 26
    put(p1, (54, y+2), "Spacing test: alpha  beta   gamma    delta", font="cour", size=9)
    y += 35
    put(p1, (54, y), "SUPPLEMENTARY INFORMATION:", font="tibo", size=11)
    box(p1, fitz.Rect(54, y+10, w-54, y+58),
        "The Commission is preserving exact text style, table geometry, section titles, and bounding boxes. "
        "Italic terms remain italic and bold labels remain bold.", font="tiro", size=10.5)
    table_top = y + 78
    rows = [
        ["Commission Reference", "CFR Citation", "Control No."],
        [f"Rule {30+idx}-1", f"§ {200+idx}.30-1", f"32{idx:02d}-0756"],
        ["Regulation S-X", f"§ {210+idx}.4-08", f"32{idx:02d}-0619"],
        ["Form R-1", f"§ {239+idx}.13", f"32{idx:02d}-0716"],
    ]
    draw_table(p1, 54, table_top, [210, 155, 120], rows, row_h=25, italic_header=(idx % 2 == 0))
    put(p1, (54, table_top+126), "1  Footnote text with exact indentation.", font="tiro", size=8)

    p2 = doc.new_page(width=w, height=h)
    put(p2, (54, 60), "I. Introduction and Background", font="tibo", size=14)
    box(p2, fitz.Rect(54, 82, w-54, 180),
        f"Fixture {idx} evaluates whether the extractor maintains paragraph structure without normalizing source whitespace. "
        "The phrase important regulatory term is styled below.", font="tiro", size=11)
    put(p2, (54, 205), "important regulatory term", font="tiit", size=11)
    put(p2, (230, 205), " and ", font="tiro", size=11)
    put(p2, (260, 205), "mandatory requirement", font="tibo", size=11)
    put(p2, (54, 246), "A. Scope", font="tibo", size=12)
    box(p2, fitz.Rect(54, 265, w-54, 350),
        "This subsection applies to all records, tables, headings, inline emphasis, and page-space relationships. "
        "Coordinates are expressed in PDF points from the visible page origin.", font="tiro", size=10.5)
    # Create a two-column region on some fixtures.
    if idx % 3 == 0:
        put(p2, (54, 395), "Left Column", font="tibo", size=11)
        box(p2, fitz.Rect(54, 412, w/2-12, 575),
            "1. Preserve source order.\n2. Preserve fonts.\n3. Preserve bounding boxes.\n4. Preserve spaces.", font="tiro", size=10)
        put(p2, (w/2+18, 395), "Right Column", font="tibo", size=11)
        box(p2, fitz.Rect(w/2+18, 412, w-54, 575),
            "5. Preserve tables.\n6. Preserve links.\n7. Preserve drawings.\n8. Preserve page geometry.", font="tiro", size=10)

    p3 = doc.new_page(width=w, height=h)
    put(p3, (54, 60), "II. Requirements", font="tibo", size=14)
    put(p3, (54, 91), "§ 1. Exact preservation.", font="tibo", size=11)
    box(p3, fitz.Rect(72, 105, w-54, 178),
        "The stored representation must maintain characters, spaces, typographic emphasis, and geometry. "
        "Retrieval may return an exact source PDF clip instead of reconstructing fonts.", font="tiro", size=10.5)
    put(p3, (54, 204), "§ 2. Table preservation.", font="tibo", size=11)
    rows2 = [
        ["Requirement", "Stored Field", "Status"],
        ["Bold / italic", "span.flags", "Required"],
        ["Bounding box", "bbox[x0,y0,x1,y1]", "Required"],
        ["Whitespace", "char sequence", "Required"],
        ["Table cells", "cell bbox + text", "Required"],
    ]
    draw_table(p3, 72, 226, [180, 210, 110], rows2, row_h=28, header=True)
    put(p3, (54, 405), "III. Compliance Date", font="tibo", size=14)
    put(p3, (54, 432), "Compliance begins on October 1, 2026.", font="tiro", size=11)

    toc = [
        [1, f"Technical Amendments and Requirements - Test {idx}", 1],
        [1, "I. Introduction and Background", 2],
        [2, "A. Scope", 2],
        [1, "II. Requirements", 3],
        [1, "III. Compliance Date", 3],
    ]
    doc.set_toc(toc)
    doc.set_metadata({
        "title": f"Regulatory Fidelity Fixture {idx}",
        "author": "Automated test generator",
        "subject": "Layout preservation regression fixture",
    })
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def main() -> None:
    manifest = {"fixtures": [str(make_fixture(i).name) for i in range(1, 11)]}
    (OUT / "fixtures.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
