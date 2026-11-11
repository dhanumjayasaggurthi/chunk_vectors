"""Deterministic fault-isolation and retry tests; no Azure or PostgreSQL needed."""
import importlib
import sys
import threading
import types
from unittest import TestCase, mock

# Avoid importing production config.ini in isolated CI runs.
config=types.ModuleType("config")
config.POSTGRES={}
profile=types.ModuleType("profile_store")
profile._safe_identifier=lambda x:'"'+x+'"'
sys.modules["config"]=config
sys.modules["profile_store"]=profile
runtime=importlib.import_module("fast_embedding_runtime")


class TestEmbeddingRecovery(TestCase):
    def test_one_poison_row_does_not_block_neighbors(self):
        cfg={"attempts":2,"quota":"test"}
        items=[("ok1","hello","digest",2),("bad","bad text","digest",2),
               ("ok2","world","digest",2)]
        persisted,failures=[],[]
        def fake_call(_c,_s,texts):
            if "bad text" in texts:
                raise runtime.ApiError("HTTP 400 invalid input",400)
            return [[1.0,2.0] for _ in texts]
        with mock.patch.object(runtime,"throttle",return_value=True),\
             mock.patch.object(runtime,"call",side_effect=fake_call),\
             mock.patch.object(runtime,"persist",side_effect=lambda c,t,r: persisted.extend(x[0] for x in r)),\
             mock.patch.object(runtime,"txn",return_value=0),\
             mock.patch.object(runtime,"mark",side_effect=lambda c,t,r,*a: failures.append(r)):
            runtime.do_batch(cfg,None,"claim",items,threading.Event())
        self.assertEqual(persisted,["ok1","ok2"])
        self.assertEqual(failures,["bad"])

    def test_429_retries_reserve_quota_again(self):
        cfg={"attempts":3,"quota":"test"}
        reservations=[]
        with mock.patch.object(runtime,"throttle",side_effect=lambda c,n,st: reservations.append(n) or True),\
             mock.patch.object(runtime,"call",side_effect=[
                 runtime.ApiError("rate limited",429,.01),[[1.0]]]),\
             mock.patch.object(runtime,"limit_429") as cooldown,\
             mock.patch.object(runtime,"persist"),\
             mock.patch.object(runtime,"txn"),\
             mock.patch.object(threading.Event,"wait",return_value=False):
            runtime.do_batch(cfg,None,"claim",[("id","text","hash",10)],threading.Event())
        self.assertEqual(reservations,[10,10])
        cooldown.assert_called_once()

    def test_single_invalid_row_records_reason(self):
        cfg={"attempts":3,"quota":"test"}
        errors=[]
        with mock.patch.object(runtime,"throttle",return_value=True),\
             mock.patch.object(runtime,"call",side_effect=runtime.ApiError("invalid text",400)),\
             mock.patch.object(runtime,"mark",side_effect=lambda *a: errors.append(a)):
            runtime.do_batch(cfg,None,"claim",[("bad","text","hash",1)],threading.Event())
        self.assertEqual(errors[0][2],"bad")
        self.assertEqual(errors[0][3],"FAILED")
        self.assertEqual(errors[0][4],"API_FAILURE")

    def test_database_deadlock_retries_only_vector_persistence(self):
        # A transaction rollback must not cause another Azure call.
        class MockDeadlock(runtime.psycopg2.DatabaseError):
            pgcode = "40P01"
        rows=[("row-1","md5",[1.0,2.0])]
        with mock.patch.object(runtime,"commit_vectors",side_effect=[
                MockDeadlock("simulated deadlock"), {"row-1"}]) as commit,\
             mock.patch.object(runtime,"mark") as mark,\
             mock.patch.object(runtime.time,"sleep") as sleep,\
             mock.patch.object(runtime.random,"random",return_value=0.0):
            runtime.persist({"job":"test"},"claim",rows)
        self.assertEqual(commit.call_count,2)
        self.assertEqual(sleep.call_count,1)
        mark.assert_not_called()

    def test_persistent_database_deadlock_is_not_marked_as_bad_source(self):
        # On persistent systemic DB contention the caller stops safely;
        # the claim is recovered on restart rather than marked FAILED.
        class MockDeadlock(runtime.psycopg2.DatabaseError):
            pgcode = "40P01"
        with mock.patch.object(runtime,"commit_vectors",
                               side_effect=MockDeadlock("simulated deadlock")) as commit,\
             mock.patch.object(runtime,"mark") as mark,\
             mock.patch.object(runtime.time,"sleep"),\
             mock.patch.object(runtime.random,"random",return_value=0.0):
            with self.assertRaises(MockDeadlock):
                runtime.persist({"job":"test"},"claim",
                                [("row-1","md5",[1.0,2.0])])
        self.assertEqual(commit.call_count,runtime._DB_COMMIT_ATTEMPTS)
        mark.assert_not_called()

    def test_fatal_auth_failure_stops_work_instead_of_poisoning_all_rows(self):
        cfg={"attempts":3,"quota":"test"}
        with mock.patch.object(runtime,"throttle",return_value=True),\
             mock.patch.object(runtime,"call",side_effect=runtime.FatalApiError("bad credentials")),\
             mock.patch.object(runtime,"mark") as mark:
            with self.assertRaises(runtime.FatalApiError):
                runtime.do_batch(cfg,None,"claim",[("id","text","hash",1)],threading.Event())
            mark.assert_not_called()
