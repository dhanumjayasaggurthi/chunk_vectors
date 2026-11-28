"""Cloud-free checks for lossless source-table evidence and non-inferred text."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class TableSourceFidelityTests(unittest.TestCase):
    def _sample(self):
        from table_extractor import build_source_table
        raw = [
            ["Study group","Dose","Result"],
            ["Cohort A","10 mg","Improved"],
            ["Cohort B",None,"Stable"],
            [None,"",""],
            ["Long narrative","Some dose","Text " + ("x"*720)],
        ]
        rows=[]
        for r in range(len(raw)):
            boxes=[]
            for c in range(len(raw[r])):
                boxes.append(None if raw[r][c] is None else
                             (float(20+c*70),float(50+r*30),
                              float(85+c*70),float(75+r*30)))
            rows.append(SimpleNamespace(cells=boxes))
        obj=build_source_table(raw,SimpleNamespace(rows=rows),max_json_bytes=2_000_000)
        return raw,obj

    def test_original_empty_cells_rows_and_long_text_are_preserved(self):
        from table_extractor import TableExtractor,validate_source_table
        raw,stored=self._sample()
        self.assertEqual(validate_source_table(stored),stored)
        self.assertEqual(stored["source_cells"],raw)
        self.assertIsNone(stored["source_cells"][2][1])
        self.assertEqual(stored["source_cells"][3],[None,"",""])
        self.assertEqual(stored["cell_bboxes"][2][1],None)
        self.assertGreater(len(stored["source_cells"][-1][-1]),500)
        with tempfile.TemporaryDirectory() as dirname:
            extractor=TableExtractor(str(Path(dirname)/"test.pdf"),summarise=False)
            faithful=extractor._process_table(raw,1,0,"",allow_short=True,preserve_source=True)
            legacy=extractor._process_table(raw,1,0,"",allow_short=True)
        self.assertIsNotNone(faithful)
        self.assertIn("Cohort B",faithful.markdown)
        b_row=next(line for line in faithful.markdown.splitlines() if "Cohort B" in line)
        self.assertNotIn("10 mg",b_row)  # no fabricated fill-forward
        self.assertIn("x"*720,faithful.markdown)  # no 500-character truncation
        self.assertIn("10 mg",legacy.markdown.split("Cohort B")[1].split("\n")[0])
        self.assertNotIn("x"*720,legacy.markdown)

    def test_corruption_or_budget_must_fail_closed(self):
        from table_extractor import build_source_table,validate_source_table
        raw,data=self._sample()
        broken=copy.deepcopy(data)
        broken["source_cells"][1][1]="20 mg"
        with self.assertRaisesRegex(ValueError,"checksum"):
            validate_source_table(broken)
        with self.assertRaisesRegex(ValueError,"budget"):
            validate_source_table(data,max_json_bytes=100)
        rows=[SimpleNamespace(cells=[(0,0,1,1)]) for _ in raw]
        with self.assertRaisesRegex(ValueError,"column"):
            build_source_table(raw,SimpleNamespace(rows=rows),
                               max_json_bytes=2_000_000)
        # Exact matrix remains JSON serializable across DB JSONB boundaries.
        self.assertEqual(validate_source_table(json.loads(json.dumps(data))),data)

    def test_fingerprint_changes_only_for_semantic_table_mode(self):
        from processing_profile import load_profile,validate_profile
        from page_stage import extraction_fingerprint
        profile=load_profile("profiles/regulatory_page_stage_example.yml")
        baseline=extraction_fingerprint(profile)
        profile["extraction"]["capture_table_structure"]=True
        enabled=extraction_fingerprint(profile)
        self.assertNotEqual(baseline,enabled)
        profile["extraction"]["max_table_source_json_bytes"]=12_000_000
        self.assertEqual(enabled,extraction_fingerprint(profile))
        validate_profile(profile)
        profile["extraction"]["max_table_source_json_bytes"]=-1
        with self.assertRaisesRegex(ValueError,"max_table_source_json_bytes"):
            validate_profile(profile)

    def test_real_pdf_table_source_geometry_roundtrip(self):
        import fitz
        from pdf_processor import PDFProcessor,ElementType
        from page_stage import _page_row
        from table_extractor import validate_source_table
        with tempfile.TemporaryDirectory() as dirname:
            path=Path(dirname)/"table.pdf"
            with fitz.open() as pdf:
                page=pdf.new_page(width=612,height=792)
                xs=[50,240,480]
                ys=[90,130,170,210]
                for y in ys:
                    page.draw_line((xs[0],y),(xs[-1],y))
                for x in xs:
                    page.draw_line((x,ys[0]),(x,ys[-1]))
                for r in range(3):
                    page.insert_text((60,112+r*40),f"Parameter {r}: 25mg")
                    page.insert_text((250,112+r*40),f"Evidence {r}")
                pdf.save(path)
            proc=PDFProcessor(str(path),"test-id",path.name,strict=True,
                              capture_rich_text=True,capture_table_structure=True)
            struct=next(proc.iter_page_batches(batch_pages=1))
            page=struct.pages[0]
            self.assertFalse(page.error)
            tables=[e for e in page.elements if e.element_type==ElementType.TABLE]
            self.assertEqual(len(tables),1)
            from table_extractor import inject_table_text_into_elements
            inject_table_text_into_elements(
                page.elements,page.staged_table_results,strict=True)
            self.assertIsNotNone(tables[0].source_table)
            self.assertEqual(len(tables[0].source_table["source_cells"]),3)
            row=_page_row(page,struct,"src","extract-version")
            saved=json.loads(row[9])
            stored=next(e["source_table"] for e in saved if e["type"]=="table")
            self.assertEqual(validate_source_table(stored),tables[0].source_table)
            self.assertEqual(stored["row_count"],3)
            self.assertEqual(stored["max_columns"],2)


if __name__=="__main__":
    unittest.main()
