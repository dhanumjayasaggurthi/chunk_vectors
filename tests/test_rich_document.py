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
