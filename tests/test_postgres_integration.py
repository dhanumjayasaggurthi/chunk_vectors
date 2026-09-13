import os
import tempfile
import unittest
from pathlib import Path

from mir_ai.models import BBox, ChunkRecord, ElementRef, PageRecord
from mir_ai.settings import Settings
from mir_ai.store import PostgresStore


@unittest.skipUnless(
    os.environ.get("MIRAI_PG_INTEGRATION") == "1",
    "set MIRAI_PG_INTEGRATION=1 to run pgvector integration coverage",
)
class TestPostgresIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = Path(self.temp.name) / "config.ini"
        config.write_text(
            """
[POSTGRES]
host = 127.0.0.1
port = 5432
database = mirai_test
user = postgres
password = postgres
schema = mirai_ci
folder = mirai

[PATHS]
docs_root = .
source_type = nas

[MIR_AI]
scratch_dir = .mirai_test_scratch
page_workers = 2
max_inflight_pages = 4
doc_workers = 1
max_inflight_docs = 1
enable_embeddings = false
embedding_model = text-embedding-3-large
embedding_dim = 3072
embedding_api_version = 2025-04-01-preview
chunk_target_min_tokens = 1200
chunk_target_max_tokens = 1500
chunk_overlap_tokens = 100
vector_index_mode = exact
""".strip(),
            encoding="utf-8",
        )
        self.settings = Settings.load(config)
        self.store = PostgresStore(self.settings, minconn=1, maxconn=4)
        with self.store.conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA IF EXISTS mirai_ci CASCADE")
            connection.commit()
        self.store.init_schema()

    def tearDown(self):
        try:
            with self.store.conn() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DROP SCHEMA IF EXISTS mirai_ci CASCADE")
                connection.commit()
        finally:
            self.store.close()
            self.temp.cleanup()

    def test_generation_page_vector_and_atomic_activation(self):
        canonical = "//archive/report.pdf"
        doc_id = self.store.ensure_document(
            canonical,
            "rimdocs://report",
            {"Study ID": "TOX-42"},
        )
        generation_id = self.store.ensure_generation(
            doc_id,
            "hash-1",
            "version-1",
            processing_fingerprint="profile-1",
        )
        self.assertTrue(self.store.claim_generation(generation_id, "worker-a", 600, 3))

        page = PageRecord(
            1,
            "1",
            612,
            792,
            elements=[
                ElementRef(
                    "table-1",
                    "table",
                    BBox(10, 10, 400, 200),
                    0,
                    text="Study Administration\nStudy ID: TOX-42",
                    extraction_status="success",
                )
            ],
            native_text="Native front matter",
        )
        self.store.upsert_page(generation_id, page)
        self.assertTrue(
            self.store.heartbeat(
                generation_id,
                "worker-a",
                600,
                stage="PAGE_EXTRACTION_COMPLETE",
                last_page=1,
                pages_total=1,
            )
        )
        progress = self.store.generation_progress(generation_id)
        self.assertEqual(progress["pages_total"], 1)

        candidates = list(self.store.iter_metadata_candidate_pages(generation_id, 20))
        self.assertEqual([item.page_number for item in candidates], [1])

        vector = [0.0] * 3072
        vector[0] = 1.0
        chunk = ChunkRecord(
            "chunk-1",
            doc_id,
            generation_id,
            0,
            "Study ID: TOX-42",
            1,
            1,
            ["1"],
            ["Study Administration"],
            ["table"],
            "rimdocs://report",
            [{"page": 1, "bbox": [10, 10, 400, 200]}],
            {},
            vector,
        )
        self.store.insert_chunk_batch([chunk])
        self.store.activate_generation(generation_id, "worker-a")

        active = self.store.active_source_state(canonical)
        self.assertEqual(active["generation_id"], generation_id)
        self.assertEqual(active["pages_total"], 1)
        self.assertEqual(active["chunk_count"], 1)

        results = self.store.search_chunks(vector, top_k=1, doc_id=doc_id)
        self.assertEqual(results[0]["chunk_id"], "chunk-1")
        self.assertGreater(results[0]["similarity"], 0.99)


if __name__ == "__main__":
    unittest.main()
