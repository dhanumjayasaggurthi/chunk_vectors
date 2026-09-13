import tempfile
import unittest
from pathlib import Path

import fitz

from mir_ai.pdf_stream import PDFPageStream


class TestPDFVectorChart(unittest.TestCase):
    def test_vector_chart_candidate_has_bbox_and_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vector-chart.pdf"
            doc = fitz.open()
            page = doc.new_page()
            # Dense line-art region plus nearby numeric/axis labels.
            page.draw_rect(fitz.Rect(100, 150, 420, 430))
            page.draw_line((120, 400), (390, 400))
            page.draw_line((120, 180), (120, 400))
            page.draw_polyline([(120, 360), (190, 330), (260, 290), (330, 230), (390, 200)])
            for x in (180, 240, 300, 360):
                page.draw_line((x, 395), (x, 405))
            page.insert_text((120, 140), "Dose Response")
            page.insert_text((130, 420), "Dose 0 10 20 30 mg/kg")
            page.insert_text((60, 230), "Response 1 2 3 4")
            doc.save(path)
            doc.close()

            manifest = next(iter(PDFPageStream(str(path), "doc-vector")))
            charts = [
                element
                for element in manifest.elements
                if element.element_type == "chart"
                and element.source_locator.get("kind") == "pdf_vector_region"
            ]
            self.assertGreaterEqual(len(charts), 1)
            self.assertIsNotNone(charts[0].bbox)
            self.assertTrue(charts[0].raw.get("vector_graphic"))


if __name__ == "__main__":
    unittest.main()
