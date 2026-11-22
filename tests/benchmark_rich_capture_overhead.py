"""A/B measure rich font-run capture overhead on an authentic local PDF.

Alternating scan order reduces file-cache bias; no external APIs or DB writes.
Results are observational, not a hard CI speed SLA on noisy shared runners.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path


def compare_rich_capture(path: str, *, batch_size=16, repeats=2) -> dict:
    from pdf_processor import PDFProcessor
    if type(repeats) is not int or not 1<=repeats<=5:
        raise ValueError("repeats must be 1..5")
    scans={"baseline":[],"rich":[]}
    for trial in range(repeats):
        # ABBA interleaving minimizes OS cache/warm-up order effects.
        order=(False,True) if trial%2==0 else (True,False)
        for enabled in order:
            t0=time.perf_counter()
            pages=0
            style_blocks=0
            proc=PDFProcessor(str(path),"perf-test",Path(path).name,
                              strict=True,capture_rich_text=enabled)
            for batch in proc.iter_page_batches(batch_pages=batch_size):
                for page in batch.pages:
                    if page.error:
                        raise RuntimeError(
                            f"Cannot benchmark incomplete page {page.page_number}: "
                            f"{page.error}")
                    pages+=1
                    style_blocks+=sum(bool(element.runs)
                                     for element in page.elements)
            elapsed=time.perf_counter()-t0
            scans["rich" if enabled else "baseline"].append(elapsed)
    baseline=statistics.median(scans["baseline"])
    rich=statistics.median(scans["rich"])
    return {
        "source":Path(path).name,
        "pages_per_pass":pages,
        "repeats_per_mode":repeats,
        "baseline_seconds":scans["baseline"],
        "rich_seconds":scans["rich"],
        "median_baseline_seconds":round(baseline,4),
        "median_rich_seconds":round(rich,4),
        "rich_over_baseline_ratio":round(rich/max(baseline,1e-6),4),
        "additional_pdf_passes_for_rich_spans":0,
        "notes":"Native parsing only; no OCR, translation, embeddings or persistence",
    }


if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--pdf",required=True)
    p.add_argument("--repeats",type=int,default=2)
    p.add_argument("--batch-size",type=int,default=16)
    a=p.parse_args()
    print(json.dumps(compare_rich_capture(
        a.pdf,batch_size=a.batch_size,repeats=a.repeats),indent=2))
