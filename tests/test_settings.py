import tempfile,unittest
from pathlib import Path
from mir_ai.settings import Settings
class TestSettings(unittest.TestCase):
 def test_accepts_lowercase_screenshot_style_sections(self):
  cfg='''[database]\nhost=localhost\nport=5432\nname=regulatory\nuser=u\npassword=p\nschema=dev_raw\nfolder=d05\n[paths]\ndocs_root=.\n[processing]\npage_workers=2\n'''
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'config.ini';p.write_text(cfg);s=Settings.load(p);s.validate();self.assertEqual(s.embedding_dim,3072);self.assertEqual(s.embedding_model,'text-embedding-3-large')
