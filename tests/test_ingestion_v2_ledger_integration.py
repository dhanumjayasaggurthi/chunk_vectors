"""PostgreSQL ledger integration tests against a disposable database.

Never runs against production implicitly. Requires PGHOST and explicit
INGESTION_V2_TEST_DB=1; CI uses a temporary PostgreSQL service.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import unittest
from dataclasses import asdict

from ingestion_v2.ledger import Claim, Ledger, connect_database
from ingestion_v2.plan import ExtractionPolicy


@unittest.skipUnless(
    os.getenv("PGHOST") and os.getenv("INGESTION_V2_TEST_DB") == "1",
    "Requires disposable PGHOST and INGESTION_V2_TEST_DB=1",
)
class TestDistributedLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger()
        cls.ledger.create_schema()

    def setUp(self):
        self.key = hashlib.sha256(os.urandom(32)).hexdigest()
        self.document = hashlib.sha256(os.urandom(32)).hexdigest()
        self.policy = asdict(ExtractionPolicy(range_pages=2))
        self.ledger.register(
            job_key=self.key, document_id=self.document,
            source_uri="nas://pilot/test.pdf",
            source_sha256=hashlib.sha256(b"pdf").hexdigest(),
            source_size=1024, page_count=5, range_pages=2,
            policy=self.policy,
        )

    def test_register_is_idempotent_and_source_mismatch_rejected(self):
        registered = self.ledger.register(
            job_key=self.key, document_id=self.document,
            source_uri="nas://pilot/test.pdf",
            source_sha256=hashlib.sha256(b"pdf").hexdigest(),
            source_size=1024, page_count=5, range_pages=2,
            policy=self.policy,
        )
        self.assertEqual(registered, 3)
        with self.assertRaisesRegex(ValueError,"immutable source"):
            self.ledger.register(
                job_key=self.key, document_id=self.document,
                source_uri="nas://another/path.pdf",
                source_sha256=hashlib.sha256(b"pdf").hexdigest(),
                source_size=1024, page_count=5, range_pages=2,
                policy=self.policy,
            )

    def test_two_servers_claim_distinct_ranges_concurrently(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            claims=list(pool.map(
                lambda i: self.ledger.claim(self.key, f"host-{i}",120,4),
                range(4),
            ))
        got=[c for c in claims if c is not None]
        self.assertEqual(len(got),3)
        self.assertEqual(len({c.start_page for c in got}),3)
        self.assertEqual(sorted(c.start_page for c in got),[0,2,4])

    def test_fenced_expiration_rejects_old_worker(self):
        old=self.ledger.claim(self.key,"linux-worker",120,4)
        self.assertIsNotNone(old)
        conn=connect_database()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE ingestion_v2_runtime.ranges
                           SET lease_until=clock_timestamp()-interval '1 minute'
                           WHERE job_key=%s AND start_page=%s""",
                        (self.key,old.start_page),
                    )
        finally:
            conn.close()
        newer=self.ledger.claim(self.key,"windows-worker",120,4)
        self.assertEqual(newer.start_page,old.start_page)
        self.assertNotEqual(newer.token,old.token)
        artifact_hash=hashlib.sha256(b"artifact").hexdigest()
        self.assertFalse(self.ledger.renew(old,120))
        self.assertFalse(self.ledger.complete(old,"s3://artifact/old",artifact_hash,0))
        self.assertIsNone(self.ledger.fail(
            old,"STALE","Must never overwrite",max_attempts=4
        ))
        self.assertTrue(self.ledger.complete(
            newer,"s3://artifact/new",artifact_hash,0
        ))
        self.assertFalse(self.ledger.complete(newer,"s3://artifact/new",artifact_hash,0))
        self.assertEqual(self.ledger.summary(self.key)["SUCCESS"]["ranges"],1)

    def test_retry_and_individual_failure_reason(self):
        claim=self.ledger.claim(self.key,"worker-A",120,2)
        self.assertEqual(self.ledger.fail(
            claim,"PDF_READ","Transient source read failure",
            max_attempts=2,retry_delay_seconds=0
        ),"PENDING")
        retry=self.ledger.claim(self.key,"worker-A",120,2)
        self.assertEqual(retry.start_page,claim.start_page)
        self.assertEqual(retry.attempts,2)
        self.assertEqual(self.ledger.fail(
            retry,"PDF_READ","Repeated read failure",
            max_attempts=2,retry_delay_seconds=0
        ),"FAILED")
        self.assertEqual(self.ledger.summary(self.key)["FAILED"]["ranges"],1)
        conn=connect_database()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT error_code FROM ingestion_v2_runtime.range_failures
                           WHERE job_key=%s AND start_page=%s
                           ORDER BY id""",
                        (self.key,claim.start_page),
                    )
                    self.assertEqual([r[0] for r in cur.fetchall()],
                                     ["PDF_READ","PDF_READ"])
        finally:
            conn.close()
        # Other unprocessed ranges remain claimable; only this failed range is terminal.
        next_claim=self.ledger.claim(self.key,"worker",120,1)
        self.assertIsNotNone(next_claim)
        self.assertNotEqual(next_claim.start_page,claim.start_page)

    def test_expired_last_attempt_becomes_failed_and_auditable(self):
        first=self.ledger.claim(self.key,"worker",120,1)
        conn=connect_database()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE ingestion_v2_runtime.ranges
                           SET lease_until=clock_timestamp()-interval '1 second'
                           WHERE job_key=%s AND start_page=%s""",
                        (self.key,first.start_page),
                    )
        finally:
            conn.close()
        self.assertEqual(self.ledger.expire_exhausted(self.key,1),1)
        self.assertEqual(self.ledger.expire_exhausted(self.key,1),0)
        self.assertEqual(self.ledger.summary(self.key)["FAILED"]["ranges"],1)

    def test_expired_running_recovered_then_other_ranges_finish(self):
        claim=self.ledger.claim(self.key,"crashed-host",120,4)
        conn=connect_database()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE ingestion_v2_runtime.ranges
                           SET lease_until=clock_timestamp()-interval '2 seconds'
                           WHERE job_key=%s AND start_page=%s""",
                        (self.key,claim.start_page),
                    )
        finally:
            conn.close()
        found=[]
        for _ in range(3):
            newer=self.ledger.claim(self.key,"healthy-host",120,4)
            self.assertIsNotNone(newer)
            found.append(newer)
            self.assertTrue(self.ledger.complete(
                newer,"nas://out/"+str(newer.start_page),
                hashlib.sha256(str(newer.start_page).encode()).hexdigest(),0
            ))
        self.assertEqual(sorted(x.start_page for x in found),[0,2,4])
        self.assertEqual(self.ledger.summary(self.key)["SUCCESS"]["ranges"],3)
        self.assertIsNone(self.ledger.claim(self.key,"healthy-host",120,4))


if __name__=="__main__":
    unittest.main()
