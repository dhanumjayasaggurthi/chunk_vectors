import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_table_geometry_probe import compare, sample_numbers


class NativeGeometryProbeTests(unittest.TestCase):
    def test_plain_text_and_rotated_table_parity(self):
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"probe.pdf"
            with fitz.open() as doc:
                doc.new_page().insert_text((50,50),"Native body text without table")
                for rotation in (0,90):
                    page=doc.new_page()
                    for x in (50,150,250):
                        page.draw_line((x,100),(x,180))
                    for y in (100,140,180):
                        page.draw_line((50,y),(250,y))
                    for x,y,s in ((60,120,"Dose"),(160,120,"Units"),(60,160,"20"),(160,160,"mg")):
                        page.insert_text((x,y),s)
                    page.set_rotation(rotation)
                doc.save(path)
            result=compare(path,[1,2,3],{"vertical_strategy":"lines","horizontal_strategy":"lines","snap_tolerance":3})
            self.assertEqual(result["positive_pages"],2)
            self.assertEqual(result["pages"],3)
            self.assertTrue(result["exact_detected_geometry_and_cell_parity"])
            with self.assertRaisesRegex(ValueError,"line-based"):
                compare(path,[1],{"vertical_strategy":"text","horizontal_strategy":"lines"})

    def test_sample_has_unique_physical_pages_and_includes_endpoints(self):
        self.assertEqual(sample_numbers(3,100),[1,2,3])
        n=sample_numbers(1521,100)
        self.assertEqual(len(set(n)),100)
        self.assertEqual((n[0],n[-1]),(1,1521))


if __name__=="__main__":
    unittest.main()
