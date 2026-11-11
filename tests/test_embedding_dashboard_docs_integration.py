"""Validate document rollups against PostgreSQL with source text + pgvector.

Skipped unless PGHOST is configured. CI supplies a disposable pgvector PG16 DB.
"""
import os
import unittest
from unittest.mock import patch

import psycopg2

import embedding_dashboard_docs as dashboard


@unittest.skipUnless(os.environ.get("PGHOST"), "PGHOST required for DB integration")
class DocumentRollupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.options={
            "host":os.environ["PGHOST"],
            "port":int(os.environ.get("PGPORT","5432")),
            "user":os.environ.get("PGUSER","postgres"),
            "password":os.environ.get("PGPASSWORD","test"),
            "dbname":os.environ.get("PGDATABASE","embedding_test"),
        }
        cls.dataset={
            "schema":"public","table":"embedding_dashboard_doc_test",
            "id_column":"chunk_id","vector_column":"chunk_vector",
        }
        conn=psycopg2.connect(**cls.options)
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("DROP TABLE IF EXISTS public.embedding_dashboard_doc_test")
                cur.execute("""
                    CREATE TABLE public.embedding_dashboard_doc_test (
                        chunk_id text PRIMARY KEY,
                        doc_id text,
                        file_name text,
                        chunk_index int,
                        chunk_total int,
                        chunk_vector vector(3),
                        embedding_processing_note text,
                        updated_at timestamptz DEFAULT now()
                    )
                """)
                cur.execute("CREATE INDEX ON public.embedding_dashboard_doc_test(doc_id)")
                cur.execute("""
                    INSERT INTO public.embedding_dashboard_doc_test
                    (chunk_id,doc_id,file_name,chunk_index,chunk_total,
                     chunk_vector,embedding_processing_note) VALUES
                    ('a1','A','a.pdf',0,2,'[1,0,0]',NULL),
                    ('a2','A','a.pdf',1,2,'[0,1,0]',NULL),
                    ('b1','B','b.pdf',0,2,'[0,0,1]',NULL),
                    ('b2','B','b.pdf',1,2,NULL,'INPUT_TOO_LONG'),
                    ('c1','C','c.pdf',0,2,NULL,NULL),
                    ('c2','C','c.pdf',1,2,NULL,NULL),
                    ('o1',NULL,'no-doc.pdf',0,1,NULL,NULL)
                """)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn=psycopg2.connect(**cls.options)
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS public.embedding_dashboard_doc_test")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def connection(cls):
        return psycopg2.connect(
            **cls.options,
            options="-c default_transaction_read_only=on -c statement_timeout=12000",
        )

    def test_full_collection_rollup(self):
        with patch.object(dashboard,"connect_readonly",side_effect=self.connection):
            data=dashboard.fetch_document_rollup(self.dataset)
        self.assertEqual(data["total_documents"],3)
        self.assertEqual(data["completed_documents"],1)
        self.assertEqual(data["partial_documents"],1)
        self.assertEqual(data["untouched_documents"],1)
        self.assertEqual(data["attention_documents"],1)
        self.assertEqual(data["total_chunks"],7)
        self.assertEqual(data["embedded_chunks"],3)
        self.assertEqual(data["missing_chunks"],4)
        self.assertEqual(data["unmapped_chunks"],1)
        self.assertEqual(data["priority_documents"][0]["document_id"],"C")

    def test_indexed_one_document_inspection(self):
        with patch.object(dashboard,"connect_readonly",side_effect=self.connection):
            result=dashboard.fetch_document_details(self.dataset,"B")
        self.assertEqual(result["summary"]["total_chunks"],2)
        self.assertEqual(result["summary"]["embedded_chunks"],1)
        self.assertEqual(result["summary"]["noted_chunks"],1)
        self.assertEqual([row["embedded"] for row in result["rows"]],[True,False])
        self.assertFalse(result["truncated"])
