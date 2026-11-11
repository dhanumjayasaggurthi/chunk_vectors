"""Small real-PDF integration test for resumable extraction and safe publishing."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

import fitz

from ingestion_v2.plan import ExtractionPolicy
from ingestion_v2.extract import run_local, extract_range
from ingestion_v2.cli import chunk_artifacts, load_complete_pages
from ingestion_v2.chunking import ChunkPolicy


class TestNativePdfCheckpoint(unittest.TestCase):
    def make_pdf(self,path):
        doc=fitz.open()
        for n in range(5):
            page=doc.new_page()
            page.insert_text((72,72),
                ("Page "+str(n+1)+": confirmed native text extraction. ")*10)
        doc.save(str(path))
        doc.close()

    def test_resumable_atomic_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf=Path(tmp)/"sample.pdf"
            self.make_pdf(pdf)
            policy=ExtractionPolicy(range_pages=2,cpu_processes=2,
                                    native_text_min_chars=50)
            first=run_local(pdf,Path(tmp)/"out",policy)
            self.assertEqual(first["page_count"],5)
            self.assertEqual(first["SUCCESS"],3)
            self.assertEqual(first["FAILED"],0)
            self.assertEqual(first["ocr_candidates"],0)
            second=run_local(pdf,Path(tmp)/"out",policy)
            self.assertEqual(second["SUCCESS"],0)
            self.assertEqual(second["SKIPPED"],3)
            manifest,pages=load_complete_pages(first["output_dir"])
            self.assertEqual(len(pages),5)
            self.assertTrue(all(p["text_blocks"] for p in pages))
            self.assertTrue(all("bbox" in p["text_blocks"][0] for p in pages))

            result=chunk_artifacts(first["output_dir"],Path(tmp)/"chunks.json.gz",
                                   ChunkPolicy())
            self.assertGreater(result["chunks"],0)
            with gzip.open(result["output"],"rt",encoding="utf-8") as source:
                artifact=json.load(source)
            self.assertEqual(len(artifact["chunks"]),result["chunks"])
            self.assertTrue(all(chunk["token_count"]<=800 for chunk in artifact["chunks"]))

    def test_pilot_subset_refuses_complete_chunk_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf=Path(tmp)/"sample.pdf"
            self.make_pdf(pdf)
            policy=ExtractionPolicy(range_pages=2,cpu_processes=1)
            result=run_local(pdf,Path(tmp)/"out",policy,max_ranges=1)
            self.assertEqual(result["SUCCESS"],1)
            with self.assertRaisesRegex(ValueError,"Pilot only extracted"):
                load_complete_pages(result["output_dir"])

    def test_file_version_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf=Path(tmp)/"sample.pdf"
            self.make_pdf(pdf)
            state=pdf.stat()
            with self.assertRaisesRegex(RuntimeError,"Source version"):
                extract_range(pdf,0,1,state.st_size,state.st_mtime_ns+1,100)


if __name__=="__main__":
    unittest.main()
