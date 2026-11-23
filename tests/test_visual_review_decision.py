"""Auditable source-image fidelity approvals, not clinical interpretations."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class VisualReviewDecisionUnitTests(unittest.TestCase):
    def test_settings_validate_review_table_and_attestation(self):
        import yaml
        from visual_review_decision import (
            ATTESTATION, ReviewSettings, init, load_settings, record,
        )
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"review.yml"
            p.write_text(yaml.safe_dump({
                "version":1,"schema":"cv_validation_v1",
                "ledger_table":"cv_bench_visual_tasks_v1",
                "review_table":"cv_bench_visual_reviews_v1",
            }),encoding="utf-8")
            cfg=load_settings(str(p))
            self.assertEqual(cfg.review_relation,'"cv_validation_v1"."cv_bench_visual_reviews_v1"')
            self.assertFalse(init(cfg)["writes"])
            with self.assertRaisesRegex(ValueError,"separate"):
                ReviewSettings(schema="x",ledger_table="y",review_table="y").validate()
            p.write_text(yaml.safe_dump({
                "version":1,"schema":"x","ledger_table":"y",
                "review_table":"z","drop_all_tables":True,
            }),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"Unknown reviewer"):
                load_settings(str(p))
            self.assertIn("NO_NUMERIC_INTERPRETATION",ATTESTATION)

    def test_unreviewed_manifests_never_auto_grant_acceptance(self):
        from figure_recovery import prepare_page
        from test_figure_recovery import FigureRecoveryTests
        from visual_review_decision import (
            ATTESTATION, ReviewSettings, record,
        )
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            pdf=FigureRecoveryTests()._fixture(folder)
            item=prepare_page(pdf,1,folder/"evidence",native_extraction_hash="a"*64)[0]
            cfg=ReviewSettings(schema="s",ledger_table="t",review_table="r")
            common=dict(
                manifest_path=item["manifest"],pdf_path=pdf,extraction_hash="a"*64,
                reviewer_id="internal.123",lease_owner="reviewer-123",
                lease_token="12345678-1234-1234-1234-123456789abc",
                review_note="Compared complete legend, caption and curves to original PDF.",
            )
            with self.assertRaisesRegex(ValueError,"attestation"):
                record(cfg,attestation_code="I_READ_EVERY_PLOTTED_POINT",**common)
            answer=record(cfg,attestation_code=ATTESTATION,apply=False,**common)
            self.assertEqual(answer["decision"],"VISUAL_FIDELITY_ACCEPTED")
            self.assertEqual(answer["numeric_interpretation"],"NOT_PERFORMED")
            self.assertFalse(answer["writes"])


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB") == "1"
    and os.getenv("PGDATABASE") == "embedding_test",
    "Only against disposable PostgreSQL CI",
)
class VisualReviewDecisionPostgresTests(unittest.TestCase):
    def test_claim_review_idempotence_and_prevention_of_reclaim(self):
        import yaml

        from db import get_conn
        from figure_recovery import prepare_page
        from test_figure_recovery import FigureRecoveryTests
        from visual_task_ledger import (
            LedgerSettings,init as init_ledger,register,claim,status,
        )
        from visual_review_decision import (
            ATTESTATION,ReviewSettings,init as init_review,record,inspect,
        )
        ledger=LedgerSettings(
            schema="intake_ci",table="ci_source_figures",
            review_table="ci_source_figure_reviews",
        )
        review=ReviewSettings(
            schema="intake_ci",ledger_table=ledger.table,
            review_table=ledger.review_table,
        )
        conn=get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                    cur.execute("DROP TABLE IF EXISTS intake_ci.ci_source_figure_reviews")
                    cur.execute("DROP TABLE IF EXISTS intake_ci.ci_source_figures")
        finally:
            conn.close()
        try:
            init_ledger(ledger,apply=True)
            init_review(review,apply=True)
            with tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp)
                pdf=FigureRecoveryTests()._fixture(folder)
                prepared=prepare_page(
                    pdf,1,folder/"figures",
                    native_extraction_hash="b"*64,
                )[0]
                registered=register(
                    ledger,manifest_path=Path(prepared["manifest"]),
                    pdf_path=pdf,extraction_hash="b"*64,apply=True,
                )
                claim_result=claim(
                    ledger,worker_id="manual-review-1",batch_size=1,apply=True,
                )
                self.assertEqual(claim_result["claimed"],1)
                attrs=dict(
                    manifest_path=Path(prepared["manifest"]),
                    pdf_path=pdf,extraction_hash="b"*64,
                    reviewer_id="regulatory.reviewer.17",
                    attestation_code=ATTESTATION,
                    review_note="Compared chart, axes, legend, caption to source PDF.",
                    lease_owner=claim_result["worker_id"],
                    lease_token=claim_result["lease_token"],
                )
                first=record(review,apply=True,**attrs)
                self.assertTrue(first["inserted"])
                self.assertEqual(first["native_pages_modified"],0)
                repeated=record(review,apply=True,**attrs)
                self.assertFalse(repeated["inserted"])
                with self.assertRaisesRegex(ValueError,"Immutable review"):
                    record(review,apply=True,**{**attrs,"reviewer_id":"other.reviewer"})
                doc_id=__import__("hashlib").sha256(
                    str(pdf.resolve()).encode("utf-8")
                ).hexdigest()
                audit=inspect(review,doc_id=doc_id)
                self.assertEqual(len(audit["tasks"]),1)
                self.assertEqual(audit["tasks"][0]["visual_fidelity_decision"],
                                 "VISUAL_FIDELITY_ACCEPTED")
                self.assertEqual(audit["tasks"][0]["numeric_interpretation"],
                                 "NOT_VERIFIED")
                # Review tasks are no longer consumed by the queue even if
                # the lease eventually expires or is explicitly released.
                self.assertEqual(claim(
                    ledger,worker_id="other-worker",batch_size=1,apply=True
                )["claimed"],0)
        finally:
            conn=get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("DROP TABLE IF EXISTS intake_ci.ci_source_figure_reviews")
                        cur.execute("DROP TABLE IF EXISTS intake_ci.ci_source_figures")
            finally:
                conn.close()


if __name__=="__main__":
    unittest.main()
