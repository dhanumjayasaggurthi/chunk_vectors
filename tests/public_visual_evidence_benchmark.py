"""Public PDF evidence acceptance test; no databases, credentials or cloud AI.

Downloads three fixed public government PDFs, captures complete selected page
ranges at 300 DPI, checks exact rendered pixels, simulates interrupted capture,
verifies no rendering on replay, then removes only the temporary public copy.
This measures retained page evidence, NOT figure segmentation or OCR accuracy.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_evidence import capture, verify, _paths, digest_file

# Physical page ranges selected to include FDA 7-panel survival plots and forest
# plots, and two NIST sources with microscopy and scientific charts.
SOURCES = (
    ("fda_odac_2024", "https://www.fda.gov/media/182138/download", 19, 29),
    ("nist_sem", "https://nvlpubs.nist.gov/nistpubs/jres/125/jres.125.014.pdf", 1, 0),
    ("nist_ir8524", "https://nvlpubs.nist.gov/nistpubs/ir/2024/NIST.IR.8524.pdf", 25, 40),
)


def check(spec):
    import fitz
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    name, url, first, last = spec
    with tempfile.TemporaryDirectory(prefix="public-visual-") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        retained = root / "retained"
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"])))
            with session.get(url, stream=True, timeout=(15, 60),
                             headers={"User-Agent": "Public regulatory extraction benchmark/1.0"}) as response:
                response.raise_for_status()
                size = 0
                with source.open("wb") as stream:
                    for block in response.iter_content(1024*1024):
                        size += len(block)
                        if size > 100*1024*1024:
                            raise ValueError("Public fixture exceeds download limit")
                        stream.write(block)
        source_hash = digest_file(source)
        with fitz.open(source) as pdf:
            count = pdf.page_count
            last = last or count
            if not first <= last <= count or last-first >= 50:
                raise ValueError("Unexpected public fixture page range")
            image_pages = vector_pages = text_pages = 0
            for i in range(first-1, last):
                page = pdf[i]
                image_pages += bool(page.get_image_info())
                vector_pages += bool(page.get_drawings())
                text_pages += bool(page.get_text().strip())
        kw = dict(dpi=300, policy_id="public-benchmark", authorized_page_derivatives=True)
        cold = capture(source, retained, first, last, **kw)
        n = last-first+1
        assert cold["rendered_pages"] == n and cold["skipped_verified_pages"] == 0
        cid = cold["capture_id"]
        # Pixel-exact comparison against fresh independent MuPDF rendering,
        # including axes, labels, overlays and microscopy in source rasterization.
        started = time.monotonic()
        with fitz.open(source) as pdf:
            for i in range(first, last+1):
                png, _ = _paths(retained, cid, i)
                saved = fitz.Pixmap(str(png))
                expected = pdf[i-1].get_pixmap(dpi=300, colorspace=fitz.csRGB,
                                                alpha=False, annots=True)
                assert (saved.width, saved.height, saved.n) == (expected.width, expected.height, expected.n)
                assert saved.samples == expected.samples, f"Pixel mismatch on page {i}"
        pixel_seconds = round(time.monotonic()-started,3)
        with patch.object(fitz.Page, "get_pixmap", side_effect=AssertionError("re-render")):
            replay = capture(source, retained, first, last, **kw)
        assert replay["skipped_verified_pages"] == n and replay["rendered_pages"] == 0
        assert not replay["derivative_writes"]
        # Crash between publishing PNG and manifest: replay just that page.
        _, manifest = _paths(retained, cid, first)
        manifest.unlink()
        resumed = capture(source, retained, first, last, **kw)
        assert resumed["rendered_pages"] == 1 and resumed["skipped_verified_pages"] == n-1
        assert digest_file(source) == source_hash
        source.unlink()
        restored = verify(retained, cid, first, last)
        assert restored["verified_pages"] == n
        assert not list(retained.rglob("*.pdf"))
        png, _ = _paths(retained, cid, last)
        with png.open("ab") as stream:
            stream.write(b"tamper")
        try:
            verify(retained, cid, first, last)
        except ValueError as error:
            assert "checksum/size" in str(error)
        else:
            raise AssertionError("Tampered evidence was accepted")
        return {
            "source": name, "url": url, "source_sha256": source_hash,
            "source_pages": count, "tested_pages": n, "first": first, "last": last,
            "pages_with_images": image_pages, "pages_with_paths": vector_pages,
            "pages_with_text": text_pages, "pixel_exact_pages": n,
            "retained_bytes": cold["image_bytes"], "cold_seconds": cold["elapsed_seconds"],
            "replay_seconds": replay["elapsed_seconds"], "pixel_check_seconds": pixel_seconds,
            "replay_rendered_pages": replay["rendered_pages"],
            "replay_skipped_pages": replay["skipped_verified_pages"],
            "resume_rendered_pages": resumed["rendered_pages"],
            "verified_after_source_removal": restored["verified_pages"],
            "tamper_rejected": True,
        }


def main():
    output = Path(sys.argv[1] if len(sys.argv)>1 else "public_visual_report.json")
    results, failures = [], []
    # Separate processes, never shared MuPDF documents/threads.
    with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(check,spec):spec[0] for spec in SOURCES}
        for job in concurrent.futures.as_completed(jobs):
            try:
                result = job.result()
                results.append(result)
                print(json.dumps(result), flush=True)
            except Exception as exc:
                failure = {"source":jobs[job],"error":repr(exc)}
                failures.append(failure)
                print(json.dumps(failure), flush=True)
    report = {"results":results,"failures":failures,"passed":len(results)==len(SOURCES) and not failures,
              "scope":"rendered-page integrity and replay only; not figure/OCR/LLM quality"}
    output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
