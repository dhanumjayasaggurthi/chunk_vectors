import tempfile
import unittest
from pathlib import Path

from mir_ai.batch import iter_nas


class TestBatchNAS(unittest.TestCase):
    def test_lazy_filter_supported_types_and_recursive_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.pdf").write_bytes(b"x")
            (p / "b.docx").write_bytes(b"x")
            (p / "c.txt").write_text("x")
            sub = p / "sub"
            sub.mkdir()
            (sub / "d.pdf").write_bytes(b"x")
            items = list(iter_nas(p))
        self.assertEqual(
            [Path(i.canonical_path).suffix for i in items],
            [".pdf", ".docx", ".pdf"],
        )
        self.assertEqual(
            [i.logical_object_id for i in items],
            ["a", "b", "sub/d"],
        )

    def test_recursive_can_be_disabled_from_config_driven_setting(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.pdf").write_bytes(b"x")
            sub = p / "sub"
            sub.mkdir()
            (sub / "b.pdf").write_bytes(b"x")
            items = list(iter_nas(p, recursive=False))
        self.assertEqual([i.logical_object_id for i in items], ["a"])
