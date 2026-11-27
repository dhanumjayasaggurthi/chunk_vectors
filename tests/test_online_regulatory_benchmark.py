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
from online_public_regulatory_benchmark import candidates, extract, performance_summary


class TestRegulatoryBenchmark(unittest.TestCase):
    def test_throughput_excludes_failed_pages_and_partial_documents(self):
        base={"structured_table_fragments":1,"source_table_rows":4,
              "suspected_header_only_table_pages":0}
        results=[{**base,"extracted_pages":100,"source_pages":100,"errors":[],"seconds":10},
                 {**base,"extracted_pages":80,"source_pages":80,"errors":[{"page":7}],"seconds":20},
                 {**base,"extracted_pages":5,"source_pages":200,"errors":[],"seconds":3}]
        summary=performance_summary(results,20,full_documents=True)
        self.assertEqual(summary["successful_native_pages"],184)
        self.assertEqual(summary["fully_processed_error_free_documents"],1)
        self.assertEqual(summary["native_success_pages_per_second"],9.2)
        self.assertEqual(summary["complete_documents_per_minute"],3)
        self.assertEqual(summary["document_latency_p95_seconds"],20)
        self.assertEqual(summary["max_source_pages"],200)
        self.assertFalse(summary["native_semantic_accuracy_verified"])
        self.assertIsNone(performance_summary(results,20,full_documents=False)["complete_documents_per_minute"])

    def test_corpus_shards_are_disjoint_and_complete(self):
        universe = list(candidates(include_fda=False))
        shards = [
            [candidate for i, candidate in enumerate(universe) if i % 4 == shard]
            for shard in range(4)
        ]
        self.assertEqual(sum(len(v) for v in shards), len(universe))
        self.assertEqual(len({value[0] for shard in shards for value in shard}),
                         len(universe))

    def test_open_grid_probe_preserves_all_columns_without_claiming_logical_rows(self):
        import fitz
        import pdfplumber
        from config import TABLE_SETTINGS
        from diagnose_pdf_tables import open_grid_probe
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"open-grid.pdf"
            with fitz.open() as pdf:
                for crossing in (False,True):
                    page=pdf.new_page(width=612,height=792)
                    xs=[50,200,350,550]
                    for x in xs:
                        page.draw_line((x,60),(x,700))
                    for y in (60,100):
                        page.draw_line((50,y),(550,y))
                    for x,value in zip(xs,["Visit","Dose","Response"]):
                        page.insert_text((x+8,85),value)
                    for y,values in ((120,["Visit day one","Dose amount 20 mg","Observed response"]),
                                     (145,["Visit day two","Dose amount 40 mg","Final result"]),
                                     (170,["Visit day three","Dose amount 60 mg","Follow up"]),
                                     (195,["Visit day four","Dose amount 80 mg","Follow up"])):
                        for x,value in zip(xs,values):
                            page.insert_text((x+8,y),value,fontsize=10)
                    if crossing:
                        page.insert_text((198,210),"WORD",fontsize=12)
                pdf.save(path)
            with pdfplumber.open(path) as pdf:
                first=pdf.pages[0]
                tables=first.find_tables(TABLE_SETTINGS)
                self.assertEqual(len(tables),1)
                result=open_grid_probe(first,tables[0])
                self.assertEqual(result["status"],"PHYSICAL_LINES_VERIFIED_ONLY")
                self.assertEqual(result["columns"],3)
                self.assertEqual(result["physical_lines"],4)
                self.assertFalse(result["logical_rows_verified"])
                other=pdf.pages[1]
                self.assertEqual(open_grid_probe(other,other.find_tables(TABLE_SETTINGS)[0])["status"],
                                 "GLYPH_CROSSES_COLUMN")

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
