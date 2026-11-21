"""Tests for main-based ingestion reuse; no calls to Vision or Azure."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from processing_profile import load_profile
from reuse_materialize import compose_embedding_text, identifiers


REPO=Path(__file__).resolve().parents[1]
SAMPLE=REPO/"profiles"/"intake_reuse_immbot_example.yml"


class TestConsumerIntake(unittest.TestCase):
    def test_yaml_input_fields_and_existing_source_mapping(self):
        profile=load_profile(str(SAMPLE))
        self.assertEqual(profile["chunking"]["max_tokens"],800)
        self.assertEqual(profile["chunking"]["min_tokens"],100)
        self.assertEqual(profile["chunking"]["overlap_tokens"],80)
        self.assertIn("document.protocol_number",profile["embedding"]["input_fields"])
        self.assertEqual(profile["extracted_content"]["position_column"],"chunk_index")
        self.assertNotEqual(identifiers(profile)["source"],identifiers(profile)["table"])

    def test_input_composition_is_ordered_and_preserves_original(self):
        chunk=SimpleNamespace(chunk_text="Results were clinically relevant.",
                              section_title="3. Results",file_name="study.pdf",
                              file_path="nas://study.pdf",
                              document_metadata={"protocol_number":"ABC-123"},
                              chunk_metadata={"classification":"sensitive"})
        original=chunk.chunk_text
        result=compose_embedding_text(
            chunk,["document.protocol_number","source.product",
                   "section_title","chunk_text"],
            {"product":"Compound X"},max_tokens=1000)
        self.assertIn("ABC-123",result)
        self.assertIn("Compound X",result)
        self.assertIn("3. Results",result)
        self.assertTrue(result.endswith(original))
        self.assertEqual(chunk.chunk_text,original)

    def test_input_does_not_fabricate_missing_metadata_or_truncate(self):
        chunk=SimpleNamespace(chunk_text="Clinical text",section_title="",
                              file_name="sample.pdf",file_path="x",
                              document_metadata={},chunk_metadata={})
        result=compose_embedding_text(
            chunk,["document.protocol_number","chunk_text"],{},max_tokens=100)
        self.assertEqual(result,"Clinical text")
        with self.assertRaisesRegex(ValueError,"exceeds"):
            compose_embedding_text(chunk,["chunk_text"],{},max_tokens=1)

    def test_reject_same_source_output(self):
        profile=load_profile(str(SAMPLE))
        profile["output"]["schema"]=profile["extracted_content"]["schema"]
        profile["output"]["table"]=profile["extracted_content"]["table"]
        with self.assertRaisesRegex(ValueError,"different tables"):
            identifiers(profile)

    def test_duplicate_yaml_keys_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"bad.yml"
            path.write_text("version: 1\nversion: 1\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"duplicate"):
                load_profile(str(path))


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1" and os.getenv("PGDATABASE")=="embedding_test",
    "Requires explicitly authorized disposable embedding_test PostgreSQL",
)
class TestPostgresRechunkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.connect=lambda: psycopg2.connect(
            host=os.environ["PGHOST"],port=os.environ.get("PGPORT","5432"),
            dbname=os.environ["PGDATABASE"],user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"])
        conn=cls.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("CREATE SCHEMA IF NOT EXISTS intake_ci")
                for table in ("out_chunks_v1_failures","out_chunks_v1_status",
                              "out_chunks_v1","source_chunks"):
                    cur.execute('DROP TABLE IF EXISTS intake_ci."' + table + '" CASCADE')
                cur.execute("""
                    CREATE TABLE intake_ci.source_chunks(
                      doc_id text NOT NULL, chunk_index integer NOT NULL,
                      chunk_text text NOT NULL, page_start integer,
                      page_end integer, section_title text, file_path text,
                      file_name text,
                      PRIMARY KEY(doc_id,chunk_index))
                """)
                text="Protocol Number: ABC-123\n1 STUDY DESIGN\n" + "Study information. "*80
                cur.execute("""
                    INSERT INTO intake_ci.source_chunks VALUES
                    ('doc_ok',0,%s,1,1,'Study Design','nas://doc_ok.pdf','doc_ok.pdf'),
                    ('doc_ok',1,%s,2,3,'Study Design','nas://doc_ok.pdf','doc_ok.pdf'),
                    ('doc_bad',0,%s,1,1,'Overview','nas://doc_bad.pdf','first.pdf'),
                    ('doc_bad',1,%s,2,2,'Overview','nas://doc_bad.pdf','other.pdf')
                """,(text,"Continuation of study information. "*90,
                     text,"Additional information. "*90))
        conn.close()
        p=load_profile(str(SAMPLE))
        p["extracted_content"]["schema"]="intake_ci"
        p["extracted_content"]["table"]="source_chunks"
        p["output"]["schema"]="intake_ci"
        p["output"]["table"]="out_chunks_v1"
        # Recompute stable profile fingerprint using the identical
        # canonicalization used by processing_profile.load_profile.
        p.pop("_fingerprint")
        p["_fingerprint"]=hashlib.sha256(
            json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=False
                      ).encode("utf-8")
        ).hexdigest()
        cls.profile=p

    @classmethod
    def tearDownClass(cls):
        conn=cls.connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    for name in ("out_chunks_v1_failures","out_chunks_v1_status",
                                 "out_chunks_v1","source_chunks"):
                        cur.execute('DROP TABLE IF EXISTS intake_ci."'+name+'" CASCADE')
            conn.close()
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def test_atomic_skip_failure_recovery_and_embedding_readiness(self):
        from reuse_materialize import init_output, run
        p=self.profile
        self.assertEqual(init_output(p,apply=False)["status"],"NO_WRITES")
        init_output(p,apply=True)
        result=run(p,max_docs=10,workers=2)
        self.assertEqual(result["SUCCESS"],1)
        self.assertEqual(result["FAILED"],1)
        conn=self.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*),bool_and(chunk_vector IS NULL)
                    FROM intake_ci.out_chunks_v1""")
                count, no_vectors=cur.fetchone()
                self.assertGreater(count,0)
                self.assertTrue(no_vectors)
                cur.execute("""SELECT chunk_text,source_chunk_text,document_metadata,
                    metadata_evidence,page_end FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_ok' ORDER BY chunk_index LIMIT 1""")
                composed,original,meta,evidence,page_end=cur.fetchone()
                self.assertIn("ABC-123",composed)
                self.assertNotIn("document / protocol",original)
                self.assertEqual(meta["protocol_number"],"ABC-123")
                self.assertEqual(evidence["protocol_number"]["page"],1)
                cur.execute("""SELECT COUNT(*) FROM intake_ci.out_chunks_v1_failures
                    WHERE doc_id='doc_bad'""")
                self.assertEqual(cur.fetchone()[0],1)
                # Repair the one poisoned document without touching successful rows.
                cur.execute("""UPDATE intake_ci.source_chunks
                    SET file_name='first.pdf' WHERE doc_id='doc_bad'""")
        conn.close()
        result2=run(p,max_docs=10,workers=2)
        self.assertEqual(result2["SUCCESS"],1)
        self.assertEqual(result2["SKIPPED"],1)
        self.assertEqual(result2["FAILED"],0)
        conn=self.connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*) FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_bad'""")
                self.assertGreater(cur.fetchone()[0],0)
                cur.execute("""SELECT max(page_end) FROM intake_ci.out_chunks_v1
                    WHERE doc_id='doc_ok'""")
                self.assertEqual(cur.fetchone()[0],3)
        conn.close()


if __name__=="__main__":
    unittest.main()
