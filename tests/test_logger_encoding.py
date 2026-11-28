"""Console encoding regressions: Windows cp1252 pipes must not break logging."""
from __future__ import annotations

import io
import logging
import unittest

from logger import _EncodingSafeConsoleHandler, _ColourFormatter


class DestinationLoggingTests(unittest.TestCase):
    def test_destination_and_legacy_fallback(self):
        from config import pipeline_log_name
        self.assertEqual(pipeline_log_name("cv_overlap_chunk_v1","legacy"),"cv_overlap_chunk_v1")
        self.assertEqual(pipeline_log_name("","regulatory"),"regulatory")
        self.assertEqual(pipeline_log_name("../unsafe/name","legacy"),"unsafe_name")
        self.assertEqual(pipeline_log_name("",""),"pipeline")

    def test_worker_logs_do_not_share_rotation_file(self):
        from pathlib import Path
        from logger import _process_log_file
        path=Path("logs/cv_overlap_chunk_v1.log")
        self.assertEqual(_process_log_file(path,is_worker=False),path)
        self.assertEqual(_process_log_file(path,is_worker=True,pid=123),
                         Path("logs/cv_overlap_chunk_v1.worker-123.log"))

    def test_console_destination_label_preserves_internal_namespace(self):
        from unittest.mock import patch
        from logger import _display_logger_name
        with patch("logger.LOG_NAME","cv_overlap_chunk_v1"):
            self.assertEqual(_display_logger_name("epod"),"cv_overlap_chunk_v1")
            self.assertEqual(_display_logger_name("epod.crawler"),"cv_overlap_chunk_v1.crawler")
            self.assertEqual(_display_logger_name("retention_batch"),"retention_batch")


class ConsoleEncodingTests(unittest.TestCase):
    def test_cp1252_backslash_escape_without_logging_exception(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        handler = _EncodingSafeConsoleHandler(stream)
        handler.setFormatter(_ColourFormatter())
        record = logging.LogRecord(
            "epod.test", logging.INFO, __file__, 1,
            "Logger initialised → logs/epod_pipeline.log", (), None,
        )
        handler.emit(record)
        handler.flush()
        output = raw.getvalue().decode("cp1252")
        self.assertIn(r"\u2192", output)
        self.assertNotIn("Logging error", output)

    def test_utf8_console_preserves_arrow(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        handler = _EncodingSafeConsoleHandler(stream)
        handler.setFormatter(_ColourFormatter())
        record = logging.LogRecord(
            "epod.test", logging.INFO, __file__, 1, "Opened → 747 pages", (), None,
        )
        handler.emit(record)
        handler.flush()
        self.assertIn("Opened → 747 pages", raw.getvalue().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
