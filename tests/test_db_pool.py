"""Connection reuse and session-state isolation, no external services."""
from __future__ import annotations

import concurrent.futures
import os
import unittest
from unittest.mock import patch


class FakeRaw:
    def __init__(self, pid):
        self.pid=pid
        self.closed=0
        self.reset_count=0
        self.rollback_count=0
        self.readonly=False

    def rollback(self):
        self.rollback_count += 1

    def reset(self):
        self.readonly=False
        self.reset_count += 1

    def set_session(self, *, readonly=False):
        self.readonly=readonly

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.rollback()

    def close(self):
        self.closed=1


class FakeThreadedPool:
    def __init__(self, min_conn, max_conn, **kwargs):
        self.max_conn=max_conn
        self.all=[]
        self.idle=[]
        self.busy=set()
        self.closed=False
        self.kwargs=kwargs

    def getconn(self):
        if self.idle:
            conn=self.idle.pop()
        elif len(self.all)<self.max_conn:
            conn=FakeRaw(len(self.all)+1)
            self.all.append(conn)
        else:
            raise RuntimeError("Pool slots exhausted")
        self.busy.add(conn)
        return conn

    def putconn(self, raw, close=False):
        self.busy.discard(raw)
        if close:
            raw.close()
            self.all.remove(raw)
        else:
            self.idle.append(raw)

    def closeall(self):
        self.closed=True
        for conn in self.all:
            conn.close()


class TestConnectionPool(unittest.TestCase):
    def config(self):
        return dict(host="localhost",port=5432,database="local",
                    user="fake",password="none",
                    connect_timeout_seconds=10,
                    pool_min_connections=1,
                    pool_max_connections=2,
                    pool_acquire_timeout_seconds=1)

    def test_reuse_returns_on_close_and_resets_readonly(self):
        from db_pool import ConnectionPoolManager
        with patch("db_pool.ThreadedConnectionPool",FakeThreadedPool):
            manager=ConnectionPoolManager(self.config())
            first=manager.acquire()
            pid=first.pid
            first.set_session(readonly=True)
            self.assertTrue(first.readonly)
            first.close()
            first.close()  # Double close must not release someone else's slot.
            with self.assertRaisesRegex(RuntimeError,"already returned"):
                first.set_session(readonly=True)
            second=manager.acquire()
            self.assertEqual(second.pid,pid)
            self.assertFalse(second.readonly)
            second.close()
            self.assertEqual(manager._pool.all[0].reset_count,2)

    def test_exhaustion_wait_timeout_and_no_global_pool_fork(self):
        from db_pool import ConnectionPoolManager
        with patch("db_pool.ThreadedConnectionPool",FakeThreadedPool):
            manager=ConnectionPoolManager(self.config())
            a=manager.acquire()
            b=manager.acquire()
            self.assertNotEqual(a.pid,b.pid)
            with self.assertRaisesRegex(TimeoutError,"pool exhausted"):
                manager.acquire()
            a.close()
            c=manager.acquire()
            self.assertEqual(c.pid,1)
            c.close()
            b.close()
            manager.pid=-1
            with self.assertRaisesRegex(RuntimeError,"must not cross fork"):
                manager.acquire()


@unittest.skipUnless(os.getenv("INGESTION_REUSE_TEST_DB")=="1"
                     and os.getenv("PGDATABASE")=="embedding_test",
                     "Only a disposable CI PostgreSQL database")
class TestRealPooledPostgres(unittest.TestCase):
    def test_reused_backend_is_clean_and_parallel_pool_is_bounded(self):
        from db_pool import ConnectionPoolManager
        from config import POSTGRES
        settings={
            **POSTGRES,"pool_min_connections":1,"pool_max_connections":2,
            "pool_acquire_timeout_seconds":2,"connect_timeout_seconds":10,
        }
        pool=ConnectionPoolManager(settings)
        try:
            conn=pool.acquire()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    pid=cur.fetchone()[0]
                conn.rollback()
                conn.set_session(readonly=True,isolation_level="REPEATABLE READ")
                with conn.cursor() as cur:
                    cur.execute("SELECT current_setting('transaction_read_only')")
                    self.assertEqual(cur.fetchone()[0],"on")
            finally:
                conn.close()
            conn=pool.acquire()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid(), current_setting('transaction_read_only')")
                    fresh_pid,readonly=cur.fetchone()
                self.assertEqual(fresh_pid,pid)
                self.assertEqual(readonly,"off")
            finally:
                conn.close()
            a=pool.acquire()
            b=pool.acquire()
            try:
                with a.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    aid=cur.fetchone()[0]
                with b.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    bid=cur.fetchone()[0]
                self.assertNotEqual(aid,bid)
            finally:
                a.close()
                b.close()
        finally:
            pool.closeall()


if __name__=="__main__":
    unittest.main()
