"""Cloud-free regression coverage for real-corpus benchmarking and worker isolation."""
from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from online_public_regulatory_benchmark import candidates, extract


class TestRegulatoryBenchmark(unittest.TestCase):
    def test_corpus_shards_are_disjoint_and_complete(self):
        universe = list(candidates(include_fda=False))
        shards = [
            [candidate for i, candidate in enumerate(universe) if i % 4 == shard]
            for shard in range(4)
        ]
        self.assertEqual(sum(len(v) for v in shards), len(universe))
        self.assertEqual(len({value[0] for shard in shards for value in shard}),
                         len(universe))

    def test_real_parser_runs_in_isolated_spawned_worker(self):
        import fitz
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"offline.pdf"
            pdf=fitz.open()
            for idx in range(3):
                page=pdf.new_page()
                page.insert_text((50,80),f"Regulatory quality page {idx+1}",
                                 fontsize=12,fontname="hebo")
            pdf.save(path)
            pdf.close()
            raw=path.read_bytes()
            item={"path":str(path),"name":path.stem,
                  "url":"file:///offline-test", "sha256":hashlib.sha256(raw).hexdigest(),
                  "pages":3,"bytes":len(raw)}
            with concurrent.futures.ProcessPoolExecutor(
                    max_workers=2,mp_context=multiprocessing.get_context("spawn"),
                    max_tasks_per_child=2) as pool:
                work=functools.partial(extract,max_pages=0,batch_size=2)
                results=list(pool.map(work,[item,item]))
            self.assertEqual([r["extracted_pages"] for r in results],[3,3])
            self.assertEqual([r["errors"] for r in results],[[],[]])
            self.assertTrue(all(r["style_pages"]==3 for r in results))


if __name__ == "__main__":
    unittest.main()
