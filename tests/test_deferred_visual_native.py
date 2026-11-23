"""Fast native extraction with source-preserved, non-interpreted PDF graphics.

Never claims OCR/chart values were extracted, never silently discards image-only
pages, and never permits the deferred mode to trigger network OCR calls.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def sample_pdf(folder: Path) -> Path:
    import fitz
    from PIL import Image, ImageDraw
    file=folder/"visual-deferred.pdf"
    with fitz.open() as doc:
        page=doc.new_page(width=612,height=792)
        page.insert_text((65,85),"Figure 14: Synthetic two-curve chart")
        for index in range(7):
            image=Image.new("RGB",(480,36),"white")
            ImageDraw.Draw(image).line((0,18,479,16+index),fill="black",width=2)
            encoded=io.BytesIO()
            image.save(encoded,format="PNG")
            top=100+index*32
            page.insert_image(
                fitz.Rect(93.5,top,539.9,top+32),
                stream=encoded.getvalue(),
            )
        page.insert_text((65,400),"Native regulatory text is available")
        second=doc.new_page(width=612,height=792)
        image=Image.new("RGB",(640,600),"white")
        ImageDraw.Draw(image).text((15,50),"SCANNED SOURCE",fill="black")
        payload=io.BytesIO()
        image.save(payload,format="PNG")
        second.insert_image(
            fitz.Rect(0,0,612,792),stream=payload.getvalue()
        )
        doc.save(file)
    return file


class DeferredVisualNativeUnitTests(unittest.TestCase):
    def setUp(self):
        self.folder_obj=tempfile.TemporaryDirectory()
        self.folder=Path(self.folder_obj.name)
        self.pdf=sample_pdf(self.folder)

    def tearDown(self):
        self.folder_obj.cleanup()

    def _profile(self):
        from processing_profile import load_profile
        p=load_profile("profiles/regulatory_page_stage_example.yml")
        p["extraction"].update(
            schema="intake_ci",table="ci_pdf_preserved_visuals",
            capture_rich_text=True,
            visual_processing="preserve_defer",
            max_visual_regions_per_page=256,
            ocr_enabled=False,
            chart_descriptions_enabled=False,
        )
        p["extracted_content"].update(
            schema="intake_ci",table="ci_pdf_preserved_visuals"
        )
        p["output"].update(schema="intake_ci",table="ci_preserved_chunks")
        p["embedding"]["enabled"]=False
        return p

    def test_one_group_keeps_all_source_xrefs_without_raster(self):
        import json
        from pdf_processor import PDFProcessor,ElementType
        from page_stage import _ocr_pages,_page_row,settings
        proc=PDFProcessor(
            str(self.pdf),"id",self.pdf.name,
            strict=True,preserve_uninterpreted_visuals=True,
        )
        windows=list(proc.iter_page_batches(batch_pages=2))
        self.assertEqual(len(windows),1)
        doc=windows[0]
        self.assertEqual(len(doc.pages),2)
        self.assertTrue(all(not page.error for page in doc.pages))
        first=doc.pages[0]
        group=[x for x in first.elements if x.visual_asset]
        self.assertEqual(len(group),1)
        self.assertEqual(group[0].visual_asset["fragment_count"],7)
        self.assertEqual(group[0].visual_asset["group_type"],"contiguous_image_strips")
        self.assertEqual(len({x["xref"] for x in group[0].visual_asset["fragments"]}),7)
        self.assertFalse(group[0].image_bytes)
        self.assertFalse(group[0].text)
        self.assertEqual(group[0].element_type,ElementType.IMAGE)
        self.assertIn("Native regulatory text",first.raw_text)
        second=doc.pages[1]
        self.assertEqual(second.raw_text,"")
        self.assertTrue(any(x.visual_asset for x in second.elements))
        with patch("vision_ocr.batch_document_text_detection",
                   side_effect=AssertionError("OCR must not be called")):
            errs,used=_ocr_pages(doc.pages,settings(self._profile()),limiter=None)
        self.assertEqual(errs,{})
        self.assertEqual(used,0)
        source="b"*64
        fingerprint="a"*64
        row1=_page_row(first,doc,source,fingerprint)
        row2=_page_row(second,doc,source,fingerprint)
        self.assertTrue(row1[8])
        self.assertEqual(row2[8],"")  # Image-only native text explicitly empty.
        parsed=json.loads(row1[9])
        visual=next(e["visual_asset"] for e in parsed if "visual_asset" in e)
        self.assertEqual(visual["fragment_count"],7)
        self.assertEqual(visual["numeric_interpretation"],"NOT_PERFORMED")

    def test_new_policy_is_semantically_versioned(self):
        from page_stage import extraction_fingerprint
        from processing_profile import validate_profile
        p=self._profile()
        valid=extraction_fingerprint(p)
        self.assertEqual(valid,extraction_fingerprint(p))
        p["extraction"]["visual_processing"]="blocking"
        self.assertNotEqual(valid,extraction_fingerprint(p))
        p["extraction"]["visual_processing"]="invalid"
        with self.assertRaisesRegex(ValueError,"visual_processing"):
            validate_profile(p)
        p["extraction"]["visual_processing"]="preserve_defer"
        p["extraction"]["ocr_enabled"]=True
        with self.assertRaisesRegex(ValueError,"native-only"):
            validate_profile(p)
        p["extraction"]["ocr_enabled"]=False
        p["extraction"]["max_visual_regions_per_page"]=1
        with self.assertRaisesRegex(ValueError,"max_visual_regions_per_page"):
            validate_profile(p)

    def test_limit_fails_closed_without_silently_dropping_images(self):
        from pdf_processor import PDFProcessor
        proc=PDFProcessor(
            str(self.pdf),"id",self.pdf.name,strict=True,
            preserve_uninterpreted_visuals=True,
            max_visual_regions_per_page=6,
        )
        window=next(proc.iter_page_batches(batch_pages=1))
        self.assertEqual(len(window.pages),1)
        self.assertIn("VISUAL_REGION_LIMIT_EXCEEDED",window.pages[0].error)


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1"
    and os.getenv("PGDATABASE")=="embedding_test",
    "PostgreSQL publication test restricted to disposable CI database",
)
class DeferredVisualNativePostgresTests(unittest.TestCase):
    def test_publish_and_resume_without_an_ocr_or_approval_ledger(self):
        import json
        from db import get_conn
        from page_stage import init,stage_one,names
        from processing_profile import load_profile

        with tempfile.TemporaryDirectory() as tmp:
            pdf=sample_pdf(Path(tmp))
            p=load_profile("profiles/regulatory_page_stage_example.yml")
            p["extraction"].update(
                schema="intake_ci",table="ci_deferred_visual_stage",
                capture_rich_text=True,capture_table_structure=True,
                page_batch_size=1,visual_processing="preserve_defer",
                ocr_enabled=False,chart_descriptions_enabled=False,
                max_visual_regions_per_page=256,
            )
            p["extracted_content"].update(
                schema="intake_ci",table="ci_deferred_visual_stage",
            )
            p["output"].update(schema="intake_ci",table="ci_deferred_visual_chunks")
            p["embedding"]["enabled"]=False
            conn=get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                        for name in (
                            "ci_deferred_visual_stage",
                            "ci_deferred_visual_stage_work",
                            "ci_deferred_visual_stage_docs",
                            "ci_deferred_visual_stage_errors",
                        ):
                            cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'"')
            finally:
                conn.close()
            try:
                init(p,apply=True)
                first=stage_one(str(pdf),p,names(p))
                self.assertEqual(first["status"],"SUCCESS",first)
                self.assertEqual(first["pages"],2)
                self.assertEqual(first["visual_assets_deferred_this_run"],2)
                self.assertFalse(first["visual_enrichment_complete"])
                self.assertEqual(first["ocr_images"],0)
                second=stage_one(str(pdf),p,names(p))
                self.assertEqual(second["status"],"SKIPPED")
                doc_id=first["doc_id"]
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT page_number,chunk_text,elements
                            FROM intake_ci.ci_deferred_visual_stage
                            WHERE doc_id=%s ORDER BY page_number
                        """,(doc_id,))
                        rows=cur.fetchall()
                        self.assertEqual(len(rows),2)
                        self.assertEqual(rows[1][1],"")
                        visual=[
                            item["visual_asset"]
                            for item in rows[0][2]
                            if "visual_asset" in item
                        ]
                        self.assertEqual(len(visual),1)
                        self.assertEqual(visual[0]["fragment_count"],7)
                        from extracted_source import load_extracted_document
                        struct,_=load_extracted_document(p,doc_id)
                        round_trip=[
                            el.visual_asset for page in struct.pages
                            for el in page.elements if el.visual_asset
                        ]
                        self.assertEqual(len(round_trip),2)
                        self.assertEqual(round_trip[0]["fragment_count"],7)
                        self.assertEqual(round_trip[1]["status"],
                                         "PENDING_OCR_OR_VISUAL_ENRICHMENT")
                        cur.execute("""
                            SELECT COUNT(*) FROM intake_ci.ci_deferred_visual_stage_work
                            WHERE doc_id=%s
                        """,(doc_id,))
                        self.assertEqual(cur.fetchone()[0],0)
                finally:
                    conn.close()
            finally:
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            for name in (
                                "ci_deferred_visual_stage",
                                "ci_deferred_visual_stage_work",
                                "ci_deferred_visual_stage_docs",
                                "ci_deferred_visual_stage_errors",
                            ):
                                cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'"')
                finally:
                    conn.close()


if __name__=="__main__":
    unittest.main()
