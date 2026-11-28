"""Source-aligned physical-line body recovery, not logical row inference."""
import copy
import tempfile
import unittest
from pathlib import Path

from open_grid_recovery import recover_open_grid,validate_physical_body


class OpenGridBodyTests(unittest.TestCase):
    def test_physical_grid_and_strict_native_output_preservation(self):
        import fitz
        import pdfplumber
        from pdf_processor import PDFProcessor
        from table_extractor import inject_table_text_into_elements
        from config import TABLE_SETTINGS
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"grid.pdf"
            with fitz.open() as doc:
                for crossing in (False,True):
                    page=doc.new_page(width=612,height=792)
                    xs=[50,200,350,550]
                    for x in xs:
                        page.draw_line((x,60),(x,700))
                    for y in (60,100):
                        page.draw_line((50,y),(550,y))
                    for x,value in zip(xs,["Visit","Dose","Response"]):
                        page.insert_text((x+8,85),value)
                    for y,values in ((120,["Visit day one","Dose amount 20 mg","Observed response"]),
                                     (145,["Visit day two","Dose amount 40 mg","Final result"]),
                                     (170,["Visit day three","Dose amount 60 mg","Follow up"]),
                                     (195,["Visit day four","Dose amount 80 mg","Follow up"])):
                        for x,value in zip(xs,values):
                            page.insert_text((x+8,y),value,fontsize=10)
                    if crossing:
                        page.insert_text((198,210),"WORD",fontsize=12)
                doc.save(path)
            with pdfplumber.open(path) as doc:
                first=doc.pages[0]
                status,body=recover_open_grid(first,first.find_tables(TABLE_SETTINGS))
                self.assertEqual(status,"PHYSICAL_LINES_VERIFIED_ONLY")
                self.assertEqual(len(body["physical_cells"]),4)
                self.assertEqual(body["physical_cells"][0],["Visit day one","Dose amount 20 mg","Observed response"])
                self.assertFalse(body["logical_rows_verified"])
                self.assertEqual(validate_physical_body(body),body)
                changed=copy.deepcopy(body)
                changed["physical_cells"][0][1]="Dose amount 90 mg"
                with self.assertRaisesRegex(ValueError,"checksum"):
                    validate_physical_body(changed)
                with self.assertRaisesRegex(ValueError,"different header"):
                    validate_physical_body(body,header_bbox=[0,0,1,1])
                other=doc.pages[1]
                self.assertEqual(recover_open_grid(other,other.find_tables(TABLE_SETTINGS)),
                                 ("GLYPH_CROSSES_COLUMN_OR_BODY",None))
            kwargs=dict(strict=True,capture_rich_text=True,capture_table_structure=True,
                        preserve_uninterpreted_visuals=True,empty_table_policy="preserve_defer")
            def extract(enabled):
                proc=PDFProcessor(str(path),"fixture",path.name,**kwargs,recover_open_grid_body=enabled)
                pages=[p for w in proc.iter_page_batches(batch_pages=1) for p in w.pages]
                for p in pages:
                    self.assertFalse(p.error,p.error)
                    inject_table_text_into_elements(p.elements,p.staged_table_results,strict=True)
                return pages
            old,new=extract(False),extract(True)
            found=[e for e in new[0].elements if getattr(e,"physical_table_body",None)]
            self.assertEqual(len(found),1)
            self.assertEqual(found[0].physical_table_body,body)
            # Page staging serializes the new separate field; no table content
            # or text changes, and no inference of logical rows.
            from page_stage import _page_row
            from types import SimpleNamespace
            row=_page_row(new[0],SimpleNamespace(doc_id="fixture",file_name=path.name,
                         file_path=str(path),toc=[]),"a"*64,"b"*64)
            self.assertIn(body["sha256"],str(row))
            for pg in new:
                for el in pg.elements:
                    vars(el).pop("physical_table_body",None)
            self.assertEqual([vars(p) for p in old],[vars(p) for p in new])


if __name__=="__main__":
    unittest.main()
