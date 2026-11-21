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

    def test_page_stage_is_read_only_when_init_without_apply(self):
        from page_stage import init
        p=staging_profile()
        self.assertFalse(init(p,apply=False)["writes"])

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
