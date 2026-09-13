import tempfile
import unittest
from pathlib import Path

import fitz

from mir_ai.enrich import PDFEnricher
from mir_ai.pdf_stream import PDFPageStream


class TestPDFTableEnrichment(unittest.TestCase):
    def test_table_is_extracted_from_requested_page_without_pdfplumber(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.pdf"
            document = fitz.open()
            page = document.new_page()
            x0, y0, width, height = 100, 100, 100, 30
            for row in range(3):
                page.draw_line((x0, y0 + row * height), (x0 + 2 * width, y0 + row * height))
            for col in range(3):
                page.draw_line((x0 + col * width, y0), (x0 + col * width, y0 + 2 * height))
            page.insert_text((110, 120), "Parameter")
            page.insert_text((210, 120), "Value")
            page.insert_text((110, 150), "Dose")
            page.insert_text((210, 150), "30 mg/kg")
            document.save(path)
            document.close()

            manifest_page = next(iter(PDFPageStream(str(path), "doc-test")))
            enriched = PDFEnricher(str(path), "doc-test").enrich_page(manifest_page)
            tables = [e for e in enriched.elements if e.element_type == "table" and e.extraction_status == "success"]
            self.assertEqual(len(tables), 1)
            self.assertIn("Parameter", tables[0].text)
            self.assertIn("30 mg/kg", tables[0].text)
            self.assertIsNotNone(tables[0].bbox)


if __name__ == "__main__":
    unittest.main()
