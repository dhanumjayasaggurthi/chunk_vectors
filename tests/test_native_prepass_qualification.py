import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from native_prepass_qualification import extract_document, load_manifest
from native_table_prepass import detect_tables


class NativePrepassQualificationTests(unittest.TestCase):
    def test_fixed_manifest_refuses_unknown_duplicate_and_invalid_sources(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"manifest.json"
            urls={"a":"https://example.test/a.pdf","b":"https://example.test/b.pdf"}
            valid=[{"name":"b","pages":200},{"name":"a","pages":100}]
            path.write_text(json.dumps(valid),encoding="utf-8")
            self.assertEqual(load_manifest(path,urls),list(reversed(valid)))
            for rows in ([valid[0],valid[0]], [{"name":"missing","pages":100}],
                         [{"name":"a","pages":99}], [{"name":"a","pages":True}],
                         [{"name":"a","pages":100,"url":"other"}], []):
                path.write_text(json.dumps(rows),encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(path,urls)

    def test_fallbacks_are_authoritative_and_full_errors_propagate(self):
        page=Mock()
        sentinel=[object()]
        page.find_tables.return_value=sentinel
        settings={"vertical_strategy":"lines","horizontal_strategy":"lines"}
        with patch("native_table_prepass._eligible",return_value=False):
            self.assertEqual(detect_tables(page,settings),(sentinel,"UNSUPPORTED"))
        with patch("native_table_prepass._eligible",return_value=True):
            with patch("native_table_prepass._edge_negative",return_value=False):
                self.assertEqual(detect_tables(page,settings),(sentinel,"POSITIVE_FALLBACK"))
            with patch("native_table_prepass._edge_negative",side_effect=RuntimeError("probe")):
                self.assertEqual(detect_tables(page,settings),(sentinel,"ERROR_FALLBACK"))
                page.find_tables.side_effect=RuntimeError("authoritative")
                with self.assertRaisesRegex(RuntimeError,"authoritative"):
                    detect_tables(page,settings)
            with patch("native_table_prepass._edge_negative",return_value=True):
                self.assertEqual(detect_tables(page,settings),([],"NEGATIVE_PREPASS"))

    def test_actual_parser_output_parity_text_table_and_rotated_table(self):
        import fitz
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"mixed.pdf"
            with fitz.open() as doc:
                doc.new_page().insert_text((50,60),"Native body text")
                for rotation in (0,90):
                    page=doc.new_page()
                    for x in (50,150,250):
                        page.draw_line((x,100),(x,180))
                    for y in (100,140,180):
                        page.draw_line((50,y),(250,y))
                    for x,y,s in ((60,120,"Dose"),(160,120,"Unit"),(60,160,"20"),(160,160,"mg")):
                        page.insert_text((x,y),s)
                    page.set_rotation(rotation)
                doc.save(path)
            item={"name":path.name,"path":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"pages":3}
            old=extract_document(item,experimental=False)
            new=extract_document(item,experimental=True)
            self.assertEqual(old["error_pages"],[])
            self.assertEqual(new["error_pages"],[])
            self.assertEqual(old["page_digests"],new["page_digests"])
            self.assertEqual(old["toc_digest"],new["toc_digest"])
            self.assertEqual(new["prepass_counts"],{"NEGATIVE_PREPASS":1,"POSITIVE_FALLBACK":2})


if __name__=="__main__":
    unittest.main()
