"""PDF page-window staging and resume tests; use only disposable test data."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]
EXAMPLE=ROOT/"profiles"/"regulatory_page_stage_example.yml"


def staging_profile():
    from processing_profile import load_profile
    p=load_profile(str(EXAMPLE))
    p["extraction"].update(
        schema="intake_ci",table="ci_pdf_pages",
        page_batch_size=2,ocr_enabled=False,
    )
    p["extracted_content"].update(schema="intake_ci",table="ci_pdf_pages")
    p["output"].update(schema="intake_ci",table="ci_rechunked")
    p.pop("_fingerprint")
    p["_fingerprint"]=hashlib.sha256(
        json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=False
                  ).encode("utf-8")
    ).hexdigest()
    return p


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1" and os.getenv("PGDATABASE")=="embedding_test",
    "Run only against explicitly authorized disposable PostgreSQL",
)
class TestPageStageIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        import fitz
        cls.connection=staticmethod(lambda: psycopg2.connect(
            host=os.environ["PGHOST"],port=os.environ.get("PGPORT","5432"),
            dbname=os.environ["PGDATABASE"],user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
        ))
        cls.dir=tempfile.TemporaryDirectory()
        cls.pdf=Path(cls.dir.name)/"sample.pdf"
        doc=fitz.open()
        for i in range(5):
            page=doc.new_page()
            page.insert_text(
                (50,80),
                ("Protocol Number: ABC-123\n" if i==0 else "")+
                f"Page {i+1} regulatory source content",
                fontsize=12,
            )
        doc.save(str(cls.pdf))
        doc.close()
        cls.profile=staging_profile()
        conn=cls.connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                for name in [
                    "ci_pdf_pages_work","ci_pdf_pages_docs","ci_pdf_pages_errors",
                    "ci_pdf_pages","ci_rechunked","ci_rechunked_status",
                    "ci_rechunked_failures",
                ]:
                    cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'" CASCADE')
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn=cls.connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    for name in [
                        "ci_pdf_pages_work","ci_pdf_pages_docs","ci_pdf_pages_errors",
                        "ci_pdf_pages","ci_rechunked","ci_rechunked_status",
                        "ci_rechunked_failures",
                    ]:
                        cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'" CASCADE')
            conn.close()
        finally:
            cls.dir.cleanup()

    def test_real_native_table_cells_keep_matching_bbox(self):
        import fitz
        from page_stage import stage_one,names
        # The test creates a real vector-grid table, not mocked PDF layout.
        path=Path(self.dir.name)/"table_geometry.pdf"
        doc=fitz.open()
        page=doc.new_page(width=612,height=792)
        xs=[50,310,560]
        ys=[100,156,212,268,324]
        for y in ys:
            page.draw_line((xs[0],y),(xs[-1],y))
        for x in xs:
            page.draw_line((x,ys[0]),(x,ys[-1]))
        for i in range(len(ys)-1):
            page.insert_text((xs[0]+7,ys[i]+24),
                             f"Regulatory parameter alpha {i} - measured")
            page.insert_text((xs[1]+7,ys[i]+24),
                             f"Approved acceptance range {i}: 1 to 15")
        doc.save(str(path))
        doc.close()
        result=stage_one(str(path),self.profile,names(self.profile))
        self.assertEqual(result["status"],"SUCCESS")
        conn=self.connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT chunk_text,elements
                    FROM intake_ci.ci_pdf_pages WHERE doc_id=%s""",
                    (result["doc_id"],))
                text,geometry=cur.fetchone()
                tables=[g for g in geometry if g["type"]=="table"]
                self.assertEqual(len(tables),1)
                self.assertIn("Regulatory parameter",tables[0]["text"])
                self.assertIn("[TABLE]",text)
                bbox=tables[0]["bbox"]
                self.assertLess(abs(bbox[0]-50),2)
                self.assertLess(abs(bbox[2]-560),2)
        conn.close()

    def test_checkpoint_failure_resume_and_geometry_rechunking(self):
        from pdf_processor import PDFProcessor, PageContent
        from page_stage import init, stage_one, names
        from extracted_source import load_extracted_document
        from reuse_materialize import init_output,run

        p=self.profile
        self.assertFalse(init(p,apply=False)["writes"])
        init(p,apply=True)
        n=names(p)
        original=PDFProcessor._process_page
        def fail_one(self,doc,page_num,**kwargs):
            if page_num==2:
                return PageContent(
                    page_number=3,width=0,height=0,
                    error="SYNTHETIC_LAYOUT_ERROR",
                )
            return original(self,doc,page_num,**kwargs)

        with patch.object(PDFProcessor,"_process_page",new=fail_one):
            partial=stage_one(str(self.pdf),p,n)
        self.assertEqual(partial["status"],"INCOMPLETE")
        self.assertEqual(partial["new_pages"],4)
        self.assertEqual(partial["failed_pages"],1)
        conn=self.connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM intake_ci.ci_pdf_pages")
                self.assertEqual(cur.fetchone()[0],0)
                cur.execute("SELECT COUNT(*) FROM intake_ci.ci_pdf_pages_work")
                self.assertEqual(cur.fetchone()[0],4)
                cur.execute("""SELECT COUNT(*) FROM intake_ci.ci_pdf_pages_errors
                    WHERE error_message LIKE '%%SYNTHETIC_LAYOUT_ERROR%%'""")
                self.assertEqual(cur.fetchone()[0],1)
        conn.close()

        result=stage_one(str(self.pdf),p,n)
        self.assertEqual(result["status"],"SUCCESS")
        self.assertEqual(result["reused_pages"],4)
        self.assertEqual(result["new_pages"],1)
        self.assertEqual(stage_one(str(self.pdf),p,n)["status"],"SKIPPED")
        conn=self.connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*),min(page_number),max(page_number)
                    FROM intake_ci.ci_pdf_pages""")
                self.assertEqual(cur.fetchone(),(5,1,5))
                cur.execute("SELECT COUNT(*) FROM intake_ci.ci_pdf_pages_work")
                self.assertEqual(cur.fetchone()[0],0)
        conn.close()

        struct,digest=load_extracted_document(p,result["doc_id"])
        self.assertEqual(len(struct.pages),5)
        self.assertEqual(struct.page_count,5)
        self.assertEqual(struct.pages[0].elements[0].element_type.value,"text")
        self.assertGreater(struct.pages[0].elements[0].bbox.x1,0)
        self.assertIn("Protocol Number",struct.pages[0].elements[0].text)

        init_output(p,apply=True)
        summary=run(p,max_docs=10,workers=1)
        self.assertEqual(summary["SUCCESS"],1)
        conn=self.connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*),bool_and(chunk_vector IS NULL),
                    max(page_end) FROM intake_ci.ci_rechunked""")
                count,novectors,last=cur.fetchone()
                self.assertGreater(count,0)
                self.assertTrue(novectors)
                self.assertEqual(last,5)
                cur.execute("""SELECT document_metadata,metadata_evidence
                    FROM intake_ci.ci_rechunked ORDER BY chunk_index LIMIT 1""")
                meta,evidence=cur.fetchone()
                self.assertEqual(meta["protocol_number"],"ABC-123")
                self.assertEqual(evidence["protocol_number"]["page"],1)
        conn.close()


class TestStageValidation(unittest.TestCase):
    def test_staging_profile_is_opt_in_and_embedded_profile_stays_stable(self):
        from processing_profile import load_profile
        p=staging_profile()
        self.assertEqual(p["extraction"]["ocr_batch_images"],8)
        self.assertTrue(p["extraction"]["enabled"])
        old=load_profile(str(ROOT/"profiles"/"immbot_approved_1536.json"))
        self.assertNotIn("extraction",old)
        self.assertEqual(old["embedding"]["tpm"],900000)

    def test_throttle_tuning_does_not_invalidate_extraction_checkpoints(self):
        from page_stage import extraction_fingerprint
        p=staging_profile()
        baseline=extraction_fingerprint(p)
        p["extraction"].update(
            ocr_workers=12,ocr_batch_images=16,
            ocr_requests_rpm=250,ocr_images_ipm=250,
            page_batch_size=8,max_window_raster_bytes=90_000_000,
            chart_workers=8,ocr_timeout_seconds=180,
        )
        p["chunking"]["max_tokens"]=1200
        p["embedding"]["input_fields"]=["chunk_text"]
        self.assertEqual(extraction_fingerprint(p),baseline)
        p["extraction"]["ocr_confidence_min"]=0.9
        self.assertNotEqual(extraction_fingerprint(p),baseline)
        p["extraction"]["ocr_confidence_min"]=0.6
        p["extraction"]["table_summaries_enabled"]=True
        self.assertNotEqual(extraction_fingerprint(p),baseline)

    def test_page_stage_is_read_only_when_init_without_apply(self):
        from page_stage import init
        p=staging_profile()
        self.assertFalse(init(p,apply=False)["writes"])

    def test_pdf_windows_adapt_to_image_bytes_and_skip_checkpoints(self):
        from pdf_processor import PDFProcessor,PageContent,PageElement,BBox,ElementType
        from types import SimpleNamespace
        class FakeDocument:
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            is_encrypted=False
            page_count=5
        class FakePlumber:
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            pages=[SimpleNamespace(close=lambda:None) for i in range(5)]
        calls=[]
        def fake_page(self, doc, index, **kw):
            calls.append(index+1)
            return PageContent(
                page_number=index+1,width=600,height=800,
                elements=[PageElement(
                    ElementType.IMAGE,BBox(0,0,100,100),
                    image_bytes=b"x"*6000000,
                )],
            )
        proc=PDFProcessor("fake.pdf","doc","fake.pdf",strict=True)
        with patch("pdf_processor._fitz",
                   return_value=SimpleNamespace(open=lambda path:FakeDocument())):
            with patch("pdf_processor._pdfplumber",
                       return_value=SimpleNamespace(open=lambda path:FakePlumber())):
                with patch.object(PDFProcessor,"_extract_toc",return_value=[]):
                    with patch.object(PDFProcessor,"_process_page",new=fake_page):
                        batches=list(proc.iter_page_batches(
                            batch_pages=4,completed_pages={2},
                            max_window_raster_bytes=10_000_000,
                        ))
        self.assertEqual(calls,[1,3,4,5])
        self.assertEqual([len(b.pages) for b in batches],[1,1,1,1])
        self.assertEqual(sum(len(b.pages) for b in batches),4)

    def test_nontext_significant_image_requires_review_not_success(self):
        from pdf_processor import PageContent,PageElement,BBox,ElementType
        from vision_ocr import OcrResult
        from page_stage import _ocr_pages,settings
        p=staging_profile()
        p["extraction"]["ocr_enabled"]=True
        cfg=settings(p)
        item=PageElement(
            ElementType.IMAGE,BBox(0,0,120,120),image_bytes=b"picture"
        )
        page=PageContent(
            page_number=1,width=600,height=800,elements=[item],is_blank=False
        )
        with patch("vision_ocr.batch_document_text_detection",
                   return_value=[OcrResult("",0.0,0,"",True)]):
            errors,processed=_ocr_pages([page],cfg,limiter=None)
        self.assertEqual(processed,1)
        self.assertIn(1,errors)
        self.assertIn("IMAGE_REGION_UNRESOLVED",str(errors[1]))

    def test_chart_ocr_and_description_are_independent_bounded_stages(self):
        from pdf_processor import PageContent,PageElement,BBox,ElementType
        from vision_ocr import OcrResult
        from page_stage import _ocr_pages,settings
        p=staging_profile()
        p["extraction"]["ocr_enabled"]=True
        p["extraction"]["chart_workers"]=2
        cfg=settings(p)
        elements=[
            PageElement(ElementType.CHART,BBox(0,0,100,100),image_bytes=b"chart")
            for _ in range(4)
        ]
        page=PageContent(
            page_number=1,width=600,height=800,elements=elements,
            is_blank=False,needs_ocr=False,
        )
        with patch("vision_ocr.batch_document_text_detection",
                   return_value=[
                       OcrResult("X=10 Y=20",0.95,2,"en",True)
                       for _ in range(4)
                   ]) as batch:
            with patch("vision_ocr._describe_chart_with_gpt4o",
                       return_value="Bar chart: X versus Y") as model:
                errs,processed=_ocr_pages([page],cfg,limiter=None)
        self.assertEqual(errs,{})
        self.assertEqual(processed,4)
        self.assertEqual(batch.call_count,1)
        self.assertEqual(model.call_count,4)
        self.assertTrue(all(
            "Bar chart" in el.text and "X=10 Y=20" in el.text
            and el.image_bytes==b""
            for el in elements
        ))

    def test_groups_keep_batch_size_and_byte_budget(self):
        from page_stage import _images_in_groups,settings
        from pdf_processor import ElementType
        from types import SimpleNamespace
        p=staging_profile()
        p["extraction"]["ocr_batch_images"]=3
        p["extraction"]["ocr_max_batch_bytes"]=10
        cfg=settings(p)
        elements=[
            SimpleNamespace(element_type=ElementType.IMAGE,image_bytes=b"12345")
            for i in range(5)
        ]
        pages=[SimpleNamespace(page_number=1,elements=elements,
                               error="",is_blank=False)]
        groups,errors=_images_in_groups(pages,cfg)
        self.assertEqual([len(x) for x in groups],[2,2,1])
        self.assertEqual(errors,{})


if __name__=="__main__":
    unittest.main()
