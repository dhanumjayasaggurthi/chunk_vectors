"""Ephemeral PostgreSQL/pgvector integration tests; no cloud service credentials."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psycopg2

from db import get_conn
from processing_profile import load_profile
import profile_store
import existing_embeddings
import extracted_source


@unittest.skipUnless(os.environ.get("CI_DB_TEST") == "1", "Postgres integration DB unavailable")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("CREATE SCHEMA IF NOT EXISTS epod_ci")
            conn.commit()
        finally:
            conn.close()
        profile_store.init_profile_store()

    def test_profile_atomic_replacement_preserves_old_on_validation_error(self):
        base = dict(
            doc_id="integration-doc", file_name="fixture.pdf",
            file_path="s3://fixture.pdf", page_start=1, page_end=2,
            section_title="1.0 Trial", chunk_vector=[],
        )

        def fake(index, vector=None):
            return SimpleNamespace(
                **{**base,
                   "chunk_id": "chunk-" + str(index),
                   "chunk_index": index,
                   "chunk_text": "Test data #" + str(index),
                   "chunk_vector": vector or [],
                   "headers": ["1.0 Trial"],
                   "document_metadata": {"protocol": "MOMM281004"},
                   "chunk_metadata": {},
                   }
            )
        profile_store.save_profile_result(
            "integration-doc", "source-1", "profile-A", [fake(0), fake(1)]
        )
        self.assertTrue(profile_store.profile_is_current(
            "integration-doc", "source-1", "profile-A"
        ))
        profile_store.save_profile_result(
            "integration-doc", "source-2", "profile-A", [fake(0)]
        )
        self.assertFalse(profile_store.profile_is_current(
            "integration-doc", "source-1", "profile-A"
        ))
        with self.assertRaises(ValueError):
            profile_store.save_profile_result(
                "integration-doc", "bad", "profile-A", [fake(0, vector=[1.0] * 5)]
            )
        self.assertTrue(profile_store.profile_is_current(
            "integration-doc", "source-2", "profile-A"
        ))
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM epod_ci.profile_chunks_test "
                    "WHERE doc_id=%s AND profile_hash=%s",
                    ("integration-doc", "profile-A")
                )
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_existing_embeddings_only_updates_missing(self):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS epod_ci.integration_chunks")
                cur.execute(
                    "CREATE TABLE epod_ci.integration_chunks "
                    "(id BIGINT PRIMARY KEY, content TEXT, embedding vector(1536))"
                )
                cur.execute(
                    "INSERT INTO epod_ci.integration_chunks (id, content, embedding) "
                    "VALUES (1,'first',NULL),(2,'second',NULL)"
                )
            conn.commit()
        finally:
            conn.close()
        profile = load_profile(None, [
            "existing_chunks.schema=epod_ci",
            "existing_chunks.table=integration_chunks",
            "existing_chunks.id_column=id",
            "existing_chunks.text_column=content",
            "existing_chunks.vector_column=embedding",
        ])
        with patch.object(
            existing_embeddings, "_embed_batch",
            side_effect=lambda texts: [[0.05] * 1536 for _ in texts]
        ):
            result = existing_embeddings.embed_existing(profile)
            second = existing_embeddings.embed_existing(profile)
        self.assertEqual(result["embedded"], 2)
        self.assertEqual(second["embedded"], 0)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM epod_ci.integration_chunks WHERE embedding IS NOT NULL")
                self.assertEqual(cur.fetchone()[0], 2)
        finally:
            conn.close()

    def test_existing_extraction_page_mapping(self):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS epod_ci.integration_extracts")
                cur.execute(
                    "CREATE TABLE epod_ci.integration_extracts "
                    "(doc_key TEXT NOT NULL, pg INTEGER NOT NULL, extracted TEXT, PRIMARY KEY(doc_key,pg))"
                )
                cur.execute(
                    "INSERT INTO epod_ci.integration_extracts VALUES "
                    "('doc-1',1,'Protocol Number: ABC-1'),"
                    "('doc-1',2,'1.1 Clinical Study'),"
                    "('doc-2',1,'Another document')"
                )
            conn.commit()
        finally:
            conn.close()
        profile = load_profile(None, [
            "extracted_content.schema=epod_ci",
            "extracted_content.table=integration_extracts",
            "extracted_content.doc_id_column=doc_key",
            "extracted_content.text_column=extracted",
            "extracted_content.page_column=pg",
        ])
        keys = list(extracted_source.iter_document_ids(profile, max_docs=5))
        self.assertEqual(keys, ["doc-1", "doc-2"])
        obj, digest = extracted_source.load_extracted_document(profile, "doc-1")
        self.assertEqual(obj.page_count, 2)
        self.assertEqual(len(obj.pages), 2)
        self.assertEqual(obj.pages[0].raw_text, "Protocol Number: ABC-1")
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
