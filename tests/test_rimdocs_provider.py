import json,tempfile,unittest
from pathlib import Path
from mir_ai.rimdocs import JSONLRimDocsProvider
class TestRimDocsProvider(unittest.TestCase):
 def test_disk_backed_lookup(self):
  with tempfile.TemporaryDirectory() as d:
   src=Path(d)/'r.jsonl';src.write_text(json.dumps({'canonical_path':'/a.pdf','metadata':{'Study ID':'S1'}})+'\n');p=JSONLRimDocsProvider(src,Path(d)/'cache')
   try:self.assertEqual(p.get('/a.pdf')['Study ID'],'S1');self.assertEqual(p.get('/missing'),{});self.assertTrue(p.db_path.exists())
   finally:p.close()
