"""Focused tests for the source-chunk migration, no Azure or database required."""
import unittest

import tiktoken

from chunk_migration import rebuild_chunks, source_mapping
from processing_profile import load_profile


def row(n, text, title="1 Introduction", start=1, end=None):
    return {
        "doc_id_column": "doc-42", "text_column": text,
        "chunk_id_column": f"old-{n}", "chunk_index_column": n,
        "file_name_column": "test.pdf", "file_path_column": "nas://test.pdf",
        "section_title_column": title, "page_start_column": start,
        "page_end_column": end or start, "content_types_column": ["text"],
    }


class ChunkMigrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(None, [
            "chunking.strategy=toc_then_semantic",
            "chunking.min_tokens=20", "chunking.max_tokens=80",
            "chunking.overlap_tokens=8", "embedding.enabled=false",
        ])

    def test_validates_unsafe_source_identifiers(self):
        self.profile["source_chunks"] = {"schema": "safe", "table": "broken;drop"}
        with self.assertRaises(ValueError):
            source_mapping(self.profile)

    def test_rechunks_with_token_bounds_and_stable_ids(self):
        rows = [
            row(0, "Protocol Number: ABC-42\n" + " ".join(f"sample{i}" for i in range(110)), start=1),
            row(1, " ".join(f"result{i}" for i in range(120)), "1.1 Findings", start=2, end=3),
        ]
        struct, chunks = rebuild_chunks(rows, self.profile)
        enc = tiktoken.get_encoding("cl100k_base")
        self.assertEqual(struct.doc_id, "doc-42")
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(enc.encode(c.chunk_text)) <= 80 for c in chunks))
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        self.assertTrue(all(c.page_start >= 1 and c.page_end <= 3 for c in chunks))
        self.assertTrue(all(c.chunk_total == len(chunks) for c in chunks))
        self.assertEqual([c.chunk_id for c in chunks],
                         [c.chunk_id for c in rebuild_chunks(rows, self.profile)[1]])

    def test_short_section_is_merged_forward_when_it_fits(self):
        rows = [row(0, "Short context."), row(1, "Another short paragraph.", "2 Methods", start=2)]
        _, chunks = rebuild_chunks(rows, self.profile)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].page_start, 1)
        self.assertEqual(chunks[0].page_end, 2)

    def test_numbered_subsection_headers_are_retained(self):
        rows = [row(0, "1.1 Detailed Results\n" + "The result was positive. " * 7,
                    "1 Results")]
        _, chunks = rebuild_chunks(rows, self.profile)
        self.assertTrue(any("1.1 Detailed Results" in c.headers for c in chunks))

    def test_unicode_safe_long_text(self):
        rows = [row(0, "研究结果📄" * 130, "1 Results")]
        _, chunks = rebuild_chunks(rows, self.profile)
        self.assertTrue(chunks)
        self.assertFalse(any("\ufffd" in c.chunk_text for c in chunks))
        self.assertTrue(all(len(tiktoken.get_encoding("cl100k_base").encode(c.chunk_text)) <= 80
                            for c in chunks))

    def test_retains_page_range_without_inventing_precision(self):
        rows = [row(0, "Existing block from multiple pages " * 4, start=7, end=11)]
        _, chunks = rebuild_chunks(rows, self.profile)
        self.assertTrue(chunks)
        self.assertTrue(all(ch.page_start == 7 and ch.page_end == 11 for ch in chunks))


if __name__ == "__main__":
    unittest.main()
