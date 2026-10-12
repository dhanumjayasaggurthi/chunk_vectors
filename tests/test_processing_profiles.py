"""Unit tests requiring no Azure, GCP, S3, NAS or database credentials."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from processing_profile import load_profile

# Use the real chunker module: replacing sys.modules globally would break
# downstream integration tests that import the production pipeline.
class FakeChunk:
    def __init__(self, **data):
        self.__dict__.update(data)
        self.chunk_vector = []
        self.headers = []

import metadata_extractor
import configurable_chunking


def element(value, kind="text"):
    return SimpleNamespace(text=value, element_type=SimpleNamespace(value=kind))


def structure(pages):
    return SimpleNamespace(
        doc_id="doc-1", file_name="a.pdf", file_path="s3://bucket/a.pdf",
        toc=[], pages=[
            SimpleNamespace(page_number=i + 1, is_blank=False, error="",
                            elements=items, raw_text="")
            for i, items in enumerate(pages)
        ],
    )


class ProcessingProfileTests(unittest.TestCase):
    def load(self, payload, overrides=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_profile(str(path), overrides)

    def test_profile_override_and_stable_fingerprint(self):
        data = {"chunking": {"strategy": "toc_then_semantic", "max_tokens": 500}}
        a = self.load(data)
        b = self.load(data)
        c = self.load(data, ["chunking.max_tokens=450"])
        self.assertEqual(a["_fingerprint"], b["_fingerprint"])
        self.assertNotEqual(a["_fingerprint"], c["_fingerprint"])
        self.assertEqual(c["chunking"]["max_tokens"], 450)

    def test_invalid_profiles_rejected(self):
        with self.assertRaises(ValueError):
            self.load({"chunking": {"max_tokens": 80, "overlap_tokens": 50}})
        with self.assertRaises(ValueError):
            self.load({"metadata": {"document": {"number": {"method": "python"}}}})
        with self.assertRaises(ValueError):
            self.load({}, ["output.unknown=1"])
        with self.assertRaises(ValueError):
            self.load({"chunking": {"tokenizer": "arbitrary_package"}})
        with self.assertRaises(ValueError):
            self.load({"chunking": {"min_token": 200}})
        with self.assertRaises(ValueError):
            self.load({"output": {"table": "ignored_destination"}})
        with self.assertRaises(ValueError):
            self.load({"version": 2})

    def test_document_metadata_and_local_precedence(self):
        config = self.load({
            "metadata": {
                "document": {
                    "protocol": {"method": "label", "label": "Protocol Number"},
                    "application": {"method": "regex", "pattern": r"NDA\s*(\d+)"}
                },
                "chunk": {
                    "application": {"method": "regex", "pattern": r"NDA\s*(\d+)"}
                },
            }
        })
        doc = structure([[element("Protocol Number: MOMM281004\nNDA 12345")]])
        chunk = FakeChunk(chunk_text="See NDA 99999 for this subsection",
                          section_title="1.2 Scope")
        result = metadata_extractor.attach_metadata(doc, [chunk], config["metadata"])
        self.assertEqual(result["protocol"], "MOMM281004")
        self.assertEqual(chunk.document_metadata["application"], "12345")
        self.assertEqual(chunk.chunk_metadata["application"], "99999")
        self.assertEqual(chunk.effective_metadata["application"], "99999")
        self.assertEqual(chunk.headers, ["1.2 Scope"])

    def test_chunk_token_budget_sections_and_page_traceability(self):
        data = self.load({
            "chunking": {
                "strategy": "toc_then_semantic",
                "min_tokens": 16,
                "max_tokens": 80,
                "overlap_tokens": 8
            }
        })
        doc = structure([
            [element("1 Introduction\n" + " ".join(f"word{i}" for i in range(45)))],
            [element("1.1 Methods\n" + " ".join(f"sample{i}" for i in range(65))),
             element("| A | B |\n| 1 | 2 |", "table")]
        ])
        chunks = configurable_chunking.chunk_with_profile(doc, data["chunking"])
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        self.assertEqual({c.chunk_total for c in chunks}, {len(chunks)})
        self.assertTrue(all(c.page_start >= 1 and c.page_end <= 2 for c in chunks))
        self.assertTrue(any("1.1 Methods" in c.headers for c in chunks))
        self.assertTrue(any("table" in c.content_types for c in chunks))
        import tiktoken
        encoder = tiktoken.get_encoding("cl100k_base")
        self.assertTrue(all(len(encoder.encode(c.chunk_text)) <= 80 for c in chunks))
        self.assertTrue(any("word10" in c.chunk_text for c in chunks))
        self.assertTrue(any("sample20" in c.chunk_text for c in chunks))

    def test_identifier_validation(self):
        import profile_store
        self.assertEqual(profile_store._safe_identifier("my_schema"), '"my_schema"')
        for value in ("foo;drop table", "my.column", "space name", ""):
            with self.assertRaises(ValueError):
                profile_store._safe_identifier(value)


if __name__ == "__main__":
    unittest.main()
