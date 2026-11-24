"""Public PDF evidence acceptance test; no databases, credentials or cloud AI.

Downloads configured public PDFs, captures selected page
ranges using configured DPI, checks exact rendered pixels, simulates interrupted capture,
verifies no rendering on replay, then removes only the temporary public copy.
This measures retained page evidence, NOT figure segmentation or OCR accuracy.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_evidence import capture, verify, _paths, digest_file


def check(spec, config):
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
                total=config['download_retries'], backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"])))
            with session.get(url, stream=True, timeout=(config['connect_timeout'], config['read_timeout']),
                             headers={"User-Agent": "Public regulatory extraction benchmark/1.0"}) as response:
                response.raise_for_status()
                size = 0
                with source.open("wb") as stream:
                    for block in response.iter_content(1024*1024):
                        size += len(block)
                        if size > config['max_download_bytes']:
                            raise ValueError("Public fixture exceeds download limit")
                        stream.write(block)
        source_hash = digest_file(source)
        with fitz.open(source) as pdf:
            count = pdf.page_count
            last = last or count
            if not first <= last <= count :
                raise ValueError("Unexpected public fixture page range")
            image_pages = vector_pages = text_pages = 0
            for i in range(first-1, last):
                page = pdf[i]
                image_pages += bool(page.get_image_info())
                vector_pages += bool(page.get_drawings())
                text_pages += bool(page.get_text().strip())
        kw = dict(settings=config["visual_evidence"], policy_id="public-benchmark", authorized_page_derivatives=True)
        baseline = capture(source, root/"serial", first, last, **{**kw,"settings":{**kw["settings"],"workers":1}})
        cold = capture(source, retained, first, last, **kw)
        assert baseline["capture_id"] == cold["capture_id"]
        assert baseline["image_bytes"] == cold["image_bytes"]
        n = last-first+1
        assert cold["rendered_pages"] == n and cold["skipped_verified_pages"] == 0
        cid = cold["capture_id"]
        # Pixel-exact comparison against fresh independent MuPDF rendering,
        # including axes, labels, overlays and microscopy in source rasterization.
        started = time.monotonic()
        with fitz.open(source) as pdf:
            for i in range(first, last+1):
                png, manifest = _paths(retained, cid, i)
                serial_png, serial_manifest = _paths(root/"serial", cid, i)
                assert manifest.read_bytes() == serial_manifest.read_bytes(), f"Serial manifest mismatch on page {i}"
                assert digest_file(png) == digest_file(serial_png), f"Serial PNG mismatch on page {i}"
                saved = fitz.Pixmap(str(png))
                expected = pdf[i-1].get_pixmap(dpi=config['visual_evidence']['dpi'], colorspace=fitz.csRGB,
                                                alpha=False, annots=True)
                assert (saved.width, saved.height, saved.n) == (expected.width, expected.height, expected.n)
                assert saved.samples == expected.samples, f"Pixel mismatch on page {i}"
        pixel_seconds = round(time.monotonic()-started,3)
        # Serial replay mock ensures no rendering; parallel replay is also measured.
        with patch.object(fitz.Page, "get_pixmap", side_effect=AssertionError("re-render")):
            serial_replay = capture(source, retained, first, last, **{**kw, "settings":{**kw["settings"],"workers":1}})
        replay = capture(source, retained, first, last, **kw)
        assert serial_replay["rendered_pages"] == 0
        assert replay["skipped_verified_pages"] == n and replay["rendered_pages"] == 0
        assert not replay["derivative_writes"]
        # Crash between publishing PNG and manifest: replay just that page.
        _, manifest = _paths(retained, cid, first)
        manifest.unlink()
        resumed = capture(source, retained, first, last, **kw)
        assert resumed["rendered_pages"] == 1 and resumed["skipped_verified_pages"] == n-1
        assert digest_file(source) == source_hash
        source.unlink()
        restored = verify(retained, cid, first, last, settings=config['visual_evidence'])
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
            "serial_seconds":baseline["elapsed_seconds"],"workers":cold["workers"],
            "replay_seconds": replay["elapsed_seconds"], "pixel_check_seconds": pixel_seconds,
            "replay_rendered_pages": replay["rendered_pages"],
            "replay_skipped_pages": replay["skipped_verified_pages"],
            "resume_rendered_pages": resumed["rendered_pages"],
            "verified_after_source_removal": restored["verified_pages"],
            "tamper_rejected": True,
        }



def check_long(config):
    """Stress scheduling and resume across >3,000 small synthetic pages.

    This is deliberately a small-page stress fixture, not a substitute for
    a large, complex, real regulatory document or a fleet/RSS benchmark.
    """
    import fitz
    count = config["pages"]
    settings = config["visual_evidence"]
    if type(count) is not int or count < 3001:
        raise ValueError("Long document acceptance needs at least 3001 pages")
    with tempfile.TemporaryDirectory(prefix="long-visual-") as tmp:
        root = Path(tmp)
        source = root / "long.pdf"
        retained = root / "retained"
        with fitz.open() as doc:
            for i in range(count):
                page = doc.new_page(width=config["page_width_pt"], height=config["page_height_pt"])
                page.insert_text((8, 18), str(i + 1), fontsize=8)
                page.draw_line((8, 30), (40, 45))
            doc.save(source)
        kw = dict(settings=settings, policy_id="public-benchmark",
                  authorized_page_derivatives=True)
        initial = capture(source, retained, 1, count, **kw)
        assert initial["rendered_pages"] == count
        cid = initial["capture_id"]
        batch = settings["batch_pages"]
        missing = sorted({1, min(batch, count), min(batch + 1, count), count})
        for page in missing:
            _, manifest = _paths(retained, cid, page)
            manifest.unlink()
        resumed = capture(source, retained, 1, count, **kw)
        assert resumed["rendered_pages"] == len(missing)
        assert resumed["skipped_verified_pages"] == count - len(missing)
        replay = capture(source, retained, 1, count, **kw)
        assert replay["rendered_pages"] == 0
        assert replay["skipped_verified_pages"] == count
        with fitz.open(source) as doc:
            for page in missing:
                png, _ = _paths(retained, cid, page)
                actual = fitz.Pixmap(str(png))
                expected = doc[page-1].get_pixmap(dpi=settings["dpi"],
                            colorspace=fitz.csRGB, alpha=False, annots=True)
                assert actual.samples == expected.samples
        source.unlink()
        restored = verify(retained, cid, 1, count, settings=settings)
        assert restored["verified_pages"] == count
        return {
            "source": "synthetic_long_document", "source_pages": count,
            "verified_after_source_removal": restored["verified_pages"],
            "cold_seconds": initial["elapsed_seconds"],
            "resume_seconds": resumed["elapsed_seconds"],
            "replay_seconds": replay["elapsed_seconds"],
            "replay_rendered_pages": replay["rendered_pages"],
            "replay_skipped_pages": replay["skipped_verified_pages"],
            "resume_rendered_pages": resumed["rendered_pages"],
            "resume_missing_pages": missing, "workers": settings["workers"],
            "batch_pages": batch, "retained_bytes": initial["image_bytes"],
            "scope": "synthetic small pages: scheduler and reconciliation only",
        }


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: public_visual_evidence_benchmark.py CONFIG REPORT")
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(sys.argv[2])
    results, failures = [], []
    # Do not nest document pools and page pools. Benchmark fixed page-pool cap
    # per document, measuring serial vs parallel on the same runner.
    for spec in config["sources"]:
        try:
            result = check(spec,config)
            results.append(result)
            print(json.dumps(result), flush=True)
        except Exception as exc:
            failure = {"source":spec[0],"error":repr(exc)}
            failures.append(failure)
            print(json.dumps(failure), flush=True)
    stress = None
    if "long_document_stress" in config:
        try:
            stress = check_long(config["long_document_stress"])
            print(json.dumps(stress), flush=True)
        except Exception as exc:
            failures.append({"source": "synthetic_long_document", "error": repr(exc)})
            print(json.dumps(failures[-1]), flush=True)
    report = {"results":results,"long_document_stress":stress,"failures":failures,"passed":len(results)==len(config['sources']) and not failures,
              "scope":"rendered-page integrity and replay only; not figure/OCR/LLM quality"}
    output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
