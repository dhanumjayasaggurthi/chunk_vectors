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

    def test_existing_embeddings_3072_in_real_pgvector(self):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS epod_ci.integration_haq_query")
                cur.execute(
                    "CREATE TABLE epod_ci.integration_haq_query "
                    "(id BIGINT PRIMARY KEY, question TEXT, question_embedding vector(3072))"
                )
                cur.execute(
                    "INSERT INTO epod_ci.integration_haq_query VALUES "
                    "(11, 'What is the indication?', NULL), "
                    "(12, 'What is the outcome?', NULL)"
                )
            conn.commit()
        finally:
            conn.close()
        profile = load_profile(None, [
            "existing_chunks.schema=epod_ci",
            "existing_chunks.table=integration_haq_query",
            "existing_chunks.id_column=id",
            "existing_chunks.text_column=question",
            "existing_chunks.vector_column=question_embedding",
            "embedding.dimensions=3072",
            "embedding.model=text-embedding-3-large",
        ])
        with patch.object(
            existing_embeddings, "_embed_batch",
            side_effect=lambda texts, **kwargs: [[0.05] * kwargs["dimensions"] for _ in texts],
        ):
            first = existing_embeddings.embed_existing(profile)
            second = existing_embeddings.embed_existing(profile)
        self.assertEqual(first["embedded"], 2)
        self.assertEqual(second["embedded"], 0)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT vector_dims(question_embedding) FROM "
                    "epod_ci.integration_haq_query ORDER BY id"
                )
                self.assertEqual(cur.fetchall(), [(3072,), (3072,)])
        finally:
            conn.close()

    def test_optional_embedding_notes_and_retries(self):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS epod_ci.note_test")
                cur.execute(
                    "CREATE TABLE epod_ci.note_test "
                    "(id BIGINT PRIMARY KEY, question TEXT, embedding vector(3072), processing_note TEXT)"
                )
                cur.execute(
                    "INSERT INTO epod_ci.note_test VALUES "
                    "(1, 'Valid question', NULL, 'previous note'),"
                    "(2, '', NULL, NULL),"
                    "(3, 'Retry question', NULL, NULL)"
                )
            conn.commit()
        finally:
            conn.close()
        profile = load_profile(None, [
            "existing_chunks.schema=epod_ci",
            "existing_chunks.table=note_test",
            "existing_chunks.id_column=id",
            "existing_chunks.text_column=question",
            "existing_chunks.vector_column=embedding",
            "existing_chunks.note_column=processing_note",
            "embedding.dimensions=3072",
            "embedding.progress_every_batches=1",
        ])
        with patch.object(existing_embeddings, "_embed_batch", return_value=None):
            first = existing_embeddings.embed_existing(profile)
        self.assertEqual(first["failed"], 2)
        self.assertEqual(first["skipped_empty"], 1)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT processing_note FROM epod_ci.note_test WHERE id=2")
                self.assertIn("empty", cur.fetchone()[0].lower())
                cur.execute("SELECT processing_note FROM epod_ci.note_test WHERE id=3")
                self.assertIn("retry", cur.fetchone()[0].lower())
        finally:
            conn.close()
        with patch.object(existing_embeddings, "_embed_batch",
                          side_effect=lambda texts, **kw: [[0.3] * 3072 for _ in texts]):
            second = existing_embeddings.embed_existing(profile)
        self.assertEqual(second["embedded"], 2)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT processing_note FROM epod_ci.note_test WHERE id=1")
                self.assertIsNone(cur.fetchone()[0])
                cur.execute("SELECT processing_note FROM epod_ci.note_test WHERE id=3")
                self.assertIsNone(cur.fetchone()[0])
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


    def test_full_native_pdf_profile_processing(self):
        """PDF -> structural chunks -> profile store, without Azure/GCP calls."""
        import hashlib
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        import fitz
        import main
        from crawler import CrawlResult
        from db import hash_file_path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "regulatory.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((60, 70), "1 Introduction", fontsize=13)
            page.insert_text((60, 105), "Protocol Number: MOMM281004", fontsize=11)
            for offset in range(8):
                page.insert_text(
                    (60, 140 + offset * 17),
                    "Study outcome and application number NDA 12345 section " + str(offset),
                    fontsize=10,
                )
            document.save(path)
            document.close()
            source = CrawlResult(
                file_path=str(path), file_name=path.name,
                doc_id=hash_file_path(str(path)),
                file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                file_size=path.stat().st_size,
                last_modified=datetime.now(timezone.utc),
                should_process=True,
            )
            config = load_profile(None, [
                "chunking.strategy=toc_then_semantic",
                "chunking.max_tokens=80",
                "chunking.min_tokens=16",
                "chunking.overlap_tokens=8",
                "embedding.enabled=false",
            ])
            result = main.process_document(
                source, workers=1, enable_embeddings=False, profile=config
            )
            self.assertEqual(result["status"], "SUCCESS", result.get("error"))
            self.assertGreater(result["chunks"], 0)
            self.assertTrue(profile_store.profile_is_current(
                source.doc_id, source.file_hash, config["_fingerprint"]
            ))

    def test_extracted_content_pipeline_without_pdf(self):
        """Mapped source -> chunk -> atomic profile output with no PDF access."""
        from unittest.mock import patch
        import main
        profile = load_profile(None, [
            "chunking.strategy=toc_then_semantic",
            "chunking.max_tokens=80",
            "chunking.min_tokens=16",
            "chunking.overlap_tokens=8",
            "embedding.enabled=false",
            "extracted_content.schema=epod_ci",
            "extracted_content.table=integration_extracts",
            "extracted_content.doc_id_column=doc_key",
            "extracted_content.text_column=extracted",
            "extracted_content.page_column=pg",
        ])
        with patch.object(main, "process_pdf", side_effect=AssertionError("PDF read prohibited")):
            result = main._run_extracted_profile(
                profile, dry_run=False, max_docs=2,
                doc_workers=2, enable_embeddings=False
            )
            again = main._run_extracted_profile(
                profile, dry_run=False, max_docs=2,
                doc_workers=2, enable_embeddings=False
            )
        self.assertEqual(result.counts().get("SUCCESS"), 2, result.counts())
        self.assertEqual(again.counts().get("SKIPPED"), 2, again.counts())




if __name__ == "__main__":
    unittest.main()
