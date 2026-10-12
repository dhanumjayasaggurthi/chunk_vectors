"""Unit tests for the no-re-extraction embedding backfill path."""
import unittest
from unittest.mock import patch

from processing_profile import load_profile

# The profile-store SQL integration is checked with a deterministic DB double.
import existing_embeddings


class Cursor:
    def __init__(self, connection):
        self.db = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.db.sql.append((sql, params))
        if sql.startswith("SELECT") and "LIMIT %s" in sql:
            self.db.current = [(1, "First chunk"), (2, "Second chunk")] if not self.db.read else []
            self.db.read = True

    def fetchone(self):
        return (self.db.expected_dimensions,)

    def fetchall(self):
        return self.db.current


class Connection:
    def __init__(self):
        self.sql = []
        self.read = False
        self.current = []
        self.expected_dimensions = 1536
        self.commits = 0
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class ExistingEmbeddingTests(unittest.TestCase):
    def test_only_null_embeddings_updated(self):
        profile = load_profile(None, [
            "existing_chunks.schema=archive",
            "existing_chunks.table=old_chunks",
            "existing_chunks.id_column=segment_id",
            "existing_chunks.text_column=content",
            "existing_chunks.vector_column=embedding",
        ])
        conn = Connection()
        with patch.object(existing_embeddings, "get_conn", return_value=conn), patch.object(
            existing_embeddings, "_embed_batch", return_value=[[0.1] * 1536] * 2
        ):
            result = existing_embeddings.embed_existing(profile)
        self.assertEqual(result["embedded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(conn.closed)
        self.assertEqual(conn.commits, 1)
        sql = "\n".join(statement for statement, _ in conn.sql)
        self.assertIn('"archive"."old_chunks"', sql)
        self.assertIn('"embedding" IS NULL', sql)

    def test_3072_dimension_from_profile(self):
        profile = load_profile(None, [
            "existing_chunks.schema=archive",
            "existing_chunks.table=haq_query",
            "existing_chunks.id_column=id",
            "existing_chunks.text_column=question",
            "existing_chunks.vector_column=question_embedding",
            "embedding.dimensions=3072",
            "embedding.model=text-embedding-3-large",
        ])
        conn = Connection()
        conn.expected_dimensions = 3072
        with patch.object(existing_embeddings, "get_conn", return_value=conn), patch.object(
            existing_embeddings, "_embed_batch", return_value=[[0.1] * 3072] * 2
        ) as embed:
            result = existing_embeddings.embed_existing(profile)
        self.assertEqual(result["embedded"], 2)
        embed.assert_called_once_with(
            ["First chunk", "Second chunk"], dimensions=3072, model="text-embedding-3-large"
        )

    def test_column_dimension_mismatch_aborts_before_api(self):
        profile = load_profile(None, [
            "existing_chunks.schema=archive",
            "existing_chunks.table=haq_query",
            "existing_chunks.id_column=id",
            "existing_chunks.text_column=question",
            "existing_chunks.vector_column=question_embedding",
            "embedding.dimensions=3072",
        ])
        conn = Connection()
        with patch.object(existing_embeddings, "get_conn", return_value=conn), patch.object(
            existing_embeddings, "_embed_batch"
        ) as embed:
            with self.assertRaises(ValueError):
                existing_embeddings.embed_existing(profile)
            embed.assert_not_called()

    def test_unsafe_input_table_rejected(self):
        profile = load_profile(None, ["existing_chunks.table=invalid;drop"])
        with self.assertRaises(ValueError):
            existing_embeddings.embed_existing(profile, dry_run=True)


if __name__ == "__main__":
    unittest.main()
