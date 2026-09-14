import json
import tempfile
import unittest
from pathlib import Path

from mir_ai.rimdocs import JSONLRimDocsProvider


class TestRimDocsProvider(unittest.TestCase):
    def test_disk_backed_lookup_and_per_document_version(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "r.jsonl"
            src.write_text(
                json.dumps(
                    {
                        "canonical_path": "/a.pdf",
                        "metadata": {"Study ID": "S1"},
                    }
                )
                + "\n"
            )
            p = JSONLRimDocsProvider(src, Path(d) / "cache")
            try:
                self.assertEqual(p.get("/a.pdf")["Study ID"], "S1")
                self.assertIsNone(p.get("/missing"))
                metadata, version = p.get_with_version("/a.pdf")
                self.assertEqual(metadata["Study ID"], "S1")
                self.assertEqual(len(version), 64)
                missing, missing_version = p.get_with_version("/missing")
                self.assertIsNone(missing)
                self.assertEqual(missing_version, "none")
                self.assertTrue(p.db_path.exists())
            finally:
                p.close()
