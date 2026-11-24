"""Process workers on disposable PostgreSQL; original pages and idempotency."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def _process_native_pdf(path):
    from pdf_processor import PDFProcessor
    processor=PDFProcessor(
        str(path),"synthetic","synthetic.pdf",
        strict=True,preserve_uninterpreted_visuals=True,
    )
    pages=[]
    for window in processor.iter_page_batches(batch_pages=2):
        pages.extend(p.page_number for p in window.pages if not p.error)
    return pages


class ProcessSpawnSmokeTests(unittest.TestCase):
    def test_open_handle_capture_reuses_native_checkpoint_without_source_copy(self):
        import fitz
        from pdf_processor import PDFProcessor
        from visual_evidence import OpenPageCapture, _source_state, digest_file, verify
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=root/"source.pdf"
            with fitz.open() as doc:
                for i in range(3):
                    p=doc.new_page(width=200,height=200)
                    p.insert_text((20,40),f"Source page {i+1}")
                    p.draw_line((20,70),(150,100))
                doc.save(path)
            cfg={"root":str(root/"retained"),"policy_id":"synthetic-test",
                 "renderer":str(fitz.VersionBind),"authorized_page_derivatives":True,
                 "visual_settings":{"dpi":150,"workers":1}}
            state=_source_state(path)
            sha=digest_file(path)
            p=PDFProcessor(str(path),"synthetic","source.pdf",strict=True,
                           preserve_uninterpreted_visuals=True)
            observer=OpenPageCapture(path,sha,state,3,cfg)
            pages=[page.page_number for w in p.iter_page_batches(
                   batch_pages=2,completed_pages={1},page_observer=observer.consume)
                   for page in w.pages]
            self.assertEqual(pages,[2,3])
            result=observer.report()
            self.assertEqual(result["rendered_pages"],3)
            self.assertEqual(digest_file(path),sha)
            replay=OpenPageCapture(path,sha,state,3,cfg)
            with patch.object(fitz.Page,"get_pixmap",side_effect=AssertionError("re-render")):
                self.assertEqual(list(p.iter_page_batches(completed_pages={1,2,3},
                                  page_observer=replay.consume)),[])
            self.assertEqual(replay.report()["skipped_verified_pages"],3)
            self.assertFalse(replay.report()["derivative_writes"])
            path.unlink()
            self.assertEqual(verify(root/"retained",result["capture_id"],1,3,
                                    settings=cfg["visual_settings"])["verified_pages"],3)
            self.assertFalse(list((root/"retained").rglob("*.pdf")))

    def test_native_pdf_documents_parse_in_distinct_spawned_processes(self):
        import concurrent.futures
        import multiprocessing
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            paths=[]
            for doc in range(2):
                path=Path(tmp)/f"document-{doc}.pdf"
                with fitz.open() as pdf:
                    for page_num in range(2):
                        page=pdf.new_page()
                        page.insert_text((70,70),f"Native text document {doc} page {page_num}.")
                    pdf.save(path)
                paths.append(str(path))
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=2,mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                results=list(pool.map(_process_native_pdf,paths))
            self.assertEqual(results,[[1,2],[1,2]])


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
                from page_stage import extraction_fingerprint
                from visual_evidence import capture_identity, _capture_id, digest_file, verify
                from processing_profile import validate_profile
                fingerprint=extraction_fingerprint(profile)
                evidence_root=root/"retained"
                cfg={"root":str(evidence_root),"policy_id":"synthetic-test",
                     "renderer":str(fitz.VersionBind),"authorized_page_derivatives":True,
                     "visual_settings":{"dpi":150,"workers":1}}
                profile["extraction"]["retained_evidence"]=cfg
                validate_profile(profile)
                self.assertEqual(extraction_fingerprint(profile),fingerprint)
                # Existing publications are not rewritten during evidence backfill.
                backfill=run(profile,root=str(root),max_docs=2,doc_workers=2)
                self.assertEqual(backfill["SKIPPED"],2,backfill)
                for source in sorted(root.glob("*.pdf")):
                    cid=_capture_id(capture_identity(digest_file(source),cfg["visual_settings"],
                                                     cfg["policy_id"],cfg["renderer"]))
                    self.assertEqual(verify(evidence_root,cid,1,2,
                                            settings=cfg["visual_settings"])["verified_pages"],2)
                # Fresh native extraction must also capture evidence before publication.
                new=root/"new.pdf"
                with fitz.open() as doc:
                    page=doc.new_page()
                    page.insert_text((70,75),"Fresh native evidence document.")
                    doc.save(new)
                fresh=run(profile,file_path=str(new),max_docs=1,doc_workers=1)
                self.assertEqual(fresh["SUCCESS"],1,fresh)
                cid=_capture_id(capture_identity(digest_file(new),cfg["visual_settings"],
                                                 cfg["policy_id"],cfg["renderer"]))
                self.assertEqual(verify(evidence_root,cid,1,1,
                                        settings=cfg["visual_settings"])["verified_pages"],1)
                self.assertEqual(run(profile,file_path=str(new),max_docs=1,doc_workers=1)["SKIPPED"],1)
                conn=get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*),COUNT(DISTINCT doc_id) FROM intake_ci.ci_native_process_pool")
                        self.assertEqual(cur.fetchone(),(5,3))
                finally:
                    conn.close()
                profile["extraction"].pop("retained_evidence")
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
