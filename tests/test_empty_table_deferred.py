"""Regression: preserve empty pdfplumber table candidates without inventing cells.

Synthetic PDFs exercise exactly the observed failure shape: detectable ruled
graphics with no source words/cells, plus a separate legitimate source table.
All database tests are restricted to an explicitly disposable CI instance.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def create_pdf(directory: Path) -> Path:
    import fitz
    filename=directory/"empty-table-candidates.pdf"
    with fitz.open() as pdf:
        for number in (1,2):
            page=pdf.new_page(width=612,height=792)
            page.insert_text((48,65),f"Source clinical results page {number}.")
            # Drawable grid with no native cell words: pdfplumber detects a
            # table candidate, but it has no recoverable source cells.
            xs=[82,215,345,480]
            ys=[245,285,325,365,405,445]
            for x in xs:
                page.draw_line((x,ys[0]),(x,ys[-1]))
            for y in ys:
                page.draw_line((xs[0],y),(xs[-1],y))
            if number==2:
                # Independent valid table on same page must survive intact.
                x2=[58,238,458]
                y2=[95,132,169,206]
                for x in x2:
                    page.draw_line((x,y2[0]),(x,y2[-1]))
                for y in y2:
                    page.draw_line((x2[0],y),(x2[-1],y))
                for i in range(3):
                    page.insert_text((68,115+i*37),f"Subject {i}")
                    page.insert_text((248,115+i*37),f"Outcome {i}")
        pdf.save(filename)
    return filename


def profile():
    from processing_profile import load_profile
    p=load_profile("profiles/regulatory_page_stage_example.yml")
    p["extraction"].update(
        schema="intake_ci",table="ci_empty_table_deferred",
        visual_processing="preserve_defer",
        empty_table_policy="preserve_defer",
        ocr_enabled=False,
        chart_descriptions_enabled=False,
        table_summaries_enabled=False,
        capture_rich_text=True,
        capture_table_structure=True,
        max_visual_regions_per_page=256,
        page_batch_size=1,
    )
    p["extracted_content"].update(
        schema="intake_ci",table="ci_empty_table_deferred"
    )
    p["output"].update(schema="intake_ci",table="ci_empty_deferred_chunks")
    p["embedding"]["enabled"]=False
    return p


class EmptyTableDeferredTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.file=create_pdf(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_fail_policy_preserves_old_strict_behavior(self):
        from pdf_processor import PDFProcessor
        with patch("vision_ocr.batch_document_text_detection",
                   side_effect=AssertionError("No cloud OCR expected")):
            processor=PDFProcessor(
                str(self.file),"d"*64,self.file.name,strict=True,
                capture_table_structure=True,
                preserve_uninterpreted_visuals=True,
                empty_table_policy="fail",
            )
            pages=[p for batch in processor.iter_page_batches(batch_pages=2)
                   for p in batch.pages]
        self.assertEqual(len(pages),2)
        self.assertTrue(any("no recoverable cells" in p.error for p in pages))

    def test_opt_in_keeps_source_region_and_real_table(self):
        from pdf_processor import PDFProcessor,ElementType
        from page_stage import _page_row
        from table_extractor import inject_table_text_into_elements
        import fitz
        processor=PDFProcessor(
            str(self.file),"d"*64,self.file.name,strict=True,
            capture_table_structure=True,capture_rich_text=True,
            preserve_uninterpreted_visuals=True,
            empty_table_policy="preserve_defer",
        )
        windows=list(processor.iter_page_batches(batch_pages=2))
        pages=[p for batch in windows for p in batch.pages]
        self.assertEqual(len(pages),2)
        self.assertTrue(all(not p.error for p in pages),[p.error for p in pages])
        self.assertTrue(all(p.raw_text for p in pages))
        for page in pages:
            deferred=[el for el in page.elements if el.unresolved_region]
            self.assertEqual(len(deferred),1)
            el=deferred[0]
            self.assertEqual(el.element_type,ElementType.VECTOR_REGION)
            self.assertFalse(el.text)
            self.assertFalse(el.source_table)
            self.assertFalse(el.vector_png)
            self.assertEqual(el.unresolved_region["status"],
                             "PENDING_OCR_OR_VISUAL_ENRICHMENT")
            self.assertIs(el.unresolved_region["structured_table_recovered"],False)
            self.assertEqual(el.unresolved_region["source_page"],page.page_number)
            self.assertGreater(el.bbox.area,2500)
            inject_table_text_into_elements(
                page.elements,page.staged_table_results,strict=True
            )
            entry=json.loads(_page_row(page,windows[0],"a"*64,"b"*64)[9])
            unresolved=[e["unresolved_region"] for e in entry
                        if "unresolved_region" in e]
            self.assertEqual(len(unresolved),1)
        valid=[e for e in pages[1].elements if e.source_table is not None]
        self.assertEqual(len(valid),1)
        self.assertEqual(valid[0].source_table["row_count"],3)
        self.assertIn("Subject 1",valid[0].text)
        # Source PDF remains the authoritative original source.
        with fitz.open(self.file) as source_pdf:
            self.assertEqual(source_pdf.page_count,2)

    def test_reject_nonempty_or_unverifiable_candidate(self):
        from pdf_processor import (
            _preserve_empty_table_candidate,validate_unresolved_region
        )
        table=SimpleNamespace(bbox=(50,50,190,250))
        plumb=SimpleNamespace(width=612,height=792,
            crop=lambda box:SimpleNamespace(extract_words=lambda:[]))
        def preserve(raw,pl=plumb,limit=3,used=0):
            return _preserve_empty_table_candidate(
                table,raw,pl,1,limit,used,candidate_index=2
            )
        result=preserve([[None,""],["",None]])
        self.assertEqual(result.unresolved_region["candidate_index"],2)
        self.assertEqual(
            validate_unresolved_region(result.unresolved_region,page_number=1),
            result.unresolved_region
        )
        with self.assertRaisesRegex(ValueError,"source cell text"):
            preserve([["Dose","10mg"]])
        with self.assertRaisesRegex(ValueError,"source PDF words"):
            preserve([[None]],SimpleNamespace(
                width=612,height=792,
                crop=lambda box:SimpleNamespace(
                    extract_words=lambda:[{"text":"Dose"}]
                )
            ))
        with self.assertRaisesRegex(ValueError,"VISUAL_REGION_LIMIT_EXCEEDED"):
            preserve([[None]],limit=2,used=2)
        with self.assertRaisesRegex(ValueError,"provenance"):
            validate_unresolved_region(result.unresolved_region,page_number=2)

    def test_nested_candidate_preserves_verified_native_words_only(self):
        import fitz
        import pdfplumber
        from pdf_processor import (
            BBox, ElementType, PageElement, _preserve_empty_table_candidate,
        )
        filename=Path(self.temp.name)/"nested-form.pdf"
        with fitz.open() as pdf:
            for _ in range(2):
                page=pdf.new_page(width=612,height=792)
                page.insert_text((60,65),"X Y")
            pdf.save(filename)
        table=SimpleNamespace(bbox=(55,48,100,75))
        parent=SimpleNamespace(bbox=(40,35,300,250))
        with fitz.open(filename) as pdf, pdfplumber.open(filename) as plumb:
            for i in range(2):
                native=pdf[i]
                page=plumb.pages[i]
                blocks=native.get_text("blocks")
                elements=[PageElement(ElementType.TEXT,BBox(*b[:4]),text=b[4])
                          for b in blocks]
                self.assertEqual(len(page.crop(table.bbox).extract_words()),2)
                def preserve(els, parents=(parent,), raw=[[None],[""]]):
                    return _preserve_empty_table_candidate(
                        table,raw,page,i+1,10,0,candidate_index=9,
                        native_page=native,text_elements=els,valid_tables=parents,
                    )
                result=preserve(elements)
                self.assertEqual(result.bbox,BBox(*table.bbox))
                self.assertFalse(result.text)
                self.assertFalse(result.source_table)
                self.assertFalse(result.unresolved_region["structured_table_recovered"])
                self.assertEqual(result.unresolved_region["source_page"],i+1)
                self.assertEqual(elements[0].text.strip(),"X Y")
                with self.assertRaisesRegex(ValueError,"source PDF words"):
                    preserve([])
                with self.assertRaisesRegex(ValueError,"source PDF words"):
                    preserve([PageElement(ElementType.TEXT,BBox(*blocks[0][:4]),text="X")])
                with self.assertRaisesRegex(ValueError,"source PDF words"):
                    preserve([PageElement(ElementType.TEXT,BBox(300,300,350,350),text="X Y")])
                with self.assertRaisesRegex(ValueError,"source PDF words"):
                    preserve(elements,parents=())
                with self.assertRaisesRegex(ValueError,"source cell text"):
                    preserve(elements,raw=[["X"]])

    def test_profile_semantic_versioning_and_safety_validation(self):
        from page_stage import extraction_fingerprint
        from processing_profile import validate_profile
        p=profile()
        validate_profile(p)
        after=extraction_fingerprint(p)
        p["extraction"]["empty_table_policy"]="fail"
        validate_profile(p)
        self.assertNotEqual(after,extraction_fingerprint(p))
        p["extraction"]["empty_table_policy"]="preserve_defer"
        p["extraction"]["visual_processing"]="blocking"
        with self.assertRaisesRegex(ValueError,"requires native"):
            validate_profile(p)
        p["extraction"]["visual_processing"]="preserve_defer"
        p["extraction"]["empty_table_policy"]="unknown"
        with self.assertRaisesRegex(ValueError,"empty_table_policy"):
            validate_profile(p)


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1" and
    os.getenv("PGDATABASE")=="embedding_test",
    "Only the disposable PostgreSQL CI database is permitted",
)
class EmptyTableDeferredPostgresTests(unittest.TestCase):
    def test_publish_preserved_regions_and_idempotency(self):
        from db import get_conn
        from page_stage import init,names,stage_one
        from extracted_source import load_extracted_document
        p=profile()
        with tempfile.TemporaryDirectory() as folder:
            file=create_pdf(Path(folder))
            conn=get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                        for suffix in ("","_work","_docs","_errors"):
                            cur.execute(
                                f'DROP TABLE IF EXISTS intake_ci."ci_empty_table_deferred{suffix}"'
                            )
            finally:
                conn.close()
            try:
                init(p,apply=True)
                one=stage_one(str(file),p,names(p))
                self.assertEqual(one["status"],"SUCCESS",one)
                self.assertEqual(one["pages"],2)
                self.assertEqual(one["visual_assets_deferred_this_run"],2)
                self.assertFalse(one["visual_enrichment_complete"])
                doc_id=one["doc_id"]
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT page_number,elements FROM
                                intake_ci.ci_empty_table_deferred
                            WHERE doc_id=%s ORDER BY page_number
                        """,(doc_id,))
                        rows=cur.fetchall()
                        self.assertEqual(len(rows),2)
                        for pg,elements in rows:
                            self.assertEqual(
                                sum(1 for element in elements
                                    if "unresolved_region" in element),1
                            )
                        self.assertTrue(any(
                            "source_table" in e for e in rows[1][1]
                        ))
                    struct,_=load_extracted_document(p,doc_id)
                    self.assertEqual(len(struct.pages),2)
                    self.assertEqual(sum(
                        1 for page in struct.pages for e in page.elements
                        if e.unresolved_region
                    ),2)
                finally:
                    conn.close()
                two=stage_one(str(file),p,names(p))
                self.assertEqual(two["status"],"SKIPPED",two)
            finally:
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            for suffix in ("","_work","_docs","_errors"):
                                cur.execute(
                                    f'DROP TABLE IF EXISTS intake_ci."ci_empty_table_deferred{suffix}"'
                                )
                finally:
                    conn.close()


if __name__=="__main__":
    unittest.main()
