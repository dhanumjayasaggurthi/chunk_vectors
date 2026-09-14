import tempfile
import unittest
from pathlib import Path

from mir_ai.settings import Settings


class TestSettings(unittest.TestCase):
    def test_accepts_lowercase_screenshot_style_sections(self):
        cfg = """[database]
host=localhost
port=5432
name=regulatory
user=u
password=p
schema=dev_raw
folder=d05
[paths]
docs_root=.
source_type=nas
[processing]
page_workers=2
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(cfg)
            s = Settings.load(p)
            s.validate()
            self.assertEqual(s.embedding_dim, 3072)
            self.assertEqual(s.embedding_model, "text-embedding-3-large")

    def test_source_controls_are_configurable(self):
        cfg = """[POSTGRES]
host=localhost
port=5432
database=regulatory
user=u
password=p
schema=dev_raw
[PATHS]
docs_root=/archive
source_type=s3
log_dir=custom_logs
[MIR_AI]
source_auto_run=true
source_recursive=false
source_max_files=100
rimdocs_jsonl_path=C:\\data\\rimdocs.jsonl
object_list_enabled=false
enable_pdf=true
enable_docx=false
preferred_format=pdf
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(cfg)
            s = Settings.load(p)
            s.validate()
            self.assertEqual(s.source_type, "s3")
            self.assertTrue(s.source_auto_run)
            self.assertFalse(s.source_recursive)
            self.assertEqual(s.source_max_files, 100)
            self.assertFalse(s.object_list_enabled)
            self.assertTrue(s.enable_pdf)
            self.assertFalse(s.enable_docx)
            self.assertEqual(str(s.log_dir), "custom_logs")

    def test_invalid_source_type_fails_closed(self):
        cfg = """[POSTGRES]
host=localhost
database=regulatory
user=u
password=p
schema=dev_raw
[PATHS]
source_type=ftp
docs_root=.
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(cfg)
            s = Settings.load(p)
            with self.assertRaisesRegex(ValueError, "source_type"):
                s.validate()
