"""Figure ledger evidence, concurrency, idempotency and privacy barriers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class VisualTaskLedgerTests(unittest.TestCase):
    def setUp(self):
        from figure_recovery import prepare_page
        from test_figure_recovery import FigureRecoveryTests

        self.folder_handle = tempfile.TemporaryDirectory()
        self.folder = Path(self.folder_handle.name)
        self.pdf = FigureRecoveryTests()._fixture(self.folder)
        self.source_hash = "a" * 64
        items = prepare_page(
            self.pdf, 1, self.folder / "evidence",
            native_extraction_hash=self.source_hash,
        )
        self.assertEqual(len(items), 1)
        self.manifest = Path(items[0]["manifest"])

    def tearDown(self):
        self.folder_handle.cleanup()

    def test_manifest_verifies_original_pdf_xrefs_and_png(self):
        from visual_task_ledger import validate_evidence
        evidence = validate_evidence(
            self.manifest, self.pdf, self.source_hash, 65536
        )
        self.assertEqual(evidence["page_number"], 1)
        self.assertEqual(evidence["manifest"]["fragment_count"], 7)
        self.assertEqual(evidence["manifest"]["review_status"], "VISUAL_REVIEW_REQUIRED")
        self.assertEqual(evidence["manifest"]["interpretation_status"], "NOT_PERFORMED")
        self.assertEqual(evidence["manifest"]["visual_task_id"], evidence["visual_task_id"])

    def test_manifest_and_source_mismatch_fail_closed(self):
        from visual_task_ledger import validate_evidence
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            validate_evidence(self.manifest, self.pdf, "b" * 64, 65536)
        saved = self.manifest.read_bytes()
        try:
            data = json.loads(saved)
            data["review_status"] = "APPROVED"
            self.manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Only unreviewed"):
                validate_evidence(self.manifest, self.pdf, self.source_hash, 65536)
        finally:
            self.manifest.write_bytes(saved)

    def test_png_corruption_and_limits_fail_closed(self):
        from visual_task_ledger import validate_evidence
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        png = self.manifest.with_name(data["composite_file"])
        with png.open("ab") as output:
            output.write(b"MODIFIED")
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_evidence(self.manifest, self.pdf, self.source_hash, 65536)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            validate_evidence(self.manifest, self.pdf, self.source_hash, 1024)

    def test_safe_schema_config_no_database_writes_in_dry_run(self):
        import yaml
        from visual_task_ledger import (
            LedgerSettings, claim, init, load_settings, register, release,
        )
        cfg_path = self.folder / "ledger.yml"
        cfg_path.write_text(yaml.safe_dump({
            "version": 1, "schema": "cv_validation_v1",
            "table": "cv_bench_visual_tasks_v1",
        }), encoding="utf-8")
        cfg = load_settings(str(cfg_path))
        self.assertEqual(cfg.relation, '"cv_validation_v1"."cv_bench_visual_tasks_v1"')
        self.assertFalse(init(cfg)["writes"])
        self.assertFalse(register(cfg, manifest_path=self.manifest,
                                  pdf_path=self.pdf, extraction_hash=self.source_hash)["writes"])
        self.assertFalse(claim(cfg, worker_id="worker-1", batch_size=1)["writes"])
        self.assertFalse(release(
            cfg, task_id="a" * 64, worker_id="worker-1",
            lease_token="12345678-1234-1234-1234-123456789abc",
        )["writes"])
        with self.assertRaisesRegex(ValueError, "Unsafe SQL identifier"):
            LedgerSettings(schema="public;DROP", table="evil").validate()
        with self.assertRaisesRegex(ValueError, "Invalid bounded worker identifier"):
            claim(cfg, worker_id="'; DROP TABLE", batch_size=1)
        with self.assertRaisesRegex(ValueError, "Invalid worker claim batch"):
            claim(cfg, worker_id="good", batch_size=999)
        cfg_path.write_text(yaml.safe_dump({
            "version": 1, "schema": "cv_validation_v1", "table": "tbl",
            "protection_override": True,
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unknown visual ledger options"):
            load_settings(str(cfg_path))


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB") == "1"
    and os.getenv("PGDATABASE") == "embedding_test",
    "Only run against disposable PostgreSQL CI database",
)
class VisualLedgerPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from db import get_conn
        from visual_task_ledger import LedgerSettings, init
        cls.settings = LedgerSettings(schema="intake_ci", table="ci_visual_review_tasks")
        conn = get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                    cur.execute("DROP TABLE IF EXISTS intake_ci.ci_visual_review_tasks")
        finally:
            conn.close()
        init(cls.settings, apply=True)

    @classmethod
    def tearDownClass(cls):
        from db import get_conn
        conn = get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS intake_ci.ci_visual_review_tasks")
        finally:
            conn.close()

    def test_idempotent_registration_and_skipped_lock_review_leases(self):
        from concurrent.futures import ThreadPoolExecutor
        from figure_recovery import prepare_page
        from test_figure_recovery import FigureRecoveryTests
        from visual_task_ledger import register, claim, release, status
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf = FigureRecoveryTests()._fixture(folder)
            extraction_hash = "f" * 64
            prepared = prepare_page(pdf, 1, folder / "evidence",
                                    native_extraction_hash=extraction_hash)
            manifest = Path(prepared[0]["manifest"])
            first = register(self.settings, manifest_path=manifest,
                             pdf_path=pdf, extraction_hash=extraction_hash, apply=True)
            second = register(self.settings, manifest_path=manifest,
                              pdf_path=pdf, extraction_hash=extraction_hash, apply=True)
            self.assertTrue(first["inserted"])
            self.assertFalse(second["inserted"])
            self.assertEqual(first["visual_task_id"], second["visual_task_id"])
            self.assertEqual(first["status"], "REVIEW_REQUIRED")
            doc_id = __import__("hashlib").sha256(str(pdf.resolve()).encode("utf-8")).hexdigest()
            self.assertEqual(len(status(self.settings, doc_id=doc_id)), 1)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda i: claim(self.settings, worker_id=f"ci-worker-{i}",
                                    batch_size=1, apply=True), range(2)))
            self.assertEqual(sorted(x["claimed"] for x in results), [0, 1])
            winner = next(x for x in results if x["claimed"] == 1)
            task_id = winner["tasks"][0]["visual_task_id"]
            self.assertEqual(status(self.settings, doc_id=doc_id)[0]["status"], "LEASED")
            with self.assertRaisesRegex(ValueError, "Lease missing"):
                release(self.settings, task_id=task_id,
                        worker_id=winner["worker_id"],
                        lease_token="12345678-1234-1234-1234-123456789abc",
                        apply=True)
            released = release(self.settings, task_id=task_id,
                               worker_id=winner["worker_id"],
                               lease_token=winner["lease_token"], apply=True)
            self.assertTrue(released["released"])
            self.assertEqual(status(self.settings, doc_id=doc_id)[0]["status"], "REVIEW_REQUIRED")
            self.assertEqual(status(self.settings, doc_id=doc_id)[0]["claim_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
