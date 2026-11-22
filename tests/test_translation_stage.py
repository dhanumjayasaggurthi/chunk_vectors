"""Deterministic translation lease, retry, idempotency and parallelism tests."""
from __future__ import annotations

import hashlib
import os
import threading
import unittest

from translation_stage import (
    claim_batch, compose_language_input, create_translation_table,
    enqueue_segments, extend_lease, finish_segment, translate_claimed,
    translation_fingerprint,
)

SEMANTIC=translation_fingerprint(provider="test",model="v2")


class TestTranslationUnit(unittest.TestCase):
    def test_parallel_translator_preserves_native_and_source_identity(self):
        rows=[
            (f"seg-{i}",hashlib.sha256(str(i).encode()).hexdigest(),
             SEMANTIC,"fr",f"Texte réglementaire {i}",1)
            for i in range(32)
        ]
        lock=threading.Lock()
        active=0
        peak=0
        def translator(native, source_language, *, target_language):
            nonlocal active,peak
            self.assertEqual(source_language,"fr")
            self.assertEqual(target_language,"en")
            with lock:
                active+=1
                peak=max(peak,active)
            threading.Event().wait(.01)
            with lock:
                active-=1
            return "English: "+native
        results=translate_claimed(rows,translator,workers=8)
        self.assertEqual(len(results),32)
        self.assertGreater(peak,1)
        self.assertTrue(all(result[1].startswith("English: ")
                            and result[2] is None for result in results))
        self.assertTrue(all(row[4].startswith("Texte") for row in rows))

    def test_invalid_language_refuses_translation(self):
        with self.assertRaises(ValueError):
            compose_language_input("texte","english",language="bad code")


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1" and
    os.getenv("PGDATABASE")=="embedding_test",
    "Use explicitly authorized disposable PostgreSQL only",
)
class TestTranslationPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.conn=psycopg2.connect(
            host=os.environ["PGHOST"],port=os.getenv("PGPORT","5432"),
            dbname=os.environ["PGDATABASE"],user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"])
        with cls.conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
        cls.conn.commit()
        create_translation_table(cls.conn,schema="intake_ci")

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS intake_ci.rich_translation")
            cls.conn.commit()
        finally:
            cls.conn.close()

    def setUp(self):
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE intake_ci.rich_translation")
        self.conn.commit()

    def test_batch_enqueue_idempotency_and_language_version(self):
        segments=((f"seg-{i}",f"Texte français {i}","fr")
                  for i in range(300))
        self.assertEqual(enqueue_segments(
            self.conn,segments,translation_hash=SEMANTIC,
            schema="intake_ci",batch_size=64),300)
        self.assertEqual(enqueue_segments(
            self.conn,((f"seg-{i}",f"Texte français {i}","fr")
                        for i in range(300)),
            translation_hash=SEMANTIC,schema="intake_ci"),0)
        self.assertEqual(enqueue_segments(
            self.conn,[("seg-0","Texte français 0","en")],
            translation_hash=SEMANTIC,schema="intake_ci"),1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM intake_ci.rich_translation")
            self.assertEqual(cur.fetchone()[0],301)

        # A later malformed segment must roll back earlier buffered inserts.
        with self.assertRaises(ValueError):
            enqueue_segments(
                self.conn,[("new-valid","Bonjour","fr"),
                           ("new-invalid","","fr")],
                translation_hash=SEMANTIC,schema="intake_ci",
                batch_size=1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM intake_ci.rich_translation")
            self.assertEqual(cur.fetchone()[0],301)

    def test_parallel_claim_leases_and_stale_owners(self):
        enqueue_segments(
            self.conn,[(f"block-{i}",f"Texte {i}","fr")
                       for i in range(6)],
            translation_hash=SEMANTIC,schema="intake_ci")
        first=claim_batch(self.conn,owner="worker-A",batch_size=3,
                          schema="intake_ci")
        second=claim_batch(self.conn,owner="worker-B",batch_size=3,
                           schema="intake_ci")
        self.assertEqual(len(first),3)
        self.assertEqual(len(second),3)
        self.assertFalse({x[0] for x in first}&{x[0] for x in second})
        self.assertFalse(finish_segment(
            self.conn,first[0],owner="wrong",english="English",
            schema="intake_ci"))
        self.assertTrue(extend_lease(
            self.conn,first[0],owner="worker-A",schema="intake_ci"))
        self.assertTrue(finish_segment(
            self.conn,first[0],owner="worker-A",english="English",
            schema="intake_ci"))
        self.assertFalse(extend_lease(
            self.conn,first[0],owner="worker-A",schema="intake_ci"))
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE intake_ci.rich_translation
                SET lease_until=now()-interval '1 second'
                WHERE segment_id=%s""",(first[1][0],))
        self.conn.commit()
        restored=claim_batch(
            self.conn,owner="worker-C",batch_size=1,schema="intake_ci")
        self.assertEqual(restored[0][0],first[1][0])
        self.assertFalse(finish_segment(
            self.conn,first[1],owner="worker-A",english="stale",
            schema="intake_ci"))

    def test_retry_backoff_is_durable_and_retryable(self):
        enqueue_segments(
            self.conn,[("retry-segment","Bonjour","fr")],
            translation_hash=SEMANTIC,schema="intake_ci")
        row=claim_batch(self.conn,owner="a",schema="intake_ci")[0]
        self.assertTrue(finish_segment(
            self.conn,row,owner="a",english=None,error="429",
            schema="intake_ci"))
        self.assertEqual(claim_batch(
            self.conn,owner="b",schema="intake_ci"),[])
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE intake_ci.rich_translation
                SET next_retry_at=now()-interval '1 second'
                WHERE segment_id='retry-segment'""")
        self.conn.commit()
        retry=claim_batch(self.conn,owner="b",schema="intake_ci")
        self.assertEqual(len(retry),1)
        self.assertEqual(retry[0][5],2)
        self.assertTrue(finish_segment(
            self.conn,retry[0],owner="b",english="Hello",
            schema="intake_ci"))
        with self.conn.cursor() as cur:
            cur.execute("""SELECT translated_text,state,attempts
                FROM intake_ci.rich_translation WHERE segment_id='retry-segment'""")
            self.assertEqual(cur.fetchone(),("Hello","complete",2))


if __name__=="__main__":
    unittest.main()
