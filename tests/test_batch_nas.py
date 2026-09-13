import tempfile,unittest
from pathlib import Path
from mir_ai.batch import iter_nas
class TestBatchNAS(unittest.TestCase):
 def test_lazy_filter_supported_types(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'a.pdf').write_bytes(b'x');(p/'b.docx').write_bytes(b'x');(p/'c.txt').write_text('x');items=list(iter_nas(p))
  self.assertEqual([Path(i.canonical_path).suffix for i in items],['.pdf','.docx'])
