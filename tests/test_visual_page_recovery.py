"""One-page recovery readiness, without changing clinical-review requirements."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB") == "1"
    and os.getenv("PGDATABASE") == "embedding_test",
    "Runs only against disposable CI PostgreSQL",
)
class VisualPageRecoveryPostgresTests(unittest.TestCase):
    def test_missing_page_and_original_figure_verified_without_writes(self):
        import fitz
        import yaml

        from db import get_conn
        from figure_recovery import prepare_page
        from page_stage import init as init_pages, extraction_fingerprint, source_hash
        from processing_profile import load_profile
        from test_figure_recovery import FigureRecoveryTests
        from visual_task_ledger import LedgerSettings, init as init_ledger, register
        from visual_page_recovery import plan_recovery

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = FigureRecoveryTests()._fixture(folder)
            with fitz.open(pdf_path) as pdf:
                last = pdf.new_page()
                last.insert_text((70, 70), "Second page already checkpointed")
                pdf.saveIncr()

            base = load_profile("profiles/regulatory_page_stage_example.yml")
            base["extraction"].update(
                schema="intake_ci",
                table="ci_visual_gate_pages",
                ocr_enabled=False,
                capture_table_structure=True,
                capture_rich_text=True,
            )
            base["extracted_content"].update(
                schema="intake_ci", table="ci_visual_gate_pages"
            )
            base["output"].update(
                schema="intake_ci", table="ci_visual_gate_chunks"
            )
            base["embedding"]["enabled"] = False
            profile_path = folder / "native.yml"
            profile_path.write_text(
                yaml.safe_dump({k:v for k,v in base.items() if k!="_fingerprint"},
                               sort_keys=False),
                encoding="utf-8",
            )
            profile = load_profile(str(profile_path))
            fingerprint = extraction_fingerprint(profile)

            settings = LedgerSettings(
                schema="intake_ci",table="ci_visual_gate_tasks"
            )
            visual_profile = folder / "visual.yml"
            visual_profile.write_text(
                yaml.safe_dump({
                    "version": 1, "schema": "intake_ci",
                    "table": "ci_visual_gate_tasks",
                }),
                encoding="utf-8",
            )
            conn = get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                        for name in (
                            "ci_visual_gate_pages",
                            "ci_visual_gate_pages_work",
                            "ci_visual_gate_pages_docs",
                            "ci_visual_gate_pages_errors",
                            "ci_visual_gate_tasks",
                        ):
                            cur.execute(f'DROP TABLE IF EXISTS intake_ci."{name}"')
            finally:
                conn.close()
            try:
                init_pages(profile,apply=True)
                init_ledger(settings,apply=True)
                figure = prepare_page(
                    pdf_path,1,folder/"evidence",
                    native_extraction_hash=fingerprint,
                )[0]
                registry = register(
                    settings,manifest_path=Path(figure["manifest"]),
                    pdf_path=pdf_path,extraction_hash=fingerprint,apply=True,
                )
                doc_id=hashlib.sha256(str(pdf_path.resolve()).encode("utf-8")).hexdigest()
                checksum=source_hash(str(pdf_path))
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO intake_ci.ci_visual_gate_pages_work (
                                    doc_id,source_hash,extraction_hash,page_number,
                                    page_end,page_width,page_height,page_rotation,
                                    chunk_text,elements,section_title,file_path,
                                    file_name,content_sha256
                                ) VALUES (%s,%s,%s,2,2,612,792,0,
                                          'checkpointed native page','[]'::jsonb,
                                          '',%s,%s,%s)
                            """,(doc_id,checksum,fingerprint,str(pdf_path.resolve()),
                                 pdf_path.name,hashlib.sha256(b'page-2').hexdigest()))
                finally:
                    conn.close()

                report = plan_recovery(
                    pdf_path=str(pdf_path),
                    native_profile_path=str(profile_path),
                    visual_profile_path=str(visual_profile),
                    page_number=1,
                    expected_visual_task_id=registry["visual_task_id"],
                )
                self.assertEqual(report["consistency"],"PASS")
                self.assertEqual(report["checkpoint_count"],1)
                self.assertEqual(report["missing_checkpoint_pages"],[1])
                self.assertEqual(report["visual_status"],"REVIEW_REQUIRED")
                self.assertFalse(report["database_writes"])
                self.assertEqual(
                    report["page_publication"],"BLOCKED_PENDING_EXPLICIT_REVIEW"
                )
                self.assertTrue(all(report["checks"].values()))

                # A bad checkpoint state must fail validation; it cannot be
                # smuggled in as approved or automatically published.
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                DELETE FROM intake_ci.ci_visual_gate_pages_work
                                WHERE doc_id=%s
                            """,(doc_id,))
                finally:
                    conn.close()
                bad = plan_recovery(
                    pdf_path=str(pdf_path),
                    native_profile_path=str(profile_path),
                    visual_profile_path=str(visual_profile),
                    page_number=1,
                )
                self.assertEqual(bad["consistency"],"REVIEW_REQUIRED")
                self.assertFalse(bad["checks"]["exactly_one_missing_page"])
            finally:
                conn=get_conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            for name in (
                                "ci_visual_gate_pages",
                                "ci_visual_gate_pages_work",
                                "ci_visual_gate_pages_docs",
                                "ci_visual_gate_pages_errors",
                                "ci_visual_gate_tasks",
                            ):
                                cur.execute(f'DROP TABLE IF EXISTS intake_ci."{name}"')
                finally:
                    conn.close()


if __name__=="__main__":
    unittest.main()
