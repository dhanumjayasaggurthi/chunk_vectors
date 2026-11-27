"""Cloud-free RDR and multilingual correctness tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rich_document import native_run, validate_runs, html_for_elements
from translation_stage import compose_language_input, translation_fingerprint


class RichDocumentUnitTests(unittest.TestCase):
    def test_native_style_and_html_escaping(self):
        run = native_run({
            "text": "<confidential> & approved",
            "font": "Arial-BoldItalic", "size": 13,
            "flags": 18, "color": 0x112233,
            "bbox": (10, 20, 180, 38),
        }, 0)
        self.assertTrue(run["bold"])
        self.assertTrue(run["italic"])
        self.assertEqual(run["color_rgb"], "#112233")
        validate_runs([run])
        html = html_for_elements([{
            "type": "text", "runs": [run],
            "semantic_role": "heading", "heading_level": 2,
        }])
        self.assertIn("<h2>", html)
        self.assertIn("<strong>", html)
        self.assertIn("<em>", html)
        self.assertIn("&lt;confidential&gt; &amp; approved", html)
        self.assertNotIn("<confidential>", html)

    def test_bad_geometry_fails_closed(self):
        run = native_run({"text": "safe", "bbox": (0, 0, 1, 1)}, 0)
        run["bbox"][1] = float("nan")
        with self.assertRaisesRegex(ValueError, "geometry"):
            validate_runs([run])

    def test_multilingual_policy_never_mutates_native(self):
        native = "Le traitement a montré une amélioration significative."
        en = "The treatment showed a significant improvement."
        self.assertEqual(compose_language_input(native, en, language="fr"), en)
        self.assertEqual(compose_language_input(native, None, language="fr",
                                                mode="native"), native)
        bilingual = compose_language_input(native, en, language="fr", mode="bilingual")
        self.assertIn(native, bilingual)
        self.assertIn(en, bilingual)
        with self.assertRaisesRegex(ValueError, "required"):
            compose_language_input(native, None, language="fr")
        self.assertEqual(compose_language_input("English", None, language="en"), "English")
        self.assertEqual(
            translation_fingerprint(provider="test", model="v1"),
            translation_fingerprint(provider="test", model="v1"))
        self.assertNotEqual(
            translation_fingerprint(provider="test", model="v1"),
            translation_fingerprint(provider="test", model="v2"))

    def test_nested_empty_table_is_evidence_not_dropped_text(self):
        from types import SimpleNamespace
        from pdf_processor import _is_nested_empty_table_artifact, BBox
        outer=SimpleNamespace(bbox=(100,100,300,300))
        nested=SimpleNamespace(bbox=(150,150,190,190))
        class FakePlumber:
            def __init__(self,words=()):
                self.words=list(words)
            def crop(self, box):
                return SimpleNamespace(extract_words=lambda:self.words)
        args=(nested, [[None,None],[None,None]], [outer], FakePlumber(), [])
        self.assertTrue(_is_nested_empty_table_artifact(*args))
        self.assertFalse(_is_nested_empty_table_artifact(
            nested, [["30 mg"]], [outer], FakePlumber(), []))
        self.assertFalse(_is_nested_empty_table_artifact(
            nested, [[None]], [outer], FakePlumber(["dose"]), []))
        self.assertFalse(_is_nested_empty_table_artifact(
            nested, [[None]], [], FakePlumber(), []))
        overlapping=SimpleNamespace(bbox=BBox(150,150,190,190))
        self.assertFalse(_is_nested_empty_table_artifact(
            nested, [[None]], [outer], FakePlumber(), [overlapping]))
        oversized=SimpleNamespace(bbox=(150,150,210,210))
        self.assertFalse(_is_nested_empty_table_artifact(
            oversized, [[None]], [outer], FakePlumber(), []))

    def test_raster_caption_boundary_preserves_unresolved_region(self):
        import fitz
        import pdfplumber
        from types import SimpleNamespace
        from PIL import Image
        import io
        from pdf_processor import (BBox,PageElement,ElementType,
            _verified_raster_caption_words,_preserve_empty_table_candidate)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"caption.pdf"
            raster=io.BytesIO()
            Image.new("RGB",(600,300),"white").save(raster,format="PNG")
            with fitz.open() as pdf:
                page=pdf.new_page(width=612,height=792)
                page.insert_image(fitz.Rect(54,200,554,450),stream=raster.getvalue())
                page.insert_text((54,198),"FIGURE 9: Example of an HTML table",fontsize=12)
                pdf.save(path)
            with fitz.open(path) as pdf,pdfplumber.open(path) as plumb:
                page=pdf[0]
                pp=plumb.pages[0]
                block=page.get_text("blocks",flags=fitz.TEXTFLAGS_TEXT)[0]
                caption=PageElement(ElementType.TEXT,BBox(*block[:4]),text=block[4].strip())
                box=(53,caption.bbox.y1-2,558,451)
                words=pp.crop(box).extract_words()
                self.assertTrue(words)
                self.assertTrue(_verified_raster_caption_words(box,words,pp,page,[caption]))
                self.assertFalse(_verified_raster_caption_words(box,words[:-1],pp,page,[caption]))
                self.assertFalse(_verified_raster_caption_words(box,words,pp,page,[]))
                self.assertFalse(_verified_raster_caption_words((53,180,558,451),words,pp,page,[caption]))
                table=SimpleNamespace(bbox=box)
                kwargs=dict(candidate_index=1,native_page=page,text_elements=[caption])
                result=_preserve_empty_table_candidate(table,[["",None]],pp,1,16,0,**kwargs)
                self.assertEqual(result.element_type,ElementType.VECTOR_REGION)
                self.assertFalse(result.unresolved_region["structured_table_recovered"])
                self.assertEqual(result.bbox,BBox(*box))
                with self.assertRaisesRegex(ValueError,"source cell text"):
                    _preserve_empty_table_candidate(table,[["Dose 20 mg"]],pp,1,16,0,**kwargs)

    def test_vector_preview_persists_with_checksum_and_review_label(self):
        import hashlib
        import io
        import json
        from types import SimpleNamespace
        from PIL import Image
        from pdf_processor import PageContent,PageElement,BBox,ElementType,DocumentStructure
        from page_stage import _page_row
        buf=io.BytesIO()
        Image.new("RGB",(20,20),"white").save(buf,format="PNG")
        png=buf.getvalue()
        vector=PageElement(ElementType.VECTOR_REGION,
                           BBox(50,70,80,90),vector_png=png)
        page=PageContent(
            page_number=1,width=600,height=800,
            raw_text="Clinical results and dose 25 mg",
            elements=[PageElement(ElementType.TEXT,BBox(50,50,400,80),
                                  text="Clinical results and dose 25 mg"),vector])
        struct=DocumentStructure(
            file_path="/test.pdf",file_name="test.pdf",doc_id="doc1",
            page_count=1,pages=[page])
        row=_page_row(page,struct,"src","fingerprint")
        elements=json.loads(row[9])
        entry=next(v for v in elements if v["type"]=="vector_region")
        self.assertEqual(entry["vector_preview"]["sha256"],
                         hashlib.sha256(png).hexdigest())
        self.assertTrue(entry["vector_preview"]["review_required"])
        self.assertGreater(len(entry["vector_preview"]["base64"]),10)

    def test_native_pdf_mixed_styles_and_resumable_windows(self):
        import fitz
        from pdf_processor import PDFProcessor
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "styled.pdf"
            doc = fitz.open()
            for i in range(6):
                page = doc.new_page()
                page.insert_text((50, 70), "REGULATORY RESULTS", fontname="hebo",
                                 fontsize=16)
                page.insert_text((50, 95), "Clinical evidence", fontname="heit",
                                 fontsize=12)
                page.insert_text((50, 120), "Dose 25 mg; response 82%",
                                 fontsize=11)
                page.draw_line((50, 150), (500, 150))
                page.draw_line((50, 200), (500, 200))
                page.draw_line((50, 150), (50, 200))
                page.draw_line((500, 150), (500, 200))
            doc.save(path)
            doc.close()
            proc = PDFProcessor(str(path), "doc", path.name,
                                capture_rich_text=True)
            windows = list(proc.iter_page_batches(batch_pages=2,
                                                   completed_pages={1, 2}))
            self.assertEqual(sum(len(w.pages) for w in windows), 4)
            self.assertTrue(all(len(w.pages) <= 2 for w in windows))
            first = windows[0].pages[0]
            self.assertFalse(first.error)
            runs = [run for element in first.elements
                    for run in getattr(element, "runs", [])]
            self.assertTrue(any(run["bold"] for run in runs))
            self.assertTrue(any(run["italic"] for run in runs))
            self.assertTrue(any("Dose 25 mg" in run["text"] for run in runs))


if __name__ == "__main__":
    unittest.main()
