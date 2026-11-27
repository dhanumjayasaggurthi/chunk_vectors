"""Offline privacy-safe synthetic tests for fragmented-figure reconstruction."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path


class FigureRecoveryTests(unittest.TestCase):
    def _fixture(self, folder: Path, *, gap: float = 0):
        import fitz
        from PIL import Image, ImageDraw
        path = folder / "seven-chart-strips.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=612, height=792)
            page.insert_text((60, 85), "Figure 14: Synthetic Kaplan-Meier chart")
            for index in range(7):
                canvas = Image.new("RGB", (480, 36), "white")
                draw = ImageDraw.Draw(canvas)
                draw.line((0, 18, 479, 16 + index), fill="black", width=2)
                if index == 0:
                    draw.text((14, 2), "ORIGINAL SYNTHETIC FIGURE", fill="black")
                source = io.BytesIO()
                canvas.save(source, format="PNG")
                top = 100.0 + index * (32.0 + gap)
                page.insert_image(
                    fitz.Rect(93.5, top, 539.9, top + 32.0),
                    stream=source.getvalue(),
                )
            page.insert_text((70, 430), "Source narrative remains independently native text.")
            pdf.save(path)
        return path

    def test_adjacent_strips_one_candidate_not_seven(self):
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            with fitz.open(path) as pdf:
                groups = find_figure_groups(pdf[0])
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]), 7)
            self.assertEqual(len(set(f["xref"] for f in groups[0])), 7)
            self.assertEqual(
                [f["bbox"][1] for f in groups[0]], sorted(f["bbox"][1] for f in groups[0])
            )

    def test_nonadjacent_strips_not_merged(self):
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp), gap=8.0)
            with fitz.open(path) as pdf:
                groups = find_figure_groups(pdf[0])
            self.assertEqual(groups, [])

    def test_evidence_immutable_repeatable_and_never_approved(self):
        from figure_recovery import prepare_page
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = self._fixture(folder)
            out = folder / "evidence"
            result = prepare_page(
                path, 1, out, native_extraction_hash="native-test-version"
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["fragments"], 7)
            self.assertEqual(result[0]["review_status"], "VISUAL_REVIEW_REQUIRED")
            manifest_path = Path(result[0]["manifest"])
            evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["interpretation_status"], "NOT_PERFORMED")
            self.assertEqual(evidence["fragment_count"], 7)
            self.assertEqual(evidence["native_extraction_hash"], "native-test-version")
            self.assertIsNotNone(evidence["caption_bbox"])
            self.assertLess(evidence["render_clip_bbox"][0], evidence["caption_bbox"][0])
            self.assertLess(evidence["render_clip_bbox"][1], evidence["caption_bbox"][1])
            self.assertEqual(
                evidence["source_pdf_sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                hashlib.sha256(Path(result[0]["png"]).read_bytes()).hexdigest(),
                evidence["composite_sha256"],
            )
            self.assertEqual(
                len({f["original_sha256"] for f in evidence["source_fragments"]}), 7,
            )
            # Native input and independently versioned visual result remain stable.
            self.assertEqual(result, prepare_page(path, 1, out, native_extraction_hash="native-test-version"))
            with manifest_path.open("ab") as f:
                f.write(b"tamper")
            with self.assertRaisesRegex(RuntimeError, "Existing evidence differs"):
                prepare_page(path, 1, out, native_extraction_hash="native-test-version")

    def test_render_and_group_limits_fail_closed(self):
        import fitz
        from figure_recovery import FigureSettings, find_figure_groups, prepare_page
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = self._fixture(folder)
            with fitz.open(path) as pdf:
                with self.assertRaisesRegex(ValueError, "PAGE_IMAGE_COUNT_LIMIT"):
                    find_figure_groups(pdf[0], FigureSettings(max_page_images=6, max_fragments=6))
            with self.assertRaisesRegex(ValueError, "FIGURE_RENDER_PIXEL_BUDGET_EXCEEDED"):
                prepare_page(
                    path, 1, folder / "blocked",
                    settings=FigureSettings(max_render_pixels=100000),
                )
            with self.assertRaisesRegex(ValueError, "Invalid figure setting"):
                FigureSettings(dpi=1000).validate()

    def test_no_modification_to_native_extraction_profile(self):
        from processing_profile import load_profile
        from page_stage import extraction_fingerprint
        profile = load_profile("profiles/regulatory_page_stage_example.yml")
        before = extraction_fingerprint(profile)
        import fitz
        from figure_recovery import find_figure_groups
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            with fitz.open(path) as pdf:
                self.assertEqual(len(find_figure_groups(pdf[0])), 1)
        self.assertEqual(before, extraction_fingerprint(profile))


    def test_read_only_inventory_preserves_vector_plot_caption_and_source_identity(self):
        """Caption-first PBRER-style figure is not an embedded PDF image."""
        import fitz
        from figure_recovery import inventory_page
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"vector-figure.pdf"
            with fitz.open() as doc:
                page=doc.new_page(width=612,height=792)
                page.insert_text((60,70),
                                 "Figure 1: Worldwide reporting rates and intervals")
                page.insert_text((60,84),"of spontaneous reports in five periods")
                for x in range(130,450,45):
                    page.draw_line(fitz.Point(x,160),fitz.Point(x,420))
                    page.draw_line(fitz.Point(x-8,220),fitz.Point(x+8,220))
                for y in range(160,421,50):
                    page.draw_line(fitz.Point(130,y),fitz.Point(445,y))
                page.draw_polyline([
                    fitz.Point(150,300),fitz.Point(220,270),
                    fitz.Point(300,320),fitz.Point(390,310),
                ])
                page.insert_text((60,480),"The native safety narrative remains")
                doc.save(path)
            original=path.read_bytes()
            source=hashlib.sha256(original).hexdigest()
            with fitz.open(path) as doc:
                self.assertEqual(doc[0].get_image_info(xrefs=True),[])
                candidates=inventory_page(doc,1,source_hash=source)
                self.assertEqual(len(candidates),1)
                fig=candidates[0]
                self.assertEqual(fig["figure_number"],"1")
                self.assertEqual(fig["source_pdf_sha256"],source)
                self.assertEqual(fig["visual_kind"],"vector_drawing_candidate")
                self.assertGreater(fig["nearby_vector_drawing_paths"],5)
                self.assertEqual(fig["nearby_embedded_images"],[])
                self.assertEqual(fig["interpretation_status"],"NOT_PERFORMED")
                self.assertEqual(fig["association_status"],
                                 "GEOMETRY_NEAR_CAPTION_UNVERIFIED")
                self.assertNotIn("caption_native_text",fig)
                shown=inventory_page(doc,1,source_hash=source,
                                     include_caption=True)
                self.assertIn("Worldwide reporting rates",
                              shown[0]["caption_native_text"])
                self.assertEqual(fig["figure_candidate_id"],
                                 shown[0]["figure_candidate_id"])
                with self.assertRaisesRegex(ValueError,"source PDF SHA-256"):
                    inventory_page(doc,1,source_hash="x")
            self.assertEqual(path.read_bytes(),original)

    def test_read_only_inventory_multi_panel_microscopy_with_caption_below(self):
        """Multiple raster panels are preserved as source image references."""
        import fitz
        from PIL import Image,ImageDraw
        from figure_recovery import inventory_page,main
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"microscopy.pdf"
            panel=Image.new("RGB",(140,120),"white")
            ImageDraw.Draw(panel).ellipse((20,20,115,100),outline="black",width=5)
            out=io.BytesIO()
            panel.save(out,format="PNG")
            with fitz.open() as doc:
                page=doc.new_page(width=612,height=792)
                page.insert_image(fitz.Rect(70,150,210,270),
                                  stream=out.getvalue())
                page.insert_image(fitz.Rect(240,150,380,270),
                                  stream=out.getvalue())
                page.insert_text((75,295),
                                 "Fig. 7. Scanning electron micrographs of vials")
                page.insert_text((70,460),"Source discussion paragraphs stay native.")
                doc.save(path)
            data=path.read_bytes()
            source=hashlib.sha256(data).hexdigest()
            with fitz.open(path) as pdf:
                records=inventory_page(pdf,1,source_hash=source)
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]["figure_number"],"7")
            self.assertIn(records[0]["visual_kind"],(
                "embedded_raster_candidate","mixed_raster_vector_candidate"))
            self.assertEqual(len(records[0]["nearby_embedded_images"]),2)
            self.assertEqual(records[0]["writes"],False)
            output=io.StringIO()
            with redirect_stdout(output):
                main(["inventory","--file",str(path),"--page","1"])
            cli=json.loads(output.getvalue())
            self.assertFalse(cli["writes"])
            self.assertFalse(cli["cloud_calls"])
            self.assertEqual(cli["figure_candidates"],1)
            self.assertEqual(cli["source_file_sha256"],source)
            self.assertNotIn("caption_native_text",cli["candidates"][0])
            self.assertEqual(path.read_bytes(),data)



    def test_inventory_reports_actual_physical_page_count_not_printed_labels(self):
        from figure_recovery import main
        with tempfile.TemporaryDirectory() as tmp:
            path=self._fixture(Path(tmp))
            with self.assertRaisesRegex(
                ValueError,"requested physical pages 117-129 but this PDF has 1"
            ):
                main([
                    "inventory","--file",str(path),
                    "--page","117","--end-page","129",
                ])



    def test_full_page_background_is_not_misassigned_to_every_figure(self):
        """The original journal PDF has full-page images plus figure panels."""
        import fitz
        from PIL import Image,ImageDraw
        from figure_recovery import inventory_page,main
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"ten-page-article.pdf"
            full=Image.new("RGB",(400,600),"white")
            panel=Image.new("RGB",(120,80),"white")
            ImageDraw.Draw(panel).line((0,40,120,40),fill="black",width=2)
            bg_bytes=io.BytesIO()
            panel_bytes=io.BytesIO()
            full.save(bg_bytes,format="PNG")
            panel.save(panel_bytes,format="PNG")
            with fitz.open() as pdf:
                for n in range(10):
                    p=pdf.new_page(width=612,height=792)
                    if n!=2:
                        continue
                    p.insert_image(p.rect,stream=bg_bytes.getvalue())
                    p.insert_image(fitz.Rect(70,100,220,200),
                                   stream=panel_bytes.getvalue())
                    p.insert_text((60,285),"Figure 1: Real native caption")
                    p.insert_text((60,330),"Figure 1 shows trends in this study")
                    p.insert_text((60,360),"Figure 2. Second native caption")
                pdf.set_page_labels([{
                    "startpage":0,"prefix":"","style":"D","firstpagenum":1340,
                }])
                pdf.save(path)
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            with fitz.open(path) as pdf:
                records=inventory_page(pdf,3,source_hash=sha)
            self.assertEqual(len(records),2)
            self.assertEqual([r["figure_number"] for r in records],["1","2"])
            self.assertEqual(records[0]["printed_page_label"],"1342")
            self.assertEqual(records[0]["visual_kind"],"embedded_raster_candidate")
            self.assertEqual(len(records[0]["nearby_embedded_images"]),1)
            self.assertEqual(len(records[0]["page_spanning_images"]),1)
            self.assertFalse(records[0]["writes"])
            self.assertEqual(records[1]["source_pdf_sha256"],sha)
            output=io.StringIO()
            with redirect_stdout(output):
                main(["inventory","--file",str(path),"--page","1",
                      "--end-page","10","--compact"])
            summary=json.loads(output.getvalue())
            self.assertEqual(summary["figure_candidates"],2)
            self.assertEqual(summary["by_visual_kind"],{
                "embedded_raster_candidate":2,
            })
            self.assertEqual(summary["figures"][0]["printed_page_label"],"1342")
            self.assertEqual(summary["figures"][0]["nearby_images"],1)
            self.assertEqual(
                summary["figures"][0]["page_spanning_images_unassigned"],1)
            self.assertNotIn("caption_native_text",summary["figures"][0])
            self.assertFalse(summary["writes"])
            self.assertFalse(summary["cloud_calls"])




class DerivativePageEvidenceTests(unittest.TestCase):
    def test_retained_figure_candidates_ambiguous_and_source_independent(self):
        import fitz
        from visual_evidence import capture,read_figures,read_region,verify,_paths
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            source=folder/"figures.pdf"
            with fitz.open() as doc:
                p=doc.new_page(width=300,height=400)
                p.draw_rect((40,60,130,150))
                p.draw_rect((150,60,240,150))
                p.insert_text((40,180),"Figure 1: Two panels")
                p.insert_text((40,240),"Figure 1 shows the result")
                p.set_rotation(90)
                p=doc.new_page(width=300,height=400)
                p.draw_rect((40,80,240,200))
                p.insert_text((40,60),"Figure 2: Upper caption")
                p.insert_text((40,230),"Figure 3: Lower caption")
                doc.save(source)
            settings=dict(dpi=150,capture_figures=True,workers=1)
            root=folder/"retained"
            result=capture(source,root,1,2,settings=settings,policy_id="test",
                           authorized_page_derivatives=True)
            cid=result["capture_id"]
            self.assertEqual(capture(source,root,1,2,settings=settings,policy_id="test",
                                      authorized_page_derivatives=True)["skipped_verified_pages"],2)
            source.unlink()
            inv,meta=read_figures(root,cid,1,settings=settings)
            self.assertEqual(inv["candidate_count"],1)
            first=inv["candidates"][0]
            self.assertEqual(first["figure_number"],"1")
            self.assertEqual(first["caption_native_text"],"Figure 1: Two panels")
            self.assertEqual(first["region_hint_status"],"UNVERIFIED_REGION_HINT")
            self.assertFalse(meta["association_verified"])
            data,_=read_region(root,cid,1,first["region_hint_bbox"],settings=settings)
            self.assertTrue(data.startswith(b"\x89PNG"))
            inv,_=read_figures(root,cid,2,settings=settings)
            self.assertEqual(inv["candidate_count"],2)
            self.assertTrue(all(c["region_hint_bbox"] is None for c in inv["candidates"]))
            _,manifest=_paths(root,cid,1)
            row=json.loads(manifest.read_text(encoding="utf-8"))
            row["figure_inventory"]["candidates"][0]["caption_native_text"]="fabricated"
            manifest.write_text(json.dumps(row),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"Figure inventory checksum"):
                verify(root,cid,1,2,settings=settings)

    def test_vector_paths_retention_limits_and_post_source_access(self):
        import fitz
        from visual_evidence import capture, read_vectors, verify, _paths, _json_bytes
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            source=folder/"vector.pdf"
            with fitz.open() as doc:
                p=doc.new_page(width=200,height=200)
                p.draw_line((10,10),(10,90),color=(0,0,0))
                p.draw_line((10,90),(100,90),color=(0,0,0))
                p.draw_bezier((10,80),(30,30),(60,90),(100,20),color=(1,0,0))
                p.draw_rect(fitz.Rect(40,40,60,60),fill=(0,1,0),fill_opacity=0.5)
                p.insert_text((10,110),"Figure 1: synthetic graph")
                p.set_rotation(90)
                doc.save(source)
            with fitz.open(source) as doc:
                expected=doc[0].get_cdrawings(extended=True)
            settings=dict(dpi=150,capture_vectors=True,workers=1)
            kw=dict(policy_id="test",authorized_page_derivatives=True)
            root=folder/"out"
            normal=capture(source,root,1,1,settings=settings,**kw)
            cid=normal["capture_id"]
            self.assertEqual(capture(source,root,1,1,settings=settings,**kw)["skipped_verified_pages"],1)
            limited={**settings,"max_vector_paths":1}
            reduced=capture(source,root,1,1,settings=limited,**kw)
            self.assertNotEqual(reduced["capture_id"],cid)
            source.unlink()
            inventory,meta=read_vectors(root,cid,1,settings=settings)
            self.assertEqual(inventory["status"],"EXTRACTED_RENDERER_PATHS")
            self.assertEqual(_json_bytes(inventory["paths"]),_json_bytes(expected))
            self.assertFalse(meta["original_required"])
            fallback,_=read_vectors(root,reduced["capture_id"],1,settings=limited)
            self.assertEqual(fallback["status"],"PATH_LIMIT_RENDER_FALLBACK")
            self.assertEqual(fallback["paths"],[])
            self.assertEqual(verify(root,cid,1,1,settings=settings)["verified_pages"],1)
            _,manifest=_paths(root,cid,1)
            row=json.loads(manifest.read_text(encoding="utf-8"))
            row["vector_inventory"]["paths"].pop()
            manifest.write_text(json.dumps(row),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"Vector inventory checksum"):
                verify(root,cid,1,1,settings=settings)

    def test_native_raster_retention_dedup_mask_and_source_removal(self):
        import fitz
        from PIL import Image
        from visual_evidence import capture, verify, read_raster, _paths, _raster_path
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            source=folder/"raster.pdf"
            buffer=io.BytesIO()
            image=Image.new("RGBA",(320,240),(200,10,30,120))
            image.save(buffer,format="PNG")
            with fitz.open() as doc:
                p=doc.new_page(width=180,height=180)
                xref=p.insert_image(fitz.Rect(10,10,70,60),stream=buffer.getvalue())
                p.insert_image(fitz.Rect(80,10,140,60),xref=xref)
                p=doc.new_page(width=180,height=180)
                p.insert_image(fitz.Rect(10,10,70,60),xref=xref)
                doc.save(source)
            with fitz.open(source) as doc:
                expected=doc.extract_image(xref)
                mask=doc.extract_image(expected["smask"])["image"]
            settings=dict(dpi=150,workers=2,batch_pages=1,max_in_flight=2,capture_rasters=True)
            kw=dict(settings=settings,policy_id="test",authorized_page_derivatives=True)
            root=folder/"out"
            result=capture(source,root,1,2,**kw)
            cid=result["capture_id"]
            self.assertEqual(len(list(root.rglob("*.bin"))),2)
            self.assertEqual(capture(source,root,1,2,**kw)["skipped_verified_pages"],2)
            source.unlink()
            self.assertEqual(verify(root,cid,1,2,settings=settings)["verified_pages"],2)
            for number,index in ((1,0),(1,1),(2,0)):
                data,meta=read_raster(root,cid,number,index,settings=settings)
                self.assertEqual(data,expected["image"])
                self.assertEqual(meta["asset"]["width"],320)
                self.assertEqual(read_raster(root,cid,number,index,mask=True,settings=settings)[0],mask)
            _,meta=read_raster(root,cid,1,0,settings=settings)
            with _raster_path(root,cid,meta["asset"]["sha256"]).open("ab") as stream:
                stream.write(b"bad")
            with self.assertRaisesRegex(ValueError,"Raster asset checksum"):
                verify(root,cid,1,2,settings=settings)

    def test_native_evidence_profiling_preserves_bytes_and_replay_checks(self):
        import fitz
        from visual_evidence import OpenPageCapture, _source_state, digest_file, _paths
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "profile.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page(width=180, height=180)
                page.insert_text((15,30),"Regulatory source evidence")
                page.draw_line((15,50),(120,50))
                pdf.save(source)
            state = _source_state(source)
            sha = digest_file(source)
            settings = dict(dpi=150,capture_rasters=True,capture_vectors=True,capture_figures=True)
            def run(root, enabled):
                config = dict(root=str(root),policy_id="test",renderer=str(fitz.VersionBind),
                              visual_settings=settings,authorized_page_derivatives=True)
                with fitz.open(source) as pdf:
                    capture = OpenPageCapture(source,sha,state,pdf.page_count,config,
                                              profile_timings=enabled)
                    capture.consume(pdf,1)
                    return capture.report()
            ordinary = run(folder/"ordinary",False)
            measured = run(folder/"measured",True)
            self.assertNotIn("timings_seconds",ordinary)
            self.assertEqual(ordinary["capture_id"],measured["capture_id"])
            cid = measured["capture_id"]
            self.assertEqual(ordinary["image_bytes"],measured["image_bytes"])
            def tree(root):
                return {p.relative_to(root):digest_file(p) for p in root.rglob("*") if p.is_file()}
            expected = tree(folder/"ordinary")
            self.assertEqual(expected,tree(folder/"measured"))
            timings = measured["timings_seconds"]
            for key in ("render","png_encode","png_write","manifest_write","image_checksum",
                        "raster_capture_including_asset_writes","vector_inventory","figure_inventory",
                        "fresh_verify_manifest_read","fresh_verify_image_checksum",
                        "fresh_verify_image_decode","fresh_verify_raster_inventory",
                        "fresh_verify_vector_inventory","fresh_verify_figure_inventory","other_capture"):
                self.assertIn(key,timings)
                self.assertGreaterEqual(timings[key],0)
            self.assertAlmostEqual(sum(timings.values()),measured["elapsed_seconds"],delta=0.002)
            self.assertGreaterEqual(measured["reconciliation_seconds"],0)
            replay = run(folder/"measured",True)
            self.assertEqual(replay["rendered_pages"],0)
            self.assertEqual(replay["skipped_verified_pages"],1)
            self.assertNotIn("render",replay["timings_seconds"])
            self.assertNotIn("png_write",replay["timings_seconds"])
            self.assertIn("replay_verify_image_decode",replay["timings_seconds"])
            self.assertIn("replay_verify_image_checksum",replay["timings_seconds"])
            self.assertEqual(tree(folder/"measured"),expected)
            png,_ = _paths(folder/"measured",cid,1)
            with png.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError,"checksum/size mismatch"):
                run(folder/"measured",True)

    def test_retained_region_exact_pixels_rotation_and_source_removal(self):
        import fitz
        import hashlib
        from visual_evidence import capture, read_region, _paths
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "rotation.pdf"
            with fitz.open() as doc:
                for angle in (0,90,180,270):
                    page = doc.new_page(width=240,height=160)
                    page.draw_rect(fitz.Rect(30,40,100,90), fill=(1,0,0))
                    page.insert_text((35,60),"Panel A")
                    page.set_rotation(angle)
                doc.save(source)
            settings = dict(dpi=150, workers=1)
            root = folder/"retained"
            result = capture(source,root,1,4,settings=settings,policy_id="test",
                             authorized_page_derivatives=True)
            cid = result["capture_id"]
            # Compare returned crop to source-rendered page pixels independently.
            box = fitz.Rect(30,40,100,90)
            references = []
            with fitz.open(source) as doc:
                for page in doc:
                    pix=page.get_pixmap(dpi=150,colorspace=fitz.csRGB,alpha=False)
                    area=(box*page.rotation_matrix*fitz.Matrix(150/72,150/72)).irect
                    rows=[pix.samples_mv[y*pix.stride+area.x0*3:y*pix.stride+area.x1*3]
                          for y in range(area.y0,area.y1)]
                    references.append((area.width,area.height,b"".join(rows)))
            source.unlink()
            for number,(w,h,expected) in enumerate(references,1):
                data,meta=read_region(root,cid,number,list(box),settings=settings)
                actual=fitz.Pixmap(data)
                self.assertEqual((actual.width,actual.height),(w,h))
                self.assertEqual(actual.samples,expected)
                self.assertEqual(meta["region_sha256"],hashlib.sha256(data).hexdigest())
                self.assertFalse(meta["writes"])
                full,fullmeta=read_region(root,cid,number,settings=settings)
                png,_=_paths(root,cid,number)
                self.assertEqual(full,png.read_bytes())
                self.assertEqual(fullmeta["region_sha256"],fullmeta["parent_image_sha256"])
            with self.assertRaisesRegex(ValueError,"outside"):
                read_region(root,cid,1,[0,0,999,999],settings=settings)
            with self.assertRaises(ValueError):
                read_region(root,cid,1,[0,0,float("nan"),1],settings=settings)
            png,_=_paths(root,cid,1)
            with png.open("ab") as stream:
                stream.write(b"bad")
            with self.assertRaisesRegex(ValueError,"checksum"):
                read_region(root,cid,1,list(box),settings=settings)

    def test_selective_native_payload_and_complex_render_fallback(self):
        import fitz
        from visual_evidence import capture, verify, _paths
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "selective.pdf"
            with fitz.open() as doc:
                plain = doc.new_page()
                plain.insert_text((40,40), "Ordinary native text without a drawing")
                vector = doc.new_page()
                vector.insert_text((40,40), "Text next to a vector graph")
                vector.draw_line((20,80),(180,100))
                raster = doc.new_page()
                pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0,0,20,20), False)
                pix.clear_with(200)
                raster.insert_image(fitz.Rect(10,10,100,100),pixmap=pix)
                hidden = doc.new_page()
                hidden.insert_text((40,40), "Invisible OCR layer", render_mode=3)
                hidden.insert_text((40,70), "Visible text")
                doc.new_page()
                doc.save(source)
            settings = dict(dpi=150, workers=2, batch_pages=2, max_in_flight=2,
                            selection_mode="native_text_or_render", min_native_chars=10)
            kw = dict(policy_id="test", authorized_page_derivatives=True)
            result = capture(source,folder/"out",1,5,settings=settings,**kw)
            self.assertEqual(result["rendered_pages"],4)
            self.assertEqual(result["native_text_only_pages"],1)
            self.assertEqual(result["new_native_text_only_pages"],1)
            self.assertTrue(result["native_reconciliation_required"])
            png, manifest = _paths(folder/"out",result["capture_id"],1)
            self.assertFalse(png.exists())
            data=json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIn("Ordinary native text",json.dumps(data["native_page"]))
            replay = capture(source,folder/"out",1,5,settings=settings,**kw)
            self.assertEqual(replay["rendered_pages"],0)
            self.assertEqual(replay["skipped_verified_pages"],5)
            self.assertEqual(replay["native_text_only_pages"],1)
            self.assertFalse(replay["derivative_writes"])
            all_pages = capture(source,folder/"out",1,5,
                                settings={**settings,"selection_mode":"all_pages"},**kw)
            self.assertNotEqual(all_pages["capture_id"],result["capture_id"])
            self.assertEqual(all_pages["rendered_pages"],5)
            source.unlink()
            self.assertEqual(verify(folder/"out",result["capture_id"],1,5,
                                    settings=settings)["verified_image_pages"],4)
            data["native_page"]["blocks"][0]["lines"][0]["spans"][0]["text"]="tamper"
            manifest.write_text(json.dumps(data),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"checksum"):
                verify(folder/"out",result["capture_id"],1,5,settings=settings)

    def test_profile_parallel_replay_and_runtime_identity(self):
        import fitz
        from visual_evidence import capture, verify, load_visual_profile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._fixture(folder)
            profile = folder / "profile.json"
            settings = dict(dpi=150, workers=2, batch_pages=1, max_in_flight=2)
            profile.write_text(json.dumps({"version":1,"visual_evidence":settings}))
            kw = dict(policy_id="test", authorized_page_derivatives=True)
            parallel = capture(source, folder/"out", 1, 3,
                               settings=load_visual_profile(profile), **kw)
            self.assertEqual(parallel["rendered_pages"],3)
            serial = capture(source,folder/"out",1,3,
                             settings={**settings,"workers":1,"batch_pages":3},**kw)
            self.assertEqual(serial["capture_id"],parallel["capture_id"])
            self.assertEqual(serial["rendered_pages"],0)
            self.assertEqual(serial["skipped_verified_pages"],3)
            source.unlink()
            self.assertEqual(verify(folder/"out",parallel["capture_id"],1,3,
                                    settings=settings)["verified_pages"],3)
            for bad in ({"workers":0},{"workers":3,"max_in_flight":2},
                        {"max_pixels":True},{"unknown":1}):
                profile.write_text(json.dumps({"version":1,"visual_evidence":bad}))
                with self.assertRaises(ValueError):
                    load_visual_profile(profile)

    def test_resume_skips_verified_pages_without_rendering_and_repairs_orphan(self):
        import fitz
        from unittest.mock import patch
        from visual_evidence import capture, _paths
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._fixture(folder)
            root = folder / "retained"
            kw = dict(dpi=150, policy_id="test", authorized_page_derivatives=True)
            first = capture(source, root, 1, 2, **kw)
            self.assertEqual(first["rendered_pages"], 2)
            self.assertEqual(first["skipped_verified_pages"], 0)
            with patch.object(fitz.Page, "get_pixmap", side_effect=AssertionError("re-render")):
                replay = capture(source, root, 1, 2, **kw)
            self.assertEqual(replay["skipped_verified_pages"], 2)
            self.assertEqual(replay["rendered_pages"], 0)
            self.assertFalse(replay["derivative_writes"])
            png, manifest = _paths(root, first["capture_id"], 2)
            manifest.unlink()
            recovered = capture(source, root, 1, 3, **kw)
            self.assertEqual(recovered["rendered_pages"], 2)
            self.assertEqual(recovered["skipped_verified_pages"], 1)
            with png.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(ValueError, "checksum/size"):
                capture(source, root, 1, 3, **kw)

    def test_derivative_module_runs_without_archive_dependency(self):
        """Replicate the Windows validation folder containing only this module."""
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            module = Path(__file__).resolve().parents[1] / "visual_evidence.py"
            isolated = folder / module.name
            shutil.copyfile(module, isolated)
            result = subprocess.run(
                [sys.executable, "-I", str(isolated), "--help"],
                cwd=folder, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--authorized-page-derivatives", result.stdout)
            self.assertFalse((folder / "source_evidence_archive.py").exists())

    def _fixture(self, folder):
        import fitz
        from PIL import Image, ImageDraw
        source = folder / "mixed.pdf"
        image = Image.new("RGB", (200, 120), "white")
        ImageDraw.Draw(image).ellipse((15, 15, 180, 100), outline="black", width=5)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        with fitz.open() as doc:
            page = doc.new_page(width=612, height=792)
            page.insert_text((60, 70), "Figure 1: Reporting rates by interval")
            for x in range(100, 500, 80):
                page.draw_line((x, 130), (x, 280))
                page.draw_line((x-8, 130), (x+8, 130))
                page.draw_line((x-8, 280), (x+8, 280))
            page.draw_polyline([(100,220),(180,190),(260,230),(340,180),(420,200)])
            page = doc.new_page(width=612, height=792)
            for x in (60, 230, 400):
                page.insert_image(fitz.Rect(x, 100, x+150, 220), stream=stream.getvalue())
            page.insert_text((60, 250), "Fig. 2. Three microscopy panels")
            page.set_rotation(90)
            page = doc.new_page(width=612, height=792)
            page.insert_image(page.rect, stream=stream.getvalue())
            doc.set_page_labels([{"startpage":0,"style":"D","firstpagenum":1340}])
            doc.save(source)
        return source

    def test_derivative_evidence_survives_source_removal_and_replay(self):
        from visual_evidence import capture, verify, _paths
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._fixture(folder)
            root = folder / "retained"
            original = source.read_bytes()
            kwargs = dict(dpi=150, policy_id="synthetic-test",
                          authorized_page_derivatives=True)
            result = capture(source, root, 1, 3, **kwargs)
            self.assertEqual(result["verified_pages"], 3)
            self.assertFalse(result["purge_safe"])
            self.assertFalse(result["original_copied"])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(result["capture_id"], capture(source,root,1,3,**kwargs)["capture_id"])
            self.assertEqual(len(list(root.rglob("*.png"))),3)
            self.assertEqual(len(list(root.rglob("*.json"))),3)
            self.assertEqual(list(root.rglob("*.pdf")),[])
            png, manifest = _paths(root,result["capture_id"],2)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(record["page_rotation"],90)
            self.assertEqual(record["printed_page_label"],"1341")
            self.assertGreater(record["pixel_width"],record["pixel_height"])
            source.unlink()  # synthetic fixture only
            self.assertTrue(verify(root,result["capture_id"],1,3)["local_content_verified"])
            with png.open("ab") as f:
                f.write(b"tamper")
            with self.assertRaisesRegex(ValueError,"checksum/size"):
                verify(root,result["capture_id"],1,3)

    def test_derivative_policy_and_range_fail_before_writes(self):
        from visual_evidence import capture
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._fixture(folder)
            root = folder / "retained"
            with self.assertRaises(PermissionError):
                capture(source,root,1,3)
            with self.assertRaisesRegex(ValueError,"policy identifier"):
                capture(source,root,1,3,authorized_page_derivatives=True)
            with self.assertRaisesRegex(ValueError,"physical page range"):
                capture(source,root,1,4,policy_id="test",authorized_page_derivatives=True)
            self.assertFalse(root.exists())

    def test_derivative_missing_page_fails_verification(self):
        from visual_evidence import capture, verify
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = self._fixture(folder)
            root = folder / "retained"
            result = capture(source,root,1,1,dpi=150,policy_id="test",
                             authorized_page_derivatives=True)
            with self.assertRaises(FileNotFoundError):
                verify(root,result["capture_id"],1,3)


if __name__ == "__main__":
    unittest.main()
