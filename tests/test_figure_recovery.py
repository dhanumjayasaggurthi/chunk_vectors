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
