import tempfile
import unittest
import zipfile

from mir_ai.docx_stream import DOCXPageStream


class TestDocxStream(unittest.TestCase):
    def _write_docx(self, path, xml):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)

    def test_explicit_page_break_is_logical_page_boundary(self):
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First page</w:t></w:r><w:r><w:br w:type="page"/></w:r></w:p><w:p><w:r><w:t>Second page</w:t></w:r></w:p></w:body></w:document>'''
        with tempfile.NamedTemporaryFile(suffix=".docx") as tmp:
            self._write_docx(tmp.name, xml)
            pages = list(DOCXPageStream(tmp.name, "doc"))
        self.assertEqual(len(pages), 2)
        self.assertIn("First page", pages[0].native_text)
        self.assertIn("Second page", pages[1].native_text)
        self.assertIsNone(pages[0].elements[0].bbox)
        self.assertEqual(pages[0].page_label, "logical-1")

    def test_no_break_document_is_bounded_into_logical_segments(self):
        paragraphs = "".join(
            f"<w:p><w:r><w:t>Paragraph {index}</w:t></w:r></w:p>"
            for index in range(11)
        )
        xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}</w:body></w:document>"
        )
        with tempfile.NamedTemporaryFile(suffix=".docx") as tmp:
            self._write_docx(tmp.name, xml)
            pages = list(
                DOCXPageStream(tmp.name, "doc", max_elements_per_segment=3)
            )
        self.assertEqual(len(pages), 4)
        self.assertTrue(all(len(page.elements) <= 3 for page in pages))
        self.assertEqual(pages[-1].page_label, "logical-4")
        self.assertIn("Paragraph 10", pages[-1].native_text)
        self.assertEqual(
            pages[0].elements[0].raw.get("page_provenance"), "logical_segment"
        )


if __name__ == "__main__":
    unittest.main()
