"""Document readiness definitions and safe profile discovery."""
import json
import tempfile
import unittest
from pathlib import Path

from embedding_dashboard_docs import (
    calculate_coverage, datasets_from_profiles, document_stage,
)


class DocumentDashboardLogicTests(unittest.TestCase):
    def test_document_is_complete_only_when_every_chunk_has_vector(self):
        self.assertEqual(document_stage(4,4),"Complete")
        self.assertEqual(document_stage(4,3),"Partial")
        self.assertEqual(document_stage(4,1),"Partial")
        self.assertEqual(document_stage(4,0),"Not started")
        self.assertEqual(document_stage(0,0),"Unknown")

    def test_chunk_and_doc_ratios_are_distinct(self):
        self.assertEqual(calculate_coverage(3,4),.75)
        self.assertEqual(calculate_coverage(0,0),0.0)
        self.assertEqual(calculate_coverage(4,4),1.0)

    def test_profiles_are_curated_without_sql_from_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/"immbot_approved.json").write_text(json.dumps({
                "existing_chunks":{
                    "schema":"palm_paw","table":"doc_chunks_immbot_approved",
                    "id_column":"chunk_id","vector_column":"chunk_vector",
                }
            }),encoding="utf-8")
            (root/"pdf_ingestion.json").write_text(json.dumps({
                "chunking":{"max_tokens":512}
            }),encoding="utf-8")
            datasets=datasets_from_profiles(root)
            self.assertEqual(len(datasets),1)
            self.assertEqual(datasets[0]["table"],"doc_chunks_immbot_approved")
            self.assertEqual(datasets[0]["id_column"],"chunk_id")
