"""Reproducible, offline mixed-content PDF stress harness.

Default is a 100-document *synthetic smoke test*, NOT 100 independently
sourced large regulatory documents. Increase --pages-per-doc and run against
an approved real-world corpus for production acceptance. No cloud calls.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import resource
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create_fixture(path, pages, image_bytes):
    import fitz
    doc = fitz.open()
    for number in range(pages):
        page = doc.new_page()
        page.insert_text((45, 48), f"REGULATORY STUDY {path.stem} - {number + 1}",
                         fontsize=14, fontname="hebo")
        page.insert_text((45, 75), "Dose 25 mg; response 82%; safety margin 1.5",
                         fontsize=11)
        page.insert_text((45, 100), "Follow-up analysis", fontsize=11,
                         fontname="heit")
        # Every page combines text, a table, an image and a vector chart.
        xs = (45, 260, 550)
        ys = (145, 185, 225, 265)
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for row in range(3):
            page.insert_text((55, 170 + 40 * row), f"Endpoint {row} 25 mg")
            page.insert_text((275, 170 + 40 * row), f"Response {82 + row}%")
        page.insert_image(fitz.Rect(45, 310, 260, 420), stream=image_bytes)
        page.draw_line((310, 420), (310, 310))
        page.draw_line((310, 420), (540, 420))
        for x, height in [(340, 40), (390, 75), (440, 55), (490, 95)]:
            page.draw_rect(fitz.Rect(x, 420 - height, x + 25, 420),
                           color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        page.insert_text((310, 445), "Figure: regulatory trend 2026")
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def run_one(path, batch_size):
    from pdf_processor import PDFProcessor
    proc = PDFProcessor(str(path), path.stem, path.name, strict=True,
                        capture_rich_text=True)
    pages = 0
    styled = 0
    tables = 0
    images = 0
    for window in proc.iter_page_batches(batch_pages=batch_size):
        for page in window.pages:
            if page.error:
                raise RuntimeError(f"{path.name} page {page.page_number}: {page.error}")
            pages += 1
            styled += sum(bool(el.runs) for el in page.elements)
            tables += int(page.has_tables)
            images += int(page.has_images)
    return pages, styled, tables, images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--pages-per-doc", type=int, default=12)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--page-batch-size", type=int, default=16)
    args = parser.parse_args()
    if not (1 <= args.documents <= 1000 and 1 <= args.pages_per_doc <= 25000
            and 1 <= args.workers <= 32 and 1 <= args.page_batch_size <= 128):
        parser.error("Invalid bounded stress configuration")
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (320, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((12, 35), "Regulatory Figure", fill="black")
    draw.rectangle((15, 75, 280, 115), outline="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        files = []
        for i in range(args.documents):
            path = root / f"regulatory_{i:04d}.pdf"
            create_fixture(path, args.pages_per_doc, buffer.getvalue())
            files.append(path)
        generated_seconds = time.monotonic() - start
        totals = [0, 0, 0, 0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(lambda p: run_one(p, args.page_batch_size), files):
                for i, value in enumerate(result):
                    totals[i] += value
        elapsed = time.monotonic() - start
    expected = args.documents * args.pages_per_doc
    if totals[0] != expected or totals[1] < expected or totals[2] < expected or totals[3] < expected:
        raise RuntimeError(f"Incomplete synthetic extraction: {totals} vs {expected}")
    print(json.dumps({
        "test_type": "synthetic_mixed_page_stress_not_real_regulatory_corpus",
        "documents": args.documents, "pages": totals[0],
        "pages_with_styled_text": totals[1],
        "pages_with_tables": totals[2], "pages_with_images": totals[3],
        "workers": args.workers,
        "generation_seconds": round(generated_seconds, 2),
        "total_seconds": round(elapsed, 2),
        "pages_per_second_including_generation": round(expected / max(elapsed, .001), 2),
        "peak_process_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    }, indent=2))


if __name__ == "__main__":
    main()
