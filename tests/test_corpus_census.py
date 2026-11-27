import tempfile
import unittest
from pathlib import Path

from corpus_census import census_paths


class CorpusCensusTests(unittest.TestCase):
    def test_counts_pages_without_extracting_content_and_reports_errors(self):
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            paths=[]
            for count in (2,11):
                path=root/f"{count}.pdf"
                with fitz.open() as doc:
                    for _ in range(count):
                        doc.new_page()
                    doc.save(path)
                paths.append(path)
            bad=root/"broken.pdf"
            bad.write_bytes(b"not a PDF")
            results=[census_paths([*paths,bad],w) for w in (1,2)]
            for result in results:
                self.assertEqual(result["documents"],3)
                self.assertEqual(result["pdf_pages"],13)
                self.assertEqual(result["pdf_documents"],2)
                self.assertEqual(result["errors"],1)
                self.assertEqual(result["pdf_page_count_upper_bound_histogram"],{"10":1,"50":1})
                self.assertFalse(result["page_count_complete_for_all_documents"])
                self.assertFalse(result["writes"])
                self.assertFalse(result["cloud_calls"])
            self.assertTrue(census_paths(paths,1)["page_count_complete_for_all_documents"])


if __name__=="__main__":
    unittest.main()
