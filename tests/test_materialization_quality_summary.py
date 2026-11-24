"""Fast, read-only real-materializer preview quality invariants."""
import json
import unittest

from reuse_materialize import quality_summary


def row(index, total, *, words=40, page=1, table=False, broken=False):
    chunk_id=f"chunk-{index}"
    metadata={
        "section_id":"section-1",
        "previous_chunk_id":f"chunk-{index-1}" if index else None,
        "next_chunk_id":f"chunk-{index+1}" if index+1<total else None,
    }
    if broken:
        metadata["previous_chunk_id"]="nonexistent"
    if table:
        metadata.update(table_id="logical-table-A",
                        table_continuation_verified=True)
    return (
        chunk_id,"document","profile",index,"/tmp/source.pdf","source.pdf",
        " ".join(["Regulatory content."]*words),
        "Regulatory content",page,page,"Study Results",
        "[]","{}",json.dumps(metadata),"[]","{}",
    )


class TestQualitySummary(unittest.TestCase):
    def test_semantic_chunk_limits_and_linked_table_counts(self):
        rows=[
            row(0,3,words=10,page=1),
            row(1,3,words=30,page=2,table=True),
            row(2,3,words=40,page=3,table=True),
        ]
        report=quality_summary(
            rows,{"tokenizer":"cl100k_base","min_tokens":120,
                  "max_tokens":800,"boundary_mode":"semantic_v2"}
        )
        self.assertEqual(report["chunk_count"],3)
        self.assertEqual(report["logical_tables"],1)
        self.assertEqual(report["linked_multi_page_tables"],1)
        self.assertEqual(report["structural_errors"],0)
        self.assertEqual(report["chunks_above_budget"],0)
        self.assertGreater(report["chunks_below_min_tokens"],0)

    def test_broken_adjacency_fails_preview_quality(self):
        rows=[row(0,2),row(1,2,broken=True)]
        report=quality_summary(rows,{"tokenizer":"cl100k_base",
                                    "min_tokens":1,"max_tokens":800,
                                    "boundary_mode":"semantic_v2"})
        self.assertEqual(report["structural_errors"],1)
        self.assertIn("broken neighbor",report["structural_error_examples"][0])

    def test_legacy_chunks_do_not_require_v2_metadata(self):
        rows=[list(row(0,1))]
        rows[0][13]="{}"
        report=quality_summary([tuple(rows[0])],{
            "tokenizer":"cl100k_base","min_tokens":1,"max_tokens":800,
            "boundary_mode":"v1"})
        self.assertEqual(report["structural_errors"],0)

    def test_token_overrun_is_detected_without_truncation(self):
        rows=[row(0,1,words=300)]
        report=quality_summary(rows,{"tokenizer":"cl100k_base",
                                    "min_tokens":1,"max_tokens":32,
                                    "boundary_mode":"semantic_v2"})
        self.assertEqual(report["chunks_above_budget"],1)
        self.assertEqual(report["structural_errors"],1)


if __name__=="__main__":
    unittest.main()
