"""Fast CI contract check before the full original-PDF qualification."""
import json
import tempfile
import unittest
from pathlib import Path

from real_capture_db_benchmark import QUALIFIED_SOURCES, write_benchmark_inventory, FIXTURES
from retention_batch import iter_inventory, settings


class CaptureBenchmarkInventoryTests(unittest.TestCase):
    def test_path_only_contract_and_fixed_source_identity(self):
        self.assertEqual(set(FIXTURES),set(QUALIFIED_SOURCES))
        self.assertEqual(sum(pages for pages,_ in QUALIFIED_SOURCES.values()),6353)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=settings(json.loads((Path(__file__).resolve().parents[1]/
                                     "profiles/retention_batch_example.json").read_text()))
            items=[dict(name=name,pages=pages,sha256=sha,path=str(root/(name+".pdf")))
                   for name,(pages,sha) in QUALIFIED_SOURCES.items()]
            path=root/"inventory.jsonl"
            write_benchmark_inventory(path,items,cfg)
            with path.open("rb") as stream:
                rows=list(iter_inventory(stream,cfg))
            self.assertEqual(len(rows),8)
            self.assertTrue(all(set(r)=={"path"} for r in rows))
            changed=[dict(i) for i in items]
            changed[0]["sha256"]="0"*64
            with self.assertRaisesRegex(ValueError,"source identity"):
                write_benchmark_inventory(path,changed,cfg)
            with self.assertRaisesRegex(ValueError,"source identity"):
                write_benchmark_inventory(path,items[:-1],cfg)
            duplicate=[dict(i) for i in items]
            duplicate[0]["path"]=duplicate[1]["path"]
            with self.assertRaisesRegex(ValueError,"source identity"):
                write_benchmark_inventory(path,duplicate,cfg)


if __name__=="__main__":
    unittest.main()
