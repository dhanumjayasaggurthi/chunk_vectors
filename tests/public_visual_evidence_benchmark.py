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
import traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_evidence import capture, verify, _paths, digest_file, read_region, read_raster, read_vectors, read_figures, _json_bytes, OpenPageCapture, _source_state


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
        native_validation = None
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
        if name in config.get("native_validation_sources",[]):
            # Exercise the same strict production native parser and table
            # injection on every selected physical page of the actual PDF.
            from diagnose_pdf_tables import validate_recovery
            tick=time.monotonic()
            checked=validate_recovery(str(source),list(range(first,last+1)))
            native_validation={"pages":len(checked),
                               "successful":sum(x["status"]=="SUCCESS" for x in checked),
                               "structured_tables":sum(x["structured_tables"] for x in checked),
                               "deferred_regions":sum(len(x["deferred_candidates"]) for x in checked),
                               "elapsed_seconds":round(time.monotonic()-tick,3),
                               "failures":[{"page":x["page"],"error":x["error"]}
                                           for x in checked if x["status"]!="SUCCESS"]}
            if native_validation["failures"]:
                # Public fixtures only: emit bounded counts/geometry for the
                # exact failed candidates to diagnose rather than relax guards.
                from diagnose_pdf_tables import diagnose
                numbers=[x["page"] for x in native_validation["failures"][:5]]
                print("PUBLIC_NATIVE_FAILURE_DIAGNOSTICS="+json.dumps(
                    {"source":name,"candidates":diagnose(str(source),numbers)}),flush=True)
            assert native_validation["successful"]==last-first+1, native_validation
        kw = dict(settings=config["visual_evidence"], policy_id="public-benchmark", authorized_page_derivatives=True)
        # Exercise the exact native-worker open-handle path on complete public
        # documents, comparing with independently scheduled parallel capture.
        if first==1 and last==count:
            serial_settings={**kw["settings"],"workers":1}
            native_config={"root":str(root/"serial"),"policy_id":kw["policy_id"],
                           "renderer":str(fitz.VersionBind),"authorized_page_derivatives":True,
                           "visual_settings":serial_settings}
            serial_started=time.monotonic()
            serial_state=_source_state(source)
            assert digest_file(source)==source_hash
            observer=OpenPageCapture(source,source_hash,serial_state,count,native_config)
            with fitz.open(source) as doc:
                for i in range(1,count+1):
                    observer.consume(doc,i)
            assert digest_file(source)==source_hash
            baseline=observer.report()
            baseline["elapsed_seconds"]=round(time.monotonic()-serial_started,3)
        else:
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
        selective_result = None
        if config.get("selective_validation"):
            mode = {**kw["settings"], **config["selective_validation"],
                    "selection_mode": "native_text_or_render", "capture_figures":False}
            selected = capture(source, root/"selected", first, last, **{**kw,"settings":mode})
            sid = selected["capture_id"]
            native = fallback = 0
            with fitz.open(source) as doc:
                for i in range(first,last+1):
                    png, manifest = _paths(root/"selected",sid,i)
                    record = json.loads(manifest.read_text(encoding="utf-8"))
                    page = doc[i-1]
                    if record.get("representation") == "native_text_only_candidate":
                        assert not png.exists()
                        # Independently enforce no non-text paint operations.
                        assert all(op[0]=="fill-text" for op in page.get_bboxlog())
                        assert not page.get_image_info()
                        assert not page.get_drawings()
                        assert page.first_annot is None and page.first_widget is None
                        native += 1
                    else:
                        reference, _ = _paths(retained,cid,i)
                        assert digest_file(png) == digest_file(reference)
                        fallback += 1
            assert native+fallback == n
            assert selected["native_text_only_pages"] == native
            assert selected["verified_image_pages"] == fallback
            selected_replay = capture(source, root/"selected",first,last,**{**kw,"settings":mode})
            assert selected_replay["rendered_pages"] == 0
            assert selected_replay["new_native_text_only_pages"] == 0
            assert selected_replay["skipped_verified_pages"] == n
            selective_result = {
                "native_only_candidate_pages": native, "rendered_fallback_pages": fallback,
                "rendered_image_bytes":selected["image_bytes"],
                "capture_seconds":selected["elapsed_seconds"],
                "replay_seconds":selected_replay["elapsed_seconds"],
                "requires_native_reconciliation":bool(native),
            }
        # Store only a bounded number of independent reference pixel buffers,
        # then exercise region access after removing the temporary source PDF.
        region_references = []
        region_count = config.get("region_validation_pages", 0)
        with fitz.open(source) as doc:
            # Spread across physical page range; no assumed caption geometry.
            for i in sorted({first + (last-first)*j//max(1,region_count-1)
                             for j in range(region_count)}):
                page = doc[i-1]
                unrotated = page.rect * page.derotation_matrix
                box = fitz.Rect(unrotated.x0+unrotated.width*.25,
                                unrotated.y0+unrotated.height*.25,
                                unrotated.x0+unrotated.width*.75,
                                unrotated.y0+unrotated.height*.75)
                scale=fitz.Matrix(config["visual_evidence"]["dpi"]/72,
                                  config["visual_evidence"]["dpi"]/72)
                area=(box*page.rotation_matrix*scale).irect
                origin=(page.rect*scale).irect
                pix=page.get_pixmap(dpi=config["visual_evidence"]["dpi"],
                                    colorspace=fitz.csRGB,alpha=False,annots=True)
                x0,y0,x1,y1=area.x0-origin.x0,area.y0-origin.y0,area.x1-origin.x0,area.y1-origin.y0
                expected=b"".join(pix.samples_mv[y*pix.stride+x0*3:y*pix.stride+x1*3]
                                  for y in range(y0,y1))
                import hashlib
                region_references.append((i,list(box),x1-x0,y1-y0,hashlib.sha256(expected).hexdigest()))
        raster_refs, raster_ranges, raster_occurrences, raster_fallbacks = [], [], 0, 0
        raster_config = config.get("raster_validation")
        if raster_config:
            raster_settings = {**kw["settings"],**raster_config["settings"],"capture_rasters":True}
            with fitz.open(source) as doc:
                # Select actual raster-containing pages rather than arbitrary positions.
                raster_pages = (i for i in range(first,last+1) if doc[i-1].get_image_info())
                import itertools
                for i in itertools.islice(raster_pages,raster_config["max_pages"]):
                    report=capture(source,root/"rasters",i,i,**{**kw,"settings":raster_settings})
                    rid=report["capture_id"]
                    raster_ranges.append((rid,i))
                    _,mp=_paths(root/"rasters",rid,i)
                    inventory=json.loads(mp.read_text(encoding="utf-8"))["raster_inventory"]
                    original=doc[i-1].get_image_info(xrefs=True)
                    assert inventory["reported_occurrences"]==len(original)
                    raster_occurrences += len(original)
                    if not inventory["occurrences"]:
                        raster_fallbacks += len(original)
                    for index,entry in enumerate(inventory["occurrences"]):
                        assert entry["bbox"]==list(original[index]["bbox"])
                        if "asset" not in entry:
                            raster_fallbacks += 1
                            continue
                        for is_mask,key in ((False,"asset"),(True,"mask")):
                            item=entry.get(key)
                            if item is not None:
                                import hashlib
                                expected=doc.extract_image(item["xref"])
                                assert item["width"]==expected["width"] and item["height"]==expected["height"]
                                raster_refs.append((rid,i,index,is_mask,hashlib.sha256(expected["image"]).hexdigest()))
        vector_refs = []
        vector_config = config.get("vector_validation")
        if vector_config:
            import hashlib
            import itertools
            vector_settings={**kw["settings"],**vector_config["settings"],"capture_vectors":True}
            with fitz.open(source) as doc:
                vector_candidates=(i for i in range(first,last+1) if doc[i-1].get_drawings())
                for i in itertools.islice(vector_candidates,vector_config["max_pages"]):
                    report=capture(source,root/"vectors",i,i,**{**kw,"settings":vector_settings})
                    vid=report["capture_id"]
                    inv,_=read_vectors(root/"vectors",vid,i,settings=vector_settings)
                    original=doc[i-1].get_cdrawings(extended=True)
                    if inv["status"]=="EXTRACTED_RENDERER_PATHS":
                        assert _json_bytes(inv["paths"])==_json_bytes(original), f"Vector geometry mismatch on {i}"
                        digest=hashlib.sha256(_json_bytes(original)).hexdigest()
                    else:
                        digest=None
                        assert not inv["paths"]
                    vector_refs.append((vid,i,inv["status"],digest,len(original)))
        figure_refs=[]
        figure_ids=set()
        figure_fallback_pages=0
        if config["visual_evidence"].get("capture_figures"):
            from figure_recovery import inventory_page
            with fitz.open(source) as doc:
                for i in range(first,last+1):
                    _,mp=_paths(retained,cid,i)
                    row=json.loads(mp.read_text(encoding="utf-8"))
                    inv=row["figure_inventory"]
                    if inv["status"]!="CAPTION_CANDIDATES_UNVERIFIED":
                        figure_fallback_pages+=1
                        continue
                    expected=inventory_page(doc,i,source_hash=source_hash,include_caption=True,
                                            region_hints=True,
                                            max_distance=config["visual_evidence"]["figure_max_distance_pt"],
                                            padding=config["visual_evidence"]["figure_padding_pt"])
                    assert _json_bytes(inv["candidates"])==_json_bytes(expected)
                    for candidate in expected:
                        figure_ids.add(candidate["figure_number"])
                    if expected:
                        figure_refs.append((i,row["figure_inventory_sha256"]))
            expected_ids=set(config.get("figure_caption_anchors",{}).get(name,[]))
            assert expected_ids<=figure_ids, f"Missing configured public figure anchors: {expected_ids-figure_ids}"
        source.unlink()
        figure_count=figure_hints=figure_crops=0
        for i,digest in figure_refs:
            inv,meta=read_figures(retained,cid,i,settings=config["visual_evidence"])
            assert meta["figure_inventory_sha256"]==digest and not meta["association_verified"]
            figure_count+=inv["candidate_count"]
            for candidate in inv["candidates"]:
                box=candidate["region_hint_bbox"]
                if box is not None:
                    figure_hints+=1
                    if figure_crops<config["region_validation_pages"]:
                        data,provenance=read_region(retained,cid,i,box,settings=config["visual_evidence"])
                        assert data and not provenance["original_required"]
                        figure_crops+=1
        enrichment_pages=0
        enrichment_config=config.get("enrichment_validation")
        if enrichment_config:
            from visual_enrichment import prepare_page,messages,validate_response
            # Test captioned AND uncaptioned pages where available. Use retained
            # evidence only. A mock response is contract testing, not AI quality.
            sample=sorted({first, *[i for i,_ in figure_refs[:enrichment_config["max_caption_pages"]]]})
            for i in sample:
                pkt=prepare_page(retained,cid,i,visual_settings=config["visual_evidence"],
                                 config=enrichment_config["settings"])
                assert len(messages(pkt,enrichment_config["settings"])[1]["content"])==2
                candidates=pkt["context"]["candidates"]
                target=candidates[0]["id"] if candidates else "page"
                quote=candidates[0]["caption"][:20] if candidates else ""
                mock=json.dumps({"observations":[{"target":target,
                    "observation":"Visual evidence requires review","caption_quote":quote,
                    "uncertainty":"limited"}],"limitations":["Mock response only"]})
                result=validate_response(mock,pkt,enrichment_config["settings"])
                assert result["requires_review"] and not result["source_facts_verified"]
                enrichment_pages+=1
        cached_enrichment_pages=0
        if enrichment_config and enrichment_config.get("execution"):
            from visual_enrichment import run_pages
            mock_config={**enrichment_config["settings"],"authorized_provider":True}
            run_config={**enrichment_config["execution"],"authorized_result_storage":True}
            selected=[(cid,i) for i in sample]
            raw=json.dumps({"observations":[{"target":"page","observation":"Visual evidence requires review",
                            "caption_quote":"","uncertainty":"limited"}],"limitations":["Mock response only"]})
            with patch("azure_client.call_visual_structured",return_value=(raw,{})) as api:
                done=list(run_pages(retained,selected,visual_settings=config["visual_evidence"],
                                   config=mock_config,execution=run_config))
                assert len(done)==len(selected)
                assert all(x["status"]=="COMPLETED_UNVERIFIED" for x in done), done
                assert api.call_count==len(selected)
                enrichment_replay=list(run_pages(retained,selected,visual_settings=config["visual_evidence"],
                                     config=mock_config,execution=run_config))
                assert all(x["status"]=="SKIPPED_VERIFIED" for x in enrichment_replay), enrichment_replay
                assert api.call_count==len(selected)
                cached_enrichment_pages=len(enrichment_replay)
        for vid,i,status,digest,path_count in vector_refs:
            inv,meta=read_vectors(root/"vectors",vid,i,settings=vector_settings)
            assert inv["status"]==status and not meta["original_required"]
            if digest is not None:
                import hashlib
                assert hashlib.sha256(_json_bytes(inv["paths"])).hexdigest()==digest
                assert inv["path_count"]==path_count
        for rid,i in raster_ranges:
            assert verify(root/"rasters",rid,i,i,settings=raster_settings)["verified_pages"]==1
        for rid,i,index,is_mask,digest in raster_refs:
            import hashlib
            data,meta=read_raster(root/"rasters",rid,i,index,mask=is_mask,settings=raster_settings)
            assert hashlib.sha256(data).hexdigest()==digest
            assert not meta["original_required"]
        batched_region_matches=0
        batched_region_seconds=0.0
        independent_region_seconds=0.0
        from visual_evidence import _load_region_page, _read_loaded_region
        for i,box,w,h,digest in region_references:
            tick=time.monotonic()
            independent=[read_region(retained,cid,i,b,settings=config["visual_evidence"])
                         for b in (box,None,box)]
            independent_region_seconds+=time.monotonic()-tick
            tick=time.monotonic()
            loaded=_load_region_page(retained,cid,i,config["visual_evidence"])
            grouped=[_read_loaded_region(loaded,b) for b in (box,None,box)]
            batched_region_seconds+=time.monotonic()-tick
            assert grouped==independent, f"Grouped retained crops differ on page {i}"
            batched_region_matches+=len(grouped)
            del loaded,grouped
            output,meta=independent[0]
            pix=fitz.Pixmap(output)
            assert (pix.width,pix.height)==(w,h), f"Region dimensions mismatch {i}"
            assert hashlib.sha256(pix.samples).hexdigest()==digest, f"Region pixel mismatch {i}"
            assert not meta["original_required"] and not meta["writes"]
        if selective_result is not None:
            post = verify(root/"selected",sid,first,last,settings=mode)
            assert post["verified_pages"] == n
            selective_result["verified_after_source_removal"] = n
        restored = verify(retained, cid, first, last, settings=config['visual_evidence'])
        assert restored["verified_pages"] == n
        assert not list(retained.rglob("*.pdf"))
        png, _ = _paths(retained, cid, last)
        with png.open("ab") as stream:
            stream.write(b"tamper")
        try:
            verify(retained, cid, first, last, settings=config['visual_evidence'])
        except ValueError as error:
            assert "checksum/size" in str(error), str(error)
        else:
            raise AssertionError("Tampered evidence was accepted")
        return {
            "source": name, "url": url, "source_sha256": source_hash,
            "source_pages": count, "tested_pages": n, "first": first, "last": last,
            "strict_native_validation":native_validation,
            "pages_with_images": image_pages, "pages_with_paths": vector_pages,
            "pages_with_text": text_pages, "pixel_exact_pages": n,
            "retained_bytes": cold["image_bytes"], "cold_seconds": cold["elapsed_seconds"],
            "serial_seconds":baseline["elapsed_seconds"],"workers":cold["workers"],
            "replay_seconds": replay["elapsed_seconds"], "pixel_check_seconds": pixel_seconds,
            "replay_rendered_pages": replay["rendered_pages"],
            "replay_skipped_pages": replay["skipped_verified_pages"],
            "resume_rendered_pages": resumed["rendered_pages"],
            "verified_after_source_removal": restored["verified_pages"],
            "tamper_rejected": True, "selective":selective_result,
            "post_source_region_pixel_matches":len(region_references),
            "grouped_region_byte_matches":batched_region_matches,
            "independent_region_read_seconds":round(independent_region_seconds,3),
            "grouped_region_read_seconds":round(batched_region_seconds,3),
            "raster_sample_pages":len(raster_ranges),"raster_occurrences":raster_occurrences,
            "raster_fallbacks":raster_fallbacks,"post_source_raster_asset_matches":len(raster_refs),
            "vector_sample_pages":len(vector_refs),
            "post_source_vector_matches":sum(digest is not None for _,_,_,digest,_ in vector_refs),
            "vector_fallback_pages":sum(digest is None for _,_,_,digest,_ in vector_refs),
            "vector_reference_paths":sum(count for _,_,_,_,count in vector_refs),
            "figure_candidates":figure_count,"figure_hint_regions":figure_hints,
            "post_source_figure_crop_reads":figure_crops,"figure_detector_fallback_pages":figure_fallback_pages,
            "figure_anchor_ids":sorted(figure_ids),
            "mock_enrichment_contract_pages":enrichment_pages,
            "mock_enrichment_cache_replay_pages":cached_enrichment_pages,
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
            serialized = json.dumps(result, allow_nan=False)
            results.append(result)
            print(serialized, flush=True)
        except Exception as exc:
            failure = {"source":spec[0],"error":repr(exc),"traceback":traceback.format_exc()}
            failures.append(failure)
            print(json.dumps(failure), flush=True)
    stress = None
    if "long_document_stress" in config:
        try:
            stress = check_long(config["long_document_stress"])
            print(json.dumps(stress), flush=True)
        except Exception as exc:
            failures.append({"source": "synthetic_long_document", "error": repr(exc), "traceback": traceback.format_exc()})
            print(json.dumps(failures[-1]), flush=True)
    report = {"results":results,"long_document_stress":stress,"failures":failures,"passed":len(results)==len(config['sources']) and not failures,
              "scope":"rendered-page integrity and replay only; not figure/OCR/LLM quality"}
    output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
