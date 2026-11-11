"""PostgreSQL/pgvector integration tests, enabled only with PGHOST."""
import concurrent.futures
import os
import threading
import uuid
from unittest import TestCase, mock, skipUnless

import psycopg2
import psycopg2.pool
import fast_embedding_runtime as engine


@skipUnless(os.environ.get("PGHOST"), "requires live PGHOST and pgvector")
class TestRealDatabase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.params=dict(host=os.environ["PGHOST"],
                        dbname=os.environ.get("PGDATABASE","embedding_test"),
                        user=os.environ.get("PGUSER","postgres"),
                        password=os.environ.get("PGPASSWORD","test"),
                        port=int(os.environ.get("PGPORT","5432")))
        c=psycopg2.connect(**cls.params)
        c.autocommit=True
        with c.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("""CREATE TABLE IF NOT EXISTS public.embedding_integration_rows(
                id bigint PRIMARY KEY, input_text text, embedding vector(3), note text)""")
        c.close()

    def setUp(self):
        self.job=str(uuid.uuid4())
        self.quota=str(uuid.uuid4())
        engine.POOL=psycopg2.pool.ThreadedConnectionPool(1,12,**self.params)
        engine.initialize(self.job,self.quota,10000,100000,1000)
        engine.txn("DELETE FROM public.embedding_integration_rows")
        for i in range(1,9):
            engine.txn("""INSERT INTO public.embedding_integration_rows(id,input_text)
                        VALUES(%s,%s)""",(i,"bad input" if i==5 else "valid input "+str(i)))
        self.cfg=dict(job=self.job,quota=self.quota,table="public.embedding_integration_rows",
            id='"id"',text='"input_text"',vec='"embedding"',note='"note"',
            note_limit=400,idtype="bigint",reuse=True,batch_size=8,batch_tokens=1000,
            dim=3,attempts=2,lease=90,timeout=30)

    def tearDown(self):
        engine.POOL.closeall()
        engine.POOL=None

    def test_heartbeat_skips_rows_locked_by_batch_commit(self):
        """Critical regression: heartbeat must never wait on a locked task row."""
        engine.discover(self.cfg,threading.Event(),None)
        token,rows=engine.claim(self.cfg,"shared-worker-owner")
        self.assertEqual(len(rows),8)
        locker=psycopg2.connect(**self.params)
        try:
            with locker.cursor() as cur:
                cur.execute("""SELECT row_id FROM embedding_runtime.tasks
                    WHERE job_key=%s AND row_id=%s FOR UPDATE""",
                    (self.job,rows[0]))
                self.assertEqual(cur.fetchone()[0],rows[0])
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                # PostgreSQL statement_timeout protects CI from hanging if
                # the heartbeat accidentally regresses to blocking UPDATE.
                original_db=engine.db
                def heartbeat_db():
                    conn=original_db()
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL statement_timeout='2000ms'")
                    return conn
                with mock.patch.object(engine,"db",side_effect=heartbeat_db):
                    future=executor.submit(engine.renew_leases,self.cfg,
                                           "shared-worker-owner")
                    updated=future.result(timeout=5)
            self.assertEqual(updated,7)
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(engine.renew_leases(self.cfg,"shared-worker-owner"),8)

    def test_discovery_claim_isolation_and_per_row_failures(self):
        engine.discover(self.cfg,threading.Event(),None)
        token,ids=engine.claim(self.cfg,"worker-1")
        self.assertEqual(len(ids),8)
        _, other=engine.claim(self.cfg,"worker-2")
        self.assertEqual(other,[]) # disjoint ownership
        def azure(_cfg,_session,texts):
            if "bad input" in texts:
                raise engine.ApiError("HTTP 400 invalid source input",400)
            return [[1.0,0.0,0.0] for text in texts]
        with mock.patch.object(engine,"call",side_effect=azure):
            engine.process(self.cfg,None,token,ids,threading.Event())
        states,discovery_complete=engine.status(self.cfg)
        self.assertTrue(discovery_complete)
        self.assertEqual(states.get("SUCCESS"),7)
        self.assertEqual(states.get("FAILED"),1)
        self.assertEqual(engine.txn("""SELECT count(*) FROM public.embedding_integration_rows
                                      WHERE embedding IS NOT NULL""",fetch=True)[0][0],7)
        bad=engine.txn("""SELECT error_code,error_message,processing_comment
                         FROM embedding_runtime.tasks WHERE job_key=%s AND row_id='5'""",
                       (self.job,),True)[0]
        self.assertEqual(bad[0],"API_FAILURE")
        self.assertIn("400",bad[1])
        self.assertTrue(bad[2])
        self.assertIn("API_FAILURE",engine.txn(
            "SELECT note FROM public.embedding_integration_rows WHERE id=5",fetch=True)[0][0])
