from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_SECRET_PATTERNS = [re.compile(r"(?i)(api[-_ ]?key\s*[=:]\s*)\S+"), re.compile(r"(?i)(password\s*[=:]\s*)\S+"), re.compile(r"(?i)(secret\s*[=:]\s*)\S+")]


def _redact(value: str) -> str:
    out = value
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r"\1<redacted>", out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(), "level": record.levelname, "logger": record.name, "message": _redact(record.getMessage())}
        for key in ("run_id", "doc_id", "generation_id", "stage", "page", "chunk_id", "attempt", "elapsed_ms", "error_class"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_listener: logging.handlers.QueueListener | None = None


def configure_logging(log_dir: str | Path = "logs", level: str = "INFO") -> None:
    global _listener
    if _listener is not None:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    q: queue.Queue = queue.Queue(maxsize=10000)
    handler = logging.handlers.QueueHandler(q)
    root = logging.getLogger("mir_ai")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers[:] = [handler]
    root.propagate = False
    fh = logging.handlers.RotatingFileHandler(log_dir / "mir_ai.jsonl", maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(JsonFormatter())
    _listener = logging.handlers.QueueListener(q, fh, ch, respect_handler_level=True)
    _listener.start()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("mir_ai") else f"mir_ai.{name}")
