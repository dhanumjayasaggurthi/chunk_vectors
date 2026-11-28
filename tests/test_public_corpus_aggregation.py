"""Fail-closed validation of aggregate public-corpus benchmark reports."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from aggregate_public_regulatory_reports import aggregate


class TestRegulatoryAggregation(unittest.TestCase):
    def make_report(self,root,shard,complete=True,errors=False):
        docs=[]
        for i in range(2):
            key=f"regulatory-{shard}-{i}".encode()
            docs.append({
                "name":key.decode(),
                "source_sha256":hashlib.sha256(key).hexdigest(),
                "source_pages":150,
                "extracted_pages":150 if complete else 149,
                "style_pages":100,
                "table_pages":10,
                "image_pages":5,
                "same_page_text_table_image":2,
                "errors":[{"page":150,"error":"failed"}] if errors else [],
            })
        body={
            "shard_index":shard, "shard_count":2,
            "tested_full_documents":complete,
            "accepted_documents":len(docs),
            "page_errors":sum(len(x["errors"]) for x in docs),
            "documents":docs,
        }
        path=root/f"real_regulatory_shard_{shard}.json"
        path.write_text(json.dumps(body),encoding="utf-8")
        return path

    def test_complete_and_disjoint_corpus_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            files=[self.make_report(root,i) for i in range(2)]
            result=aggregate(files,shards=2,minimum_documents=4)
            self.assertTrue(result["verified"])
            self.assertEqual(result["total_processed_pages"],600)
            self.assertEqual(result["page_errors"],0)

    def test_incomplete_or_missing_shard_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            a=self.make_report(root,0)
            b=self.make_report(root,1,complete=False,errors=True)
            result=aggregate([a,b],shards=2,minimum_documents=4)
            self.assertFalse(result["verified"])
            self.assertGreater(len(result["verification_errors"]),0)
            result=aggregate([a],shards=2,minimum_documents=4)
            self.assertFalse(result["verified"])


if __name__=="__main__":
    unittest.main()
