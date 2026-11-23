"""Process workers on disposable PostgreSQL; original pages and idempotency."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB") == "1"
    and os.getenv("PGDATABASE") == "embedding_test",
    "Process-worker database test is restricted to disposable CI PostgreSQL",
)
class NativeProcessWorkerTests(unittest.TestCase):
    def test_two_documents_parallel_publish_and_idempotent_replay(self):
        import fitz
        from db import get_conn
        from page_stage import init,run
        from processing_profile import load_profile

        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i in range(2):
                with fitz.open() as pdf:
                    for page_index in range(2):
                        page=pdf.new_page()
                        page.insert_text((70,75),
                            f"Subject {i}: native regulatory text page {page_index+1}.")
                    pdf.save(root/f"native-{i}.pdf")
            profile=load_profile("profiles/regulatory_page_stage_example.yml")
            profile["extraction"].update(
                schema="intake_ci",table="ci_native_process_pool",
                page_batch_size=1,
                worker_backend="processes",
                worker_recycle_documents=0,
                visual_processing="preserve_defer",
                capture_rich_text=True,
                capture_table_structure=True,
                ocr_enabled=False,
                chart_descriptions_enabled=False,
                table_summaries_enabled=False,
            )
            profile["extracted_content"].update(
                schema="intake_ci",table="ci_native_process_pool",
            )
            profile["output"].update(
                schema="intake_ci",table="ci_native_process_chunks",
            )
            profile["embedding"]["enabled"]=False
            conn=get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                        for suffix in ("","_work","_docs","_errors"):
                            cur.execute(
                                f'DROP TABLE IF EXISTS intake_ci."ci_native_process_pool{suffix}"'
                            )
            finally:
                conn.close()
            try:
                init(profile,apply=True)
                counts=run(profile,root=str(root),max_docs=2,doc_workers=2)
                self.assertEqual(counts["SUCCESS"],2,counts)
                self.assertEqual(counts["FAILED"],0)
                self.assertEqual(counts["INCOMPLETE"],0)
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*),COUNT(DISTINCT doc_id) "
                                    "FROM intake_ci.ci_native_process_pool")
                        self.assertEqual(cur.fetchone(),(4,2))
                finally:
                    conn.close()
                again=run(profile,root=str(root),max_docs=2,doc_workers=2)
                self.assertEqual(again["SKIPPED"],2,again)
                self.assertEqual(again["SUCCESS"],0)
                profile["extraction"]["ocr_enabled"]=True
                with self.assertRaisesRegex(ValueError,"Process workers require"):
                    run(profile,root=str(root),max_docs=2,doc_workers=2)
            finally:
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            for suffix in ("","_work","_docs","_errors"):
                                cur.execute(
                                    f'DROP TABLE IF EXISTS intake_ci."ci_native_process_pool{suffix}"'
                                )
                finally:
                    conn.close()


if __name__=="__main__":
    unittest.main()
