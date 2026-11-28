"""Experimental line-only prepass; never used by production extraction.

Depends on PDFMiner/pdfplumber internals. Compare independent cell extraction
and exact detected geometry; agreement with baseline is NOT semantic accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

import pdfplumber
from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent, PDFPageInterpreter


class _EdgeAggregator(PDFPageAggregatorWithMarkedContent):
    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        # Exactly the advance calculation used by LTChar. Do not create glyphs.
        return font.char_width(cid) * fontsize * scaling


class _EdgePage(Page):
    @property
    def layout(self):
        if not hasattr(self, "_layout"):
            device = _EdgeAggregator(self.pdf.rsrcmgr, pageno=self.page_number, laparams=None)
            PDFPageInterpreter(self.pdf.rsrcmgr, device).process_page(self.page_obj)
            self._layout = device.get_result()
        return self._layout


def _geometry(tables):
    return [(t.bbox, sorted(t.cells)) for t in tables]


def _matrices(tables):
    return [t.extract() for t in tables]


def compare(path, numbers, settings):
    if settings.get("vertical_strategy") != "lines" or settings.get("horizontal_strategy") != "lines":
        raise ValueError("Only line-based table detection is eligible")
    report = dict(pages=0, positive_pages=0, baseline_seconds=0., hybrid_seconds=0.,
                  negative_baseline_seconds=0., negative_hybrid_seconds=0.,
                  positive_baseline_seconds=0., positive_hybrid_seconds=0., mismatches=[])
    start = time.monotonic()
    with pdfplumber.open(path, pages=numbers) as doc:
        pages = doc.pages
        report["open_seconds"] = time.monotonic() - start
        if len(pages) != len(numbers):
            raise ValueError("Incomplete sample page selection")
        for p in pages:
            def baseline():
                t = p.find_tables(settings)
                return _geometry(t), _matrices(t)
            def hybrid():
                edge = _EdgePage(doc, p.page_obj, p.page_number, p.initial_doctop)
                full = None
                try:
                    geometry = _geometry(edge.find_tables(settings))
                    if not geometry:
                        return geometry, []
                    full = Page(doc, p.page_obj, p.page_number, p.initial_doctop)
                    t = full.find_tables(settings)
                    if geometry != _geometry(t):
                        raise ValueError("Prepass/fallback geometry mismatch")
                    return geometry, _matrices(t)
                finally:
                    edge.close()
                    if full is not None:
                        full.close()
            def timed(fn):
                tick = time.monotonic()
                return fn(), time.monotonic() - tick
            try:
                # Alternate order to reduce shared PDF resource-cache bias.
                if p.page_number % 2:
                    b, bt = timed(baseline); h, ht = timed(hybrid)
                else:
                    h, ht = timed(hybrid); b, bt = timed(baseline)
                report["pages"] += 1
                positive = bool(b[0])
                report["positive_pages"] += positive
                report["baseline_seconds"] += bt
                report["hybrid_seconds"] += ht
                prefix = "positive" if positive else "negative"
                report[prefix+"_baseline_seconds"] += bt
                report[prefix+"_hybrid_seconds"] += ht
                if b != h:
                    report["mismatches"].append(p.page_number)
            finally:
                p.close()
    report["exact_detected_geometry_and_cell_parity"] = not report["mismatches"]
    return report


def sample_numbers(total, limit):
    count = min(total, limit)
    if count == 1:
        return [1]
    return [1 + i*(total-1)//(count-1) for i in range(count)]


def main():
    from online_public_regulatory_benchmark import candidates, download
    from native_worker_scaling_benchmark import FIXTURES, host_limits
    from config import TABLE_SETTINGS
    import pdfminer
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--sample-pages", type=int, default=100)
    parser.add_argument("--local-pdf", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.sample_pages <= 500:
        parser.error("sample-pages must be 1..500")
    report = {"host": host_limits(), "pdfminer": pdfminer.__version__,
              "experimental_only": True, "semantic_accuracy_verified": False,
              "cloud_calls": False, "documents": []}
    out = Path(args.report)
    def save():
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.replace(out)
    save()
    with tempfile.TemporaryDirectory(prefix="native-geometry-probe-") as tmp:
        if args.local_pdf:
            import fitz
            items = []
            for value in args.local_pdf:
                path = Path(value).resolve(strict=True)
                with fitz.open(path) as pdf:
                    count = pdf.page_count
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                items.append({"name": path.name, "path": str(path), "pages": count, "sha256": digest})
        else:
            urls = dict(candidates())
            items = [download((name, urls[name]), Path(tmp), min_pages=100,
                              max_bytes=32*1024*1024, timeout=40) for name in FIXTURES]
        if any("path" not in item for item in items):
            report["rejected_sources"] = [item for item in items if "path" not in item]
            save()
            raise SystemExit("Required source unavailable")
        if len({item["sha256"] for item in items}) != len(items):
            raise SystemExit("Duplicate sources")
        for item in items:
            numbers = sample_numbers(item["pages"], args.sample_pages)
            result = compare(item["path"], numbers, TABLE_SETTINGS)
            row = {"name": item["name"], "sha256": item["sha256"],
                   "source_pages": item["pages"], "sample_numbers": numbers, **result}
            report["documents"].append(row)
            save()
            print("GEOMETRY_RESULT="+json.dumps({k:v for k,v in row.items() if k!="sample_numbers"}), flush=True)
            if not result["exact_detected_geometry_and_cell_parity"]:
                raise SystemExit("Detected table parity failure")
    report["complete"] = True
    report["total_sampled_pages"] = sum(x["pages"] for x in report["documents"])
    report["baseline_seconds"] = sum(x["baseline_seconds"] for x in report["documents"])
    report["hybrid_seconds"] = sum(x["hybrid_seconds"] for x in report["documents"])
    save()
    print("GEOMETRY_SUMMARY="+json.dumps({k:v for k,v in report.items() if k!="documents"}), flush=True)


if __name__ == "__main__":
    main()
