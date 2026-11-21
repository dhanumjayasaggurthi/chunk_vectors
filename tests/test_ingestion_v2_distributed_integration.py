"""End-to-end native PDF distribution over a disposable PostgreSQL database."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import fitz

from ingestion_v2.distributed_cli import (
    _verify_source, register_document, run_worker,
)
from ingestion_v2.ledger import Ledger
from ingestion_v2.plan import ExtractionPolicy


@unittest.skipUnless(
    os.getenv("PGHOST") and os.getenv("INGESTION_V2_TEST_DB") == "1",
    "Requires disposable PostgreSQL (INGESTION_V2_TEST_DB=1)",
)
class TestDistributedPdfWork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger=Ledger()
        cls.ledger.create_schema()

    def make_pdf(self, path, content="Original"):
        doc=fitz.open()
        for page_number in range(5):
            page=doc.new_page()
            page.insert_text((45,45), (content+str(page_number)+" ")*30)
        doc.save(str(path))
        doc.close()

    def test_two_hosts_complete_one_document_without_duplicate_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            pdf=Path(temp)/"example.pdf"
            self.make_pdf(pdf)
            output=Path(temp)/"artifacts"
            uri="nas://shared-collection/studies/protocol-001.pdf"
            policy=ExtractionPolicy(range_pages=2,cpu_processes=2,max_attempts=3,
                                    lease_seconds=120)
            registered=register_document(self.ledger,pdf,uri,policy)
            job=registered["job_key"]
            self.assertEqual(registered["range_tasks"],3)
            a=run_worker(
                ledger=self.ledger,job_key=job,source_path=pdf,
                source_uri=uri,output_dir=output,artifact_uri_prefix="nas://results/v2",
                worker_id="linux-A",max_claims=2,
            )
            b=run_worker(
                ledger=self.ledger,job_key=job,source_path=pdf,
                source_uri=uri,output_dir=output,artifact_uri_prefix="nas://results/v2",
                worker_id="windows-B",max_claims=2,
            )
            self.assertEqual(a["SUCCESS"],2)
            self.assertEqual(b["SUCCESS"],1)
            self.assertEqual(self.ledger.summary(job)["SUCCESS"]["ranges"],3)
            self.assertEqual(self.ledger.summary(job)["SUCCESS"]["ocr_candidates"],0)
            data=self.ledger.listed_artifacts(job)
            self.assertEqual(len(data),3)
            self.assertEqual([x[0] for x in data],[0,2,4])
            for start,end,artifact_uri,checksum,ocr in data:
                name=artifact_uri.rsplit("/",1)[-1]
                path=output/job/name
                self.assertTrue(path.exists())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),checksum)
                with gzip.open(path,"rt",encoding="utf-8") as source:
                    payload=json.load(source)
                self.assertEqual(payload["range_start"],start)
                self.assertEqual(payload["range_end"],end)
                self.assertEqual(len(payload["pages"]),end-start)
                self.assertEqual(payload["source_sha256"],registered["source_sha256"])
            self.assertEqual(self.ledger.summary(job)["SUCCESS"]["ranges"],3)
            self.assertIsNone(self.ledger.claim(job,"extra",120,3))

    def test_local_file_changed_is_rejected_before_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            pdf=Path(temp)/"sample.pdf"
            self.make_pdf(pdf)
            uri="nas://shared-collection/test/changed-source.pdf"
            policy=ExtractionPolicy(range_pages=2,cpu_processes=1)
            job=register_document(self.ledger,pdf,uri,policy)["job_key"]
            pdf.unlink()
            self.make_pdf(pdf,"Different")
            with self.assertRaisesRegex(ValueError,"Source version differs"):
                _verify_source(self.ledger,job,pdf,uri)
            self.assertEqual(self.ledger.summary(job)["PENDING"]["ranges"],3)


if __name__=="__main__":
    unittest.main()
