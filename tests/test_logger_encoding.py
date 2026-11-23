"""Console encoding regressions: Windows cp1252 pipes must not break logging."""
from __future__ import annotations

import io
import logging
import unittest

from logger import _EncodingSafeConsoleHandler, _ColourFormatter


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
